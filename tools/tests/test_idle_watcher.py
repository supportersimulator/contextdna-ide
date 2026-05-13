"""Tests for tools/fleet_idle_watcher.py — watchdog-based idle detection.

Skipped automatically when `watchdog` is not installed so CI without the
optional dependency still passes.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

watchdog = pytest.importorskip("watchdog")

from tools.fleet_idle_watcher import start_event_idle_watcher  # noqa: E402


class _FakeDaemon:
    """Stand-in for FleetNerveDaemon — just the two attrs we need."""

    def __init__(self) -> None:
        self._last_activity = 0.0
        self.idle_fired = threading.Event()
        self.idle_fire_count = 0

    def _check_idle(self) -> None:
        self.idle_fire_count += 1
        self.idle_fired.set()


def test_start_returns_false_when_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    d = _FakeDaemon()
    ok = start_event_idle_watcher(d, watch_dir=missing, idle_threshold_s=0.5)
    assert ok is False


def test_activity_event_updates_last_activity(tmp_path: Path) -> None:
    d = _FakeDaemon()
    before = time.time()
    ok = start_event_idle_watcher(
        d, watch_dir=tmp_path, idle_threshold_s=60.0,  # long — don't fire during test
    )
    assert ok is True
    try:
        # Touch a .jsonl file to generate a modify event.
        target = tmp_path / "session-abc.jsonl"
        target.write_text("hello\n")
        # watchdog dispatch is async — wait up to 3s for event propagation.
        deadline = time.time() + 3.0
        while d._last_activity < before and time.time() < deadline:
            target.write_text(target.read_text() + "x")
            time.sleep(0.1)
        assert d._last_activity >= before, (
            "last_activity should advance after a jsonl modify event"
        )
    finally:
        stop = getattr(d, "_idle_watcher_stop_fn", None)
        if stop:
            stop()


def test_idle_fires_after_threshold_with_no_events(tmp_path: Path) -> None:
    d = _FakeDaemon()
    ok = start_event_idle_watcher(
        d, watch_dir=tmp_path, idle_threshold_s=0.4,  # short for test
    )
    assert ok is True
    try:
        # No file events → the timer must fire _check_idle within ~1s.
        fired = d.idle_fired.wait(timeout=2.0)
        assert fired, "idle callback should fire after threshold with no events"
        assert d.idle_fire_count >= 1
    finally:
        stop = getattr(d, "_idle_watcher_stop_fn", None)
        if stop:
            stop()


def test_idle_timer_rearms_on_activity(tmp_path: Path) -> None:
    """An event during the idle window should reset the timer, delaying fire."""
    d = _FakeDaemon()
    ok = start_event_idle_watcher(
        d, watch_dir=tmp_path, idle_threshold_s=0.5,
    )
    assert ok is True
    try:
        # Immediately touch a file 3× over ~0.9s — idle should NOT fire yet
        # because threshold resets on each event.
        target = tmp_path / "s.jsonl"
        for _ in range(3):
            target.write_text(f"{time.time()}\n")
            time.sleep(0.25)
        # At this point elapsed since first write ~0.75s but last activity
        # was ~0.25s ago → below threshold → callback should still be 0.
        # Give watchdog a moment to have processed the last event.
        time.sleep(0.05)
        assert d.idle_fire_count == 0, (
            "idle should not fire while activity keeps re-arming the timer"
        )
        # Now stop touching the file; within ~1s the timer fires.
        fired = d.idle_fired.wait(timeout=2.0)
        assert fired
    finally:
        stop = getattr(d, "_idle_watcher_stop_fn", None)
        if stop:
            stop()
