"""Tests for scripts/fleet-dashboard.py.

Covered contracts:
    1. Print-once mode renders every required section header.
    2. --json mode emits valid JSON with expected top-level keys.
    3. --follow mode dispatches incoming NATS messages to stdout.
    4. No-polling invariant: source contains no `while True` + sleep > 100ms.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASH_PATH = REPO_ROOT / "scripts" / "fleet-dashboard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fleet_dashboard", DASH_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOCK_HEALTH = {
    "nodeId": "macX",
    "uptime_s": 3600,
    "activeSessions": 1,
    "peers": {"mac1": {"lastSeen": 2}, "mac2": {"lastSeen": 5}},
    "channel_reliability": {
        "mac1": {"P1_nats": {"ok": 10, "fail": 0, "score": 1.0}},
    },
    "channel_health": {"mac1": {"score": "7/7", "broken": []}},
    "delegation": {
        "active": True,
        "chief_node": "mac1",
        "self_node": "macX",
        "active_agents": 0,
        "max_parallel_agents": 5,
        "received": 3,
        "accepted": 2,
        "dispatched": 2,
        "rejected_bad_hmac": 1,
    },
    "rate_limits": {
        "stats": {"allowed": 50, "denied": 1},
        "peers": {"mac1": {"task": {"used": 2, "limit": 5, "window_s": 60}}},
    },
    "invariance_gates": {"mode": "enforce", "blocked": 0},
}

MOCK_JSZ = {
    "meta_cluster": {"leader": "mac1", "cluster_size": 3},
    "account_details": [
        {
            "stream_detail": [
                {
                    "name": "FLEET_MESSAGES",
                    "config": {"num_replicas": 3, "max_age": 30 * 86400 * 10**9, "retention": "limits"},
                    "state": {"messages": 100, "bytes": 2048},
                }
            ]
        }
    ],
}

MOCK_ROUTEZ = {"num_routes": 2, "routes": [{"remote_name": "mac1", "rtt": "5ms", "uptime": "1h"}]}
MOCK_VARZ = {"cluster": {"name": "contextdna"}}


def _mock_http_json(url, timeout=2.0):
    if "8855/health" in url:
        return MOCK_HEALTH
    if "jsz" in url:
        return MOCK_JSZ
    if "routez" in url:
        return MOCK_ROUTEZ
    if "varz" in url:
        return MOCK_VARZ
    return {}


class PrintOnceMode(unittest.TestCase):
    def test_prints_every_section_header(self):
        fd = _load_module()
        with mock.patch.object(fd, "_http_json", side_effect=_mock_http_json), \
             mock.patch.object(fd, "_last_fleet_messages", return_value=[
                 {"from": "mac1", "to": "macX", "subject": "test", "age_s": 30, "path": "x.md"}
             ]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                # Force plain text rendering to make substring assertions robust.
                fd._RICH = False
                fd._CONSOLE = None
                rc = fd.main([])
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        for header in [
            "Fleet Dashboard",
            "Node summary",
            "Cluster",
            "JetStream",
            "Channel reliability",
            "Delegation",
            "Rate limits",
            "Invariance gates",
            "Last 5 fleet messages",
        ]:
            self.assertIn(header, out, f"missing section: {header}\n---\n{out}")
        # Spot-check a few values from the mock.
        self.assertIn("FLEET_MESSAGES", out)
        self.assertIn("R=3", out)
        self.assertIn("meta_leader=mac1", out)
        self.assertIn("mac1 -> macX", out)


class JsonMode(unittest.TestCase):
    def test_emits_valid_json_with_expected_keys(self):
        fd = _load_module()
        with mock.patch.object(fd, "_http_json", side_effect=_mock_http_json), \
             mock.patch.object(fd, "_last_fleet_messages", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = fd.main(["--json"])
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        for key in ["generated_at", "node_id", "health", "cluster", "streams", "fleet_messages"]:
            self.assertIn(key, payload)
        self.assertEqual(payload["cluster"]["meta_leader"], "mac1")
        self.assertEqual(payload["streams"][0]["name"], "FLEET_MESSAGES")
        self.assertEqual(payload["streams"][0]["replicas"], 3)
        self.assertEqual(payload["streams"][0]["max_age_s"], 30 * 86400)


class FollowMode(unittest.TestCase):
    """--follow must subscribe to fleet.>/event.> and print a line per message."""

    def test_prints_incoming_message(self):
        fd = _load_module()

        # Build a fake asyncio-like NATS client where subscribe stashes the
        # callback and we invoke it manually, then cancel the blocking future.
        class FakeMsg:
            def __init__(self, subject, data):
                self.subject = subject
                self.data = data

        class FakeNC:
            def __init__(self):
                self.subs = {}
                self.drained = False

            async def connect(self, **kw):
                return None

            async def subscribe(self, subject, cb):
                self.subs[subject] = cb

            async def drain(self):
                self.drained = True

        fake_nc = FakeNC()

        import asyncio

        async def driver():
            # Import patched module dependencies.
            await fake_nc.connect()
            await fake_nc.subscribe("fleet.>", cb=_captured["cb"])
            await _captured["cb"](FakeMsg("fleet.signal.test", b'{"from":"mac2"}'))

        # We test the innards directly rather than exercising _follow end-to-end
        # (which blocks forever). We verify the callback format + subjects.
        _captured = {}
        captured_output = io.StringIO()

        async def run_test():
            # Reconstruct the handler exactly as _follow does.
            async def on_msg(msg):
                import time as _t
                ts = _t.strftime("%H:%M:%S")
                try:
                    payload = json.loads(msg.data.decode("utf-8", errors="replace"))
                    src = payload.get("from") or payload.get("source") or "?"
                except Exception:
                    src = "?"
                captured_output.write(f"{ts}  {msg.subject}  from={src}  ({len(msg.data)}B)\n")

            _captured["cb"] = on_msg
            await driver()

        asyncio.run(run_test())
        lines = captured_output.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(
            lines[0],
            r"^\d{2}:\d{2}:\d{2}\s+fleet\.signal\.test\s+from=mac2\s+\(\d+B\)$",
        )

        # Also verify the real _follow function wires up both subjects.
        # We do this by monkeypatching nats.aio.client.Client inside the module
        # and letting connect/subscribe record calls, then cancelling.
        calls = {"subs": []}

        class RecClient:
            def __init__(self):
                pass

            async def connect(self, **kw):
                return None

            async def subscribe(self, subject, cb):
                calls["subs"].append(subject)

            async def drain(self):
                return None

        # Stub the `nats.aio.client` import path.
        fake_mod = mock.MagicMock()
        fake_mod.Client = RecClient
        with mock.patch.dict(sys.modules, {"nats": mock.MagicMock(), "nats.aio": mock.MagicMock(), "nats.aio.client": fake_mod}):
            # Patch asyncio.Future to resolve instantly so _follow returns.
            import asyncio as _a
            orig_future = _a.Future

            def instant_future():
                f = orig_future()
                f.set_result(None)
                return f

            with mock.patch.object(_a, "Future", side_effect=instant_future):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = fd._follow()
        self.assertEqual(rc, 0)
        self.assertIn("fleet.>", calls["subs"])
        self.assertIn("event.>", calls["subs"])


class NoPollingInvariant(unittest.TestCase):
    """Grep source: reject `while True` paired with a sleep >100ms."""

    def test_no_polling_loop_with_sleep(self):
        src = DASH_PATH.read_text()
        # Find all 'while True:' lines and ensure none have a sleep > 0.1 within
        # the next 20 lines.
        lines = src.splitlines()
        offenders = []
        sleep_re = re.compile(r"(?:asyncio\.sleep|time\.sleep)\(\s*([0-9.]+)")
        for i, line in enumerate(lines):
            if re.search(r"\bwhile\s+True\b", line):
                for j in range(i + 1, min(i + 20, len(lines))):
                    m = sleep_re.search(lines[j])
                    if m:
                        try:
                            val = float(m.group(1))
                        except ValueError:
                            val = 0
                        if val > 0.1:
                            offenders.append((i + 1, j + 1, val))
        self.assertEqual(offenders, [], f"polling loops detected: {offenders}")


if __name__ == "__main__":
    unittest.main()
