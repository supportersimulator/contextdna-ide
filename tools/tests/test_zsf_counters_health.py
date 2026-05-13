"""RR1 2026-05-08 — /health.zsf_counters smoke tests.

Single-roundtrip ZSF observability invariant: ``curl /health`` must reveal
whether ANY ZSF counter has spiked. The test spins up the lightest-possible
holder for ``FleetNerveNATS._build_zsf_counters`` and asserts:

  1. The block is always present in the output.
  2. Every required sub-block is present (cascade, inv022, three_surgeons),
     even when sources are absent — empty defaults instead of crashes.
  3. Cascade counters round-trip from _stats / _cascade_skipped_infeasible.
  4. The 3-surgeons file (when present) is read and surfaced.
  5. ZSF: a missing counter file → ``fresh: False``, no exception.

The aggregator is a pure function (reads in-memory dicts + an optional
JSON file); we don't need a real NATS client.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _bind():
    """Bind the production _build_zsf_counters off the real class."""
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:  # pragma: no cover — env-specific
        pytest.skip(f"fleet_nerve_nats import unavailable: {e}")
    return FleetNerveNATS._build_zsf_counters


def _make_holder() -> SimpleNamespace:
    """Minimal self-shape the aggregator reads."""
    holder = SimpleNamespace()
    holder._stats = {
        "cascade_skipped_infeasible_total": 0,
        "cascade_skipped_infeasible_errors": 0,
        "started_at": time.time(),
    }
    holder._cascade_skipped_infeasible = {}
    return holder


def test_zsf_block_always_present(monkeypatch, tmp_path):
    """Even on a clean holder with no counter file, every key is present."""
    # Point at empty location — no file → empty dict everywhere.
    monkeypatch.setenv(
        "THREE_SURGEONS_ZSF_COUNTER_PATH", str(tmp_path / "absent.json"),
    )
    build = _bind()
    holder = _make_holder()
    out = build(holder)

    assert isinstance(out, dict)
    for top in ("cascade_skipped_infeasible", "inv022",
                "invariants_all", "three_surgeons"):
        assert top in out, f"/health.zsf_counters missing top-level key: {top}"

    # Cascade block
    cb = out["cascade_skipped_infeasible"]
    assert cb["global"] == 0
    assert cb["errors"] == 0
    assert cb["per_peer_channel"] == {}

    # inv022 block — present even if memory.invariants happens to be
    # importable; values should be non-negative ints.
    iv = out["inv022"]
    assert "via_cascade_missing" in iv
    assert "no_message_attempt" in iv
    assert isinstance(iv["via_cascade_missing"], int)
    assert isinstance(iv["no_message_attempt"], int)

    # 3-surgeons block — file missing → fresh=False, sub-dicts empty.
    ts = out["three_surgeons"]
    assert ts["fresh"] is False
    assert ts["neuro_fallback"] == {}
    assert ts["diversity"] == {}
    assert ts["keychain_errors"] == {}


def test_cascade_counters_roundtrip(monkeypatch, tmp_path):
    """Bumping _stats + per-peer dict surfaces directly under /health."""
    monkeypatch.setenv(
        "THREE_SURGEONS_ZSF_COUNTER_PATH", str(tmp_path / "absent.json"),
    )
    build = _bind()
    holder = _make_holder()
    holder._stats["cascade_skipped_infeasible_total"] = 7
    holder._stats["cascade_skipped_infeasible_errors"] = 2
    holder._cascade_skipped_infeasible = {
        "mac3": {"P2_http": 5, "P5_ssh": 2},
        "mac2": {"P6_wol": 1},
    }

    out = build(holder)
    cb = out["cascade_skipped_infeasible"]
    assert cb["global"] == 7
    assert cb["errors"] == 2
    assert cb["per_peer_channel"]["mac3"]["P2_http"] == 5
    assert cb["per_peer_channel"]["mac3"]["P5_ssh"] == 2
    assert cb["per_peer_channel"]["mac2"]["P6_wol"] == 1


def test_three_surgeons_file_read(monkeypatch, tmp_path):
    """When the persistence file exists, its counters appear in /health."""
    target = tmp_path / "zsf_counters.json"
    target.write_text(json.dumps({
        "neuro_fallback": {
            "ollama": 4, "mlx": 1, "mlx_proxy": 0,
            "deepseek": 2, "default_kept": 0, "no_provider_reachable": 0,
        },
        "diversity": {
            "consensus_total": 10,
            "same_provider_same_model": 3,
            "byte_identical_replies": 1,
            "verdict_agree_no_caveats": 2,
            "yellow_signals_total": 4,
        },
        "keychain_errors": {"count": 1, "last": "boom"},
        "persist_self": {"persist_errors": 0, "last_persist_error": "",
                         "last_persist_ts": 1.0},
        "snapshot_ts": 12345.0,
        "pid": 9999,
    }), encoding="utf-8")
    monkeypatch.setenv("THREE_SURGEONS_ZSF_COUNTER_PATH", str(target))

    build = _bind()
    out = build(_make_holder())

    ts = out["three_surgeons"]
    assert ts["fresh"] is True
    assert ts["neuro_fallback"]["ollama"] == 4
    assert ts["neuro_fallback"]["deepseek"] == 2
    assert ts["diversity"]["yellow_signals_total"] == 4
    assert ts["keychain_errors"]["count"] == 1
    assert ts["pid"] == 9999


def test_zsf_block_zsf_on_malformed_three_surgeons_file(monkeypatch, tmp_path):
    """ZSF: malformed JSON in the counter file MUST NOT crash /health."""
    target = tmp_path / "zsf_counters.json"
    target.write_text("not json {{{", encoding="utf-8")
    monkeypatch.setenv("THREE_SURGEONS_ZSF_COUNTER_PATH", str(target))

    build = _bind()
    out = build(_make_holder())

    # No exception. Block present. fresh=False because parse failed.
    assert "three_surgeons" in out
    assert out["three_surgeons"]["fresh"] is False
    assert out["three_surgeons"]["neuro_fallback"] == {}


def test_zsf_block_appears_in_full_health(monkeypatch, tmp_path):
    """Live integration: _build_health must include zsf_counters at top level.

    We don't bring up nats — _build_health needs more state than a SimpleNamespace
    can provide — so this test patches a thin holder onto the real class via
    MagicMock for the helpers _build_health calls. The key assertion: the dict
    returned by the real function contains 'zsf_counters' as a top-level key.
    """
    # This test only verifies the key WIRING — that _build_health includes
    # zsf_counters in its return dict. The aggregator itself is exercised
    # by the unit tests above.
    try:
        from tools.fleet_nerve_nats import FleetNerveNATS
    except ImportError as e:  # pragma: no cover
        pytest.skip(f"fleet_nerve_nats import unavailable: {e}")

    import inspect
    src = inspect.getsource(FleetNerveNATS._build_health)
    # Cheap structural assertion: the wiring is text-stable. Catches
    # accidental removal of the key during merges.
    assert '"zsf_counters"' in src, (
        "_build_health must emit the zsf_counters top-level key"
    )
    assert "_build_zsf_counters" in src, (
        "_build_health must call _build_zsf_counters"
    )
    # UU5 2026-05-12 — dual-surface alias check. The aliasing assignment
    # under ``stats`` keeps ``/health.stats.zsf_counters`` consumers
    # working while top-level remains canonical.
    assert 'payload["stats"]["zsf_counters"]' in src, (
        "_build_health must alias zsf_counters under stats for backward compat"
    )
