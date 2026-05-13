"""RACE Q5 — extended dashboard sections.

Covers (one test class per concern, no shared fixtures so failures point at
the right blast radius):

    1. Webhook section renders age + p50/p95 + cache hit rate.
    2. Webhook section flags failing sections in red.
    3. Webhook section degrades gracefully when bridge is offline.
    4. JetStream replica view computes ``missing`` from peers - replica_set.
    5. JetStream replica view marks gaps in red.
    6. 3-surgeons probe section reflects rc=0 / rc=1 / missing-binary.
    7. Cloud listener: parses launchctl PID; flags PID=0 as warn; skips when
       launchctl rc != 0 (non-cloud nodes).
    8. ``--follow`` mode subscribes to ``event.>`` (which covers
       ``event.webhook.completed.>``) and ``fleet.>``, with no polling loop.
    9. JSON mode includes the new top-level keys.

Tests use the same module-loader trick as ``test_fleet_dashboard.py`` because
the dashboard file is named with a hyphen and isn't importable by name.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASH_PATH = REPO_ROOT / "scripts" / "fleet-dashboard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fleet_dashboard_q5", DASH_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Force plain text rendering so substring assertions are robust.
    mod._RICH = False
    mod._CONSOLE = None
    return mod


# ── Test fixtures ────────────────────────────────────────────────────────

WEBHOOK_HEALTHY = {
    "active": True,
    "events_recorded": 12,
    "receive_errors": 0,
    "last_webhook_age_s": 4,
    "last_total_ms": 318,
    "p50_total_ms": 290,
    "p95_total_ms": 412,
    "missing_sections_recent": [],
    "section_health": {
        "section_0": {"avg_ms": 56.0, "fail_rate": 0.0, "samples": 12},
        "section_1": {"avg_ms": 89.0, "fail_rate": 0.0, "samples": 12},
    },
    "cache_hit_rate": 0.75,
    "window_size": 10,
}

WEBHOOK_FAILING = {
    **WEBHOOK_HEALTHY,
    "section_health": {
        "section_3": {"avg_ms": 110.0, "fail_rate": 0.5, "samples": 10},
        "section_6": {"avg_ms": 95.0, "fail_rate": 0.2, "samples": 10},
        "section_0": {"avg_ms": 56.0, "fail_rate": 0.0, "samples": 10},
    },
    "missing_sections_recent": [{"section": "section_3", "count": 5}],
}

HEALTH_WITH_REPLICAS = {
    "nodeId": "macX",
    "uptime_s": 100,
    "activeSessions": 0,
    "peers": {"mac1": {"lastSeen": 2}, "mac2": {"lastSeen": 5}},
    "delegation": {"self_node": "macX"},
    "channel_reliability": {},
    "channel_health": {},
    "rate_limits": {},
    # RACE N3 — daemon-side jetstream_health surface
    "jetstream_health": {
        "status": "degraded",
        "streams": {
            "FLEET_MESSAGES": {
                "status": "ok",
                "replicas": 3,
                "leader": "mac1",
                "followers": [
                    {"name": "mac2", "current": True, "lag": 0},
                    {"name": "macX", "current": True, "lag": 0},
                ],
                "max_follower_lag": 0,
                "messages": 1000,
                "bytes": 50000,
            },
            "FLEET_EVENTS": {
                "status": "ok",
                "replicas": 3,
                "leader": "mac1",
                # Only one follower visible — mac2 missing from the set
                "followers": [{"name": "macX", "current": True, "lag": 12}],
                "max_follower_lag": 12,
                "messages": 200,
                "bytes": 8000,
            },
        },
    },
    "webhook": WEBHOOK_HEALTHY,
}

JSZ_FOR_REPLICAS = {
    "meta_cluster": {"leader": "mac1", "cluster_size": 3},
    "account_details": [
        {
            "stream_detail": [
                {
                    "name": "FLEET_MESSAGES",
                    "config": {"num_replicas": 3, "max_age": 0, "retention": "limits"},
                    "state": {"messages": 1000, "bytes": 50000},
                },
                {
                    "name": "FLEET_EVENTS",
                    "config": {"num_replicas": 3, "max_age": 0, "retention": "limits"},
                    "state": {"messages": 200, "bytes": 8000},
                },
            ]
        }
    ],
}


def _make_http(health: dict) -> object:
    def _http(url, timeout=2.0):
        if "8855/health" in url:
            return health
        if "jsz" in url:
            return JSZ_FOR_REPLICAS
        if "routez" in url:
            return {"num_routes": 0, "routes": []}
        if "varz" in url:
            return {"cluster": {"name": "contextdna"}}
        return {}

    return _http


# ── 1-3: Webhook section ────────────────────────────────────────────────


class WebhookSection(unittest.TestCase):
    def test_renders_age_p50_p95_cache(self):
        fd = _load_module()
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_webhook(WEBHOOK_HEALTHY)
        out = buf.getvalue()
        self.assertIn("Webhook health", out)
        self.assertIn("p50=290ms", out)
        self.assertIn("p95=412ms", out)
        self.assertIn("cache_hit=75%", out)
        self.assertIn("events=12", out)
        # Healthy section MUST stay ≤ 6 lines.
        self.assertLessEqual(len([l for l in out.splitlines() if l.strip()]), 6)

    def test_flags_failing_sections(self):
        fd = _load_module()
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_webhook(WEBHOOK_FAILING)
        out = buf.getvalue()
        self.assertIn("failing", out)
        # Worst offender first.
        self.assertIn("section_3=50%", out)
        # Recent missing sections surface with count.
        self.assertIn("recent_missing", out)
        self.assertIn("section_3x5", out)

    def test_offline_when_bridge_dead(self):
        fd = _load_module()
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_webhook({"active": False, "reason": "bridge-init-failed"})
        out = buf.getvalue()
        self.assertIn("offline", out.lower())
        self.assertIn("bridge-init-failed", out)


# ── 4-5: JetStream replica view ─────────────────────────────────────────


class JetStreamReplicas(unittest.TestCase):
    def test_missing_computed_from_peers_minus_set(self):
        fd = _load_module()
        view = fd._build_replica_view(
            HEALTH_WITH_REPLICAS,
            [
                {"name": "FLEET_MESSAGES", "replicas": 3, "messages": 1000, "bytes": 50000},
                {"name": "FLEET_EVENTS", "replicas": 3, "messages": 200, "bytes": 8000},
            ],
        )
        by_name = {s["name"]: s for s in view}
        # FLEET_MESSAGES has all three peers — no gap.
        self.assertEqual(by_name["FLEET_MESSAGES"]["missing"], [])
        self.assertEqual(by_name["FLEET_MESSAGES"]["status"], "ok")
        # FLEET_EVENTS: replica_set = {mac1, macX}; expected = {mac1, mac2, macX}
        self.assertEqual(by_name["FLEET_EVENTS"]["missing"], ["mac2"])
        self.assertEqual(by_name["FLEET_EVENTS"]["status"], "degraded")
        self.assertEqual(by_name["FLEET_EVENTS"]["max_follower_lag"], 12)

    def test_renders_red_marker_for_gap(self):
        fd = _load_module()
        view = fd._build_replica_view(
            HEALTH_WITH_REPLICAS,
            [
                {"name": "FLEET_EVENTS", "replicas": 3, "messages": 200, "bytes": 8000},
            ],
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_jetstream_replicas(view)
        out = buf.getvalue()
        self.assertIn("FLEET_EVENTS", out)
        self.assertIn("missing=mac2", out)
        # Plain-text mode keeps the rich tags so test asserts they're present.
        self.assertIn("[red]", out)
        # Stays terminal-friendly (≤ 6 lines including header).
        self.assertLessEqual(len([l for l in out.splitlines() if l.strip()]), 6)


# ── 6: 3-surgeons probe ─────────────────────────────────────────────────


class ThreeSurgeonsProbe(unittest.TestCase):
    def test_ok_when_rc_zero(self):
        fd = _load_module()
        result = fd._collect_3s_probe(runner=lambda cmd: (0, "all surgeons green"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rc"], 0)

    def test_degraded_when_rc_nonzero(self):
        fd = _load_module()
        result = fd._collect_3s_probe(runner=lambda cmd: (1, "neurologist offline"))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["rc"], 1)
        self.assertIn("neurologist offline", result["raw"])

    def test_skip_when_binary_missing(self):
        fd = _load_module()
        with mock.patch.object(fd, "shutil") as msh:
            msh.which.return_value = None
            result = fd._collect_3s_probe(runner=None)
        self.assertEqual(result["status"], "skip")

    def test_render_includes_summary(self):
        fd = _load_module()
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_three_surgeons(
                {"status": "ok", "rc": 0, "summary": "all 3 surgeons reachable", "raw": ""}
            )
        out = buf.getvalue()
        self.assertIn("3-surgeons probe", out)
        self.assertIn("OK", out)
        self.assertIn("all 3 surgeons reachable", out)


# ── 7: Cloud listener ───────────────────────────────────────────────────


class CloudListener(unittest.TestCase):
    LAUNCHCTL_RUNNING = (
        '{\n\t"PID" = 12345;\n\t"Label" = "io.contextdna.cloud-inbox-listener";\n}\n'
    )
    LAUNCHCTL_STOPPED = (
        '{\n\t"PID" = 0;\n\t"Label" = "io.contextdna.cloud-inbox-listener";\n}\n'
    )

    def test_parses_running_pid(self):
        fd = _load_module()
        result = fd._collect_cloud_listener(
            runner=lambda cmd: (0, self.LAUNCHCTL_RUNNING),
            health={"cloud_listener": {"last_event_ts": 0}},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pid"], 12345)

    def test_warns_on_pid_zero(self):
        fd = _load_module()
        result = fd._collect_cloud_listener(
            runner=lambda cmd: (0, self.LAUNCHCTL_STOPPED),
        )
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["pid"], 0)

    def test_skip_when_not_loaded(self):
        fd = _load_module()
        result = fd._collect_cloud_listener(runner=lambda cmd: (113, ""))
        self.assertEqual(result["status"], "skip")
        self.assertIsNone(result["pid"])

    def test_last_event_age_from_health(self):
        fd = _load_module()
        import time as _t

        now = _t.time()
        result = fd._collect_cloud_listener(
            runner=lambda cmd: (0, self.LAUNCHCTL_RUNNING),
            health={"cloud_listener": {"last_event_ts": now - 30}},
        )
        self.assertIsNotNone(result["last_event_age_s"])
        self.assertGreaterEqual(result["last_event_age_s"], 30)

    def test_render_ok_path(self):
        fd = _load_module()
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd._render_cloud_listener(
                {"status": "ok", "pid": 12345, "label": "x", "last_event_age_s": 5,
                 "summary": "x running"}
            )
        out = buf.getvalue()
        self.assertIn("Cloud listener", out)
        self.assertIn("OK", out)
        self.assertIn("pid=12345", out)


# ── 8: --follow mode subjects (incl. webhook completed wildcard) ───────


class FollowSubjectsExtended(unittest.TestCase):
    def test_subscribes_event_and_fleet_wildcards(self):
        """`event.>` already covers `event.webhook.completed.>`. We assert
        the dashboard subscribes to the wildcard and that its handler is
        able to enrich webhook completion events when they arrive.
        """
        fd = _load_module()

        class RecClient:
            def __init__(self):
                self.subs = []

            async def connect(self, **kw):
                return None

            async def subscribe(self, subject, cb):
                self.subs.append(subject)

            async def drain(self):
                return None

        rec = RecClient()
        fake_mod = mock.MagicMock()
        fake_mod.Client = lambda: rec
        with mock.patch.dict(
            sys.modules,
            {"nats": mock.MagicMock(), "nats.aio": mock.MagicMock(),
             "nats.aio.client": fake_mod},
        ):
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
        self.assertIn("fleet.>", rec.subs)
        self.assertIn("event.>", rec.subs)
        # The startup banner advertises the webhook completion wildcard so
        # operators know the dashboard sees those live without polling.
        self.assertIn("event.webhook.completed.", buf.getvalue())


# ── 9: JSON mode includes new top-level keys ───────────────────────────


class JsonModeExtended(unittest.TestCase):
    def test_json_payload_has_q5_keys(self):
        fd = _load_module()
        with mock.patch.object(fd, "_http_json", side_effect=_make_http(HEALTH_WITH_REPLICAS)), \
             mock.patch.object(fd, "_last_fleet_messages", return_value=[]), \
             mock.patch.object(fd, "_collect_3s_probe",
                               return_value={"status": "ok", "rc": 0,
                                             "summary": "ok", "raw": ""}), \
             mock.patch.object(fd, "_collect_cloud_listener",
                               return_value={"status": "skip", "pid": None,
                                             "label": "x", "last_event_age_s": None,
                                             "summary": "skipped"}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = fd.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        for key in ("webhook", "stream_replicas", "three_surgeons", "cloud_listener"):
            self.assertIn(key, payload)
        # Replica view computed missing for FLEET_EVENTS.
        events = next(s for s in payload["stream_replicas"] if s["name"] == "FLEET_EVENTS")
        self.assertEqual(events["missing"], ["mac2"])


# ── Print-once headers regression — Q5 sections all appear ─────────────


class PrintOnceHeadersExtended(unittest.TestCase):
    def test_all_q5_section_headers_present(self):
        fd = _load_module()
        with mock.patch.object(fd, "_http_json", side_effect=_make_http(HEALTH_WITH_REPLICAS)), \
             mock.patch.object(fd, "_last_fleet_messages", return_value=[]), \
             mock.patch.object(fd, "_collect_3s_probe",
                               return_value={"status": "ok", "rc": 0,
                                             "summary": "ok", "raw": ""}), \
             mock.patch.object(fd, "_collect_cloud_listener",
                               return_value={"status": "skip", "pid": None,
                                             "label": "x", "last_event_age_s": None,
                                             "summary": "skipped"}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = fd.main([])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        for header in (
            "Webhook health",
            "JetStream replicas",
            "3-surgeons probe",
            "Cloud listener",
        ):
            self.assertIn(header, out)


if __name__ == "__main__":
    unittest.main()
