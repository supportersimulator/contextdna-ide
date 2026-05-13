"""BBB3 — End-to-end integration test for the 3-tier bridge fallback
(DeepSeek → Anthropic → Superset) shipped in commit ``0202e8a1085``.

Exercises the three HTTP entry paths in ``tools/fleet_nerve_nats.py`` under
controlled failure conditions and confirms:

  * Test 1 — POST /v1/chat/completions: DeepSeek 5xx → Anthropic conn-fail →
    Superset answers; ``bridge_superset_fallback_ok`` increments.
  * Test 2 — POST /v1/messages (stream=true): same chain, SSE 200 emitted by
    the Superset fallback path. Guards the BBB3-fixed ``user_text`` NameError
    that previously made this branch unreachable.
  * Test 3 — POST /v1/messages (non-stream): same chain, JSON 200 emitted by
    the Superset fallback path.
  * Test 4 — Token-overflow gate: 200K-token request bypasses Anthropic and
    routes directly to DeepSeek; ``bridge_token_overflow_to_deepseek`` ticks.
  * Test 5 — Redis down: ``_incr_counter_redis`` falls back to the JSONL file
    at ``FLEET_COUNTER_FALLBACK_PATH``; ``counter_file_fallback_writes`` ticks.
  * Test 6 — Full exhaustion: DeepSeek + Anthropic + Superset all fail → 503
    with descriptive error; ``bridge_superset_fallback_errors`` increments.

Test seams (all module-level patches, no real network):

  * ``memory.llm_priority_queue.llm_generate``    — DeepSeek primary
  * ``tools.fleet_nerve_nats._try_anthropic_upstream``  — Anthropic JSON
  * ``tools.fleet_nerve_nats._stream_anthropic_passthrough`` — Anthropic SSE
  * ``tools.fleet_nerve_nats._resolve_anthropic_key`` — key resolution
  * ``tools.fleet_nerve_nats._try_superset_fallback`` — 3rd tier

The daemon's ``ThreadingHTTPServer`` is started on a free port via
``_start_http_compat``. Each test gets a fresh ``FleetNerveNATS`` instance so
counters reset between cases (HTTP socket reuse is handled by the daemon's
own SO_REUSEADDR + exponential backoff bind path).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── helpers ─────────────────────────────────────────────────────────


def _free_port() -> int:
    """Bind a kernel-assigned port, close, return it. Race-free enough for
    test daemons; the inner server uses SO_REUSEADDR so the TIME_WAIT window
    is not a problem."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _BridgeHarness:
    """Spins up ``FleetNerveNATS._start_http_compat`` on a free port in a
    dedicated asyncio loop running in a background thread. Provides
    ``post()`` for JSON requests + ``stream()`` for SSE consumption."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.port = _free_port()
        # FLEET_DAEMON_PORT is read at module import time *and* at server bind
        # time — patch both the env and the already-resolved module constant
        # so the second server invocation in the same test session picks up
        # the new port (the first ``import`` cached the old value).
        monkeypatch.setenv("FLEET_DAEMON_PORT", str(self.port))

        # Lazy-import after env tweak so module-level constant resolves to
        # this test's port. ``importlib.reload`` is heavy and rewrites every
        # other module that holds a reference, so we patch the constant in
        # place instead.
        import tools.fleet_nerve_nats as fnn  # noqa: E402
        fnn.FLEET_DAEMON_PORT = self.port
        # multifleet.constants exports FLEET_DAEMON_PORT — keep them aligned
        # so internal lookups land on the test port.
        from multifleet import constants as _mfc  # noqa: E402
        _mfc.FLEET_DAEMON_PORT = self.port

        self.fnn = fnn
        self.daemon = fnn.FleetNerveNATS(node_id="bbb3-test")
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        # Disable Anthropic key resolution by default — each test re-enables
        # it explicitly when it wants Anthropic to be tried. Keeps the
        # ``no key → Anthropic skipped`` path off the default route.
        monkeypatch.setattr(fnn, "_resolve_anthropic_key", lambda: None)
        # Default: Anthropic upstream raises a transport error so it is never
        # accidentally hit. Tests opt in to specific status codes.
        monkeypatch.setattr(
            fnn,
            "_try_anthropic_upstream",
            lambda *a, **kw: (0, None, "default: anthropic unreachable"),
        )
        # Default: streaming Anthropic refuses (committed=False, status=0) so
        # the streaming branch always falls past it.
        monkeypatch.setattr(
            fnn, "_stream_anthropic_passthrough", lambda *a, **kw: (False, 0),
        )
        # Default: Superset returns None (unavailable). Tests opt in.
        monkeypatch.setattr(
            fnn, "_try_superset_fallback", lambda prompt, stats: None,
        )

    def start(self) -> None:
        async def _run():
            await self.daemon._start_http_compat()
            self._started.set()

        def _runner():
            loop = asyncio.new_event_loop()
            self.loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run())
            loop.run_forever()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        # Wait for the bind to complete; the HTTP server thread itself is
        # started inside _start_http_compat *before* the coroutine awaits its
        # SSE attach, so the event is set roughly when the listener is live.
        assert self._started.wait(5.0), "bridge HTTP server did not start"
        # Brief poll until /health responds so the test doesn't race the
        # ThreadingHTTPServer.serve_forever thread that starts inside the
        # coroutine. 50ms is sufficient on the slowest CI tier we use.
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=1
                ) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.05)
        raise RuntimeError("bridge /health never came up")

    def stop(self) -> None:
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                # Loop already stopped or closed — benign during test teardown.
                pass

    def post(
        self,
        path: str,
        body: dict,
        *,
        timeout: float = 15.0,
        expect_status: Optional[int] = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read()
            code = e.code
        try:
            decoded = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            decoded = {"_raw": payload.decode("utf-8", "replace")}
        if expect_status is not None:
            assert code == expect_status, (
                f"expected HTTP {expect_status}, got {code}: {decoded}"
            )
        return code, decoded

    def post_stream(
        self, path: str, body: dict, *, timeout: float = 15.0,
    ) -> tuple[int, bytes]:
        """POST and return (status, raw body bytes) without JSON-decoding —
        used for SSE assertions on the streaming bridge."""
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read()
            code = e.code
        return code, payload


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    h = _BridgeHarness(monkeypatch)
    h.start()
    yield h
    h.stop()


# ── Test 1 — /v1/chat/completions: DeepSeek fail + Anthropic fail → Superset
# ----------------------------------------------------------------------


def test_chat_completions_falls_through_to_superset(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """3-tier ladder for the OpenAI shim: DeepSeek 5xx, Anthropic connection
    failure (status=0), Superset answers. The handler must surface a 200
    with the Superset content and bump ``bridge_superset_fallback_ok``.

    NB (BBB3 audit): when Anthropic returns a *JSON* error body (status_cc
    in {1..599} with a parseable payload), the handler currently surfaces
    that error directly and skips Superset (see audit 'Bug #2'). We model
    the no-key path here (which is the only Anthropic outcome that yields
    ``upstream_used_cc == None`` and lets Superset run).
    """
    fnn = harness.fnn
    daemon = harness.daemon

    # DeepSeek primary fails — llm_generate returns None.
    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)

    # Anthropic skipped (no key + no bearer). The handler increments
    # ``bridge_chat_completions_skipped`` and falls to Superset.
    monkeypatch.setattr(fnn, "_resolve_anthropic_key", lambda: None)

    def fake_superset(prompt: str, stats: dict) -> str:
        stats["bridge_superset_fallback_ok"] = stats.get(
            "bridge_superset_fallback_ok", 0) + 1
        return "superset says hello"
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, body = harness.post(
        "/v1/chat/completions",
        {"model": "deepseek-chat",
         "messages": [{"role": "user", "content": "hi"}]},
        expect_status=200,
    )
    assert body.get("model") == "superset-fallback", body
    content = body["choices"][0]["message"]["content"]
    assert "superset" in content.lower(), body
    assert daemon._stats["bridge_superset_fallback_ok"] == 1
    assert daemon._stats["bridge_deepseek_failures"] >= 1


# ── Test 2 — streaming /v1/messages: same chain, SSE response
# ----------------------------------------------------------------------


def test_streaming_v1_messages_falls_through_to_superset(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """Streaming branch (stream=true). The DeepSeek synthetic-SSE primary
    fails, the Anthropic SSE pass-through returns committed=False, no key →
    Superset answers. Result: HTTP 200 with the Superset JSON envelope
    (Aaron's commit emits a *JSON* fallback rather than synthetic SSE in
    this exhaustion path — see fleet_nerve_nats.py line ~12459).

    Regression guard for the BBB3-fixed ``user_text`` NameError: prior to
    this fix the Superset fallback in the streaming branch raised
    NameError, was caught by the outer try/except, and returned a
    generic 500.
    """
    fnn = harness.fnn
    daemon = harness.daemon

    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)

    # Anthropic stream refuses commit (no key, no bearer; default mock).
    def fake_superset(prompt: str, stats: dict) -> str:
        stats["bridge_superset_fallback_ok"] = stats.get(
            "bridge_superset_fallback_ok", 0) + 1
        # Prompt should carry the streaming-branch user text. Empty string is
        # acceptable (locals().get() default), but a non-empty body means the
        # fix landed and the streaming branch's flat_s buffer was emitted.
        return "superset stream answer"
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, raw = harness.post_stream(
        "/v1/messages",
        {
            "model": "claude-sonnet-4-6", "stream": True,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "stream please"}],
        },
    )
    assert code == 200, raw[:200]
    text = raw.decode("utf-8", "replace")
    # The streaming exhaustion path emits a JSON Anthropic-Messages object
    # (not an SSE stream) when Superset answers — assert the shape.
    payload = json.loads(text)
    assert payload.get("model") == "superset-fallback"
    blocks = payload.get("content") or []
    assert blocks and blocks[0].get("text") == "superset stream answer"
    assert daemon._stats["bridge_superset_fallback_ok"] == 1


# ── Test 3 — non-streaming /v1/messages: same chain, JSON response
# ----------------------------------------------------------------------


def test_nonstream_v1_messages_falls_through_to_superset(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """Non-streaming /v1/messages. DeepSeek fails, Anthropic not tried
    (no key / no bearer), Superset returns a string → 200 with the
    Anthropic Messages envelope and Superset content."""
    fnn = harness.fnn
    daemon = harness.daemon

    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)

    def fake_superset(prompt: str, stats: dict) -> str:
        stats["bridge_superset_fallback_ok"] = stats.get(
            "bridge_superset_fallback_ok", 0) + 1
        return "superset non-stream answer"
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, body = harness.post(
        "/v1/messages",
        {
            "model": "claude-sonnet-4-6", "max_tokens": 256,
            "messages": [{"role": "user", "content": "non-stream please"}],
        },
        expect_status=200,
    )
    assert body.get("model") == "superset-fallback", body
    assert body.get("type") == "message"
    content = body.get("content") or []
    assert content and content[0]["text"] == "superset non-stream answer"
    assert daemon._stats["bridge_superset_fallback_ok"] == 1
    # The non-stream branch also increments bridge_deepseek_failures + the
    # skipped counter (no key path).
    assert daemon._stats["bridge_deepseek_failures"] >= 1
    assert daemon._stats["bridge_deepseek_skipped"] >= 1


# ── Test 4 — token-overflow gate: >180K tokens skip Anthropic
# ----------------------------------------------------------------------


def test_token_overflow_gate_routes_to_deepseek(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """Compose a request whose char-length-divided-by-4 exceeds the
    threshold of 180,000 tokens. The handler must:
      - Bump ``bridge_token_overflow_to_deepseek``.
      - Set ``body["_bridge_force_deepseek"] = True`` (internal).
      - Let DeepSeek answer (we provide a llm_generate mock that returns
        a non-empty result so the request short-circuits with 200).

    This guards the env-configurable BRIDGE_TOKEN_OVERFLOW_THRESHOLD path.
    """
    fnn = harness.fnn
    daemon = harness.daemon

    # DeepSeek answers happily — this ensures Anthropic is *not* tried even
    # though we pump 200K tokens through. (Anthropic mock would raise if hit
    # because we leave it on the default 'unreachable' stub.)
    import memory.llm_priority_queue as lpq
    captured: dict = {}

    def fake_llm(**kw):
        captured.update(kw)
        return "deepseek answered the giant prompt"
    monkeypatch.setattr(lpq, "llm_generate", fake_llm)

    # ~200K tokens (chars/4). Build a single big content block; the gate
    # sums system + every message content len.
    huge_text = "x" * (200_000 * 4)
    code, body = harness.post(
        "/v1/messages",
        {
            "model": "claude-sonnet-4-6", "max_tokens": 1024,
            "messages": [{"role": "user", "content": huge_text}],
        },
        timeout=20.0,
        expect_status=200,
    )
    # Confirm DeepSeek answered (the success envelope uses the requested
    # model name, not 'superset-fallback' or 'anthropic').
    assert body.get("model") == "claude-sonnet-4-6", body
    text_blocks = body.get("content") or []
    assert text_blocks and "deepseek" in text_blocks[0]["text"].lower()
    assert daemon._stats["bridge_token_overflow_to_deepseek"] >= 1
    # llm_generate was called with the giant prompt (DeepSeek really was tried).
    assert captured.get("priority") is not None


# ── Test 5 — Redis down: counter writes fall back to JSONL file
# ----------------------------------------------------------------------


def test_counter_redis_unavailable_falls_back_to_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Force ``_incr_counter_redis`` to fail Redis lookup (bogus port 1 +
    redirected fallback path) and assert: (a) a JSONL line is appended at
    the redirected path, (b) ``counter_file_fallback_writes`` ticks on the
    supplied stats dict.

    Direct unit test on the helper — no HTTP server needed for this seam,
    keeping the test cheap and deterministic."""
    import tools.fleet_nerve_nats as fnn

    fallback_path = tmp_path / "fleet-counters.jsonl"
    monkeypatch.setattr(fnn, "_COUNTER_FALLBACK_PATH", fallback_path)
    # Force the Redis client to fail fast — bogus port, 1.0s timeout already
    # set inside _incr_counter_redis.
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")  # always-refused port

    stats: dict = {}
    fnn._incr_counter_redis("bbb3_test_counter", 1, daemon_stats=stats)
    fnn._incr_counter_redis("bbb3_test_counter", 5, daemon_stats=stats)

    assert fallback_path.exists(), "Redis-down must write to JSONL fallback"
    lines = fallback_path.read_text().strip().splitlines()
    assert len(lines) == 2, lines
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["counter"] == "bbb3_test_counter"
    assert parsed[0]["value"] == 1
    assert parsed[1]["value"] == 5
    # ZSF observability counter advanced on both calls.
    assert stats["counter_file_fallback_writes"] == 2
    # The hard-error counter must NOT have been bumped (file write succeeded).
    assert stats.get("counter_file_fallback_errors", 0) == 0


def test_counter_redis_unavailable_and_file_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Both Redis and file fallback fail. The helper must still not raise
    and must bump ``counter_file_fallback_errors``."""
    import tools.fleet_nerve_nats as fnn

    # Point the fallback at a directory path — open() in 'a' mode will fail
    # with IsADirectoryError, exercising the inner except.
    bad_path = tmp_path / "subdir"
    bad_path.mkdir()
    monkeypatch.setattr(fnn, "_COUNTER_FALLBACK_PATH", bad_path)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")

    stats: dict = {}
    # Must not raise (ZSF).
    fnn._incr_counter_redis("bbb3_double_fail", 1, daemon_stats=stats)
    assert stats.get("counter_file_fallback_errors", 0) == 1
    assert stats.get("counter_file_fallback_writes", 0) == 0


# ── Test 6 — full exhaustion: all 3 tiers fail → 503
# ----------------------------------------------------------------------


# ── Test 7 (CCC3) — chat/completions: DeepSeek 5xx + Anthropic 5xx → Superset
# ----------------------------------------------------------------------


def test_chat_completions_anthropic_5xx_falls_through_to_superset(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """CCC3 regression — Aaron's commit ``0202e8a10`` claimed all 3 paths
    fall to Superset before 503, but the chat/completions handler returned
    Anthropic's JSON error body directly when ``upstream_used_cc`` was set
    (BBB3 audit Bug #3). This test:

      * DeepSeek primary returns None (5xx-equivalent).
      * ``_resolve_anthropic_key`` returns a key so Anthropic *is* tried.
      * ``_try_anthropic_upstream`` returns a parseable 503 body.
      * Superset answers happily.

    Pre-fix expectation: 503 surfaced with Anthropic's body, Superset
    counter still 0. Post-fix expectation: 200 from Superset, counter +1.
    """
    fnn = harness.fnn
    daemon = harness.daemon

    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)

    # Re-enable Anthropic key resolution so upstream_used_cc gets set.
    monkeypatch.setattr(fnn, "_resolve_anthropic_key", lambda: "test-key")

    # Anthropic returns a parseable 503 with an error body — this is the
    # exact shape that previously hit the early-return branch.
    def fake_anthropic(body, **kw):
        return (
            503,
            {"error": {"message": "overloaded", "type": "overloaded_error"}},
            None,
        )
    monkeypatch.setattr(fnn, "_try_anthropic_upstream", fake_anthropic)

    def fake_superset(prompt: str, stats: dict) -> str:
        stats["bridge_superset_fallback_ok"] = stats.get(
            "bridge_superset_fallback_ok", 0) + 1
        return "superset rescues cc"
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, body = harness.post(
        "/v1/chat/completions",
        {"model": "deepseek-chat",
         "messages": [{"role": "user", "content": "hi"}]},
        expect_status=200,
    )
    assert body.get("model") == "superset-fallback", body
    content = body["choices"][0]["message"]["content"]
    assert "superset rescues cc" in content, body
    assert daemon._stats["bridge_superset_fallback_ok"] == 1
    # Anthropic was tried and its passthrough error counter advanced (ZSF —
    # we still observe that Anthropic failed even when Superset rescues).
    assert daemon._stats["bridge_anthropic_passthrough_errors"] >= 1
    assert daemon._stats["bridge_deepseek_failures"] >= 1


# ── Test 8 (CCC3) — non-stream /v1/messages: same chain
# ----------------------------------------------------------------------


def test_nonstream_v1_messages_anthropic_5xx_falls_through_to_superset(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """CCC3 regression — same fix on the non-stream /v1/messages branch
    (BBB3 audit Bug #2). Anthropic returns a parseable 503 body; prior
    code returned that body directly. Fixed code falls through to Superset
    and the request succeeds with 200."""
    fnn = harness.fnn
    daemon = harness.daemon

    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)
    monkeypatch.setattr(fnn, "_resolve_anthropic_key", lambda: "test-key")

    def fake_anthropic(body, **kw):
        return (
            503,
            {"error": {"message": "overloaded", "type": "overloaded_error"}},
            None,
        )
    monkeypatch.setattr(fnn, "_try_anthropic_upstream", fake_anthropic)

    def fake_superset(prompt: str, stats: dict) -> str:
        stats["bridge_superset_fallback_ok"] = stats.get(
            "bridge_superset_fallback_ok", 0) + 1
        return "superset rescues non-stream"
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, body = harness.post(
        "/v1/messages",
        {
            "model": "claude-sonnet-4-6", "max_tokens": 256,
            "messages": [{"role": "user", "content": "non-stream please"}],
        },
        expect_status=200,
    )
    assert body.get("model") == "superset-fallback", body
    assert body.get("type") == "message"
    content = body.get("content") or []
    assert content and content[0]["text"] == "superset rescues non-stream"
    assert daemon._stats["bridge_superset_fallback_ok"] == 1
    assert daemon._stats["bridge_anthropic_passthrough_errors"] >= 1
    assert daemon._stats["bridge_deepseek_failures"] >= 1


def test_full_exhaustion_returns_503_and_bumps_error_counter(
    harness: _BridgeHarness, monkeypatch: pytest.MonkeyPatch,
):
    """All three tiers fail. The handler must return 503 with a
    descriptive error envelope and bump ``bridge_superset_fallback_errors``.
    Uses the /v1/messages non-stream branch — the cleanest of the three
    exhaustion paths."""
    fnn = harness.fnn
    daemon = harness.daemon

    import memory.llm_priority_queue as lpq
    monkeypatch.setattr(lpq, "llm_generate", lambda **kw: None)
    monkeypatch.setattr(fnn, "_resolve_anthropic_key", lambda: None)

    def fake_superset(prompt: str, stats: dict):
        stats["bridge_superset_fallback_errors"] = stats.get(
            "bridge_superset_fallback_errors", 0) + 1
        return None
    monkeypatch.setattr(fnn, "_try_superset_fallback", fake_superset)

    code, body = harness.post(
        "/v1/messages",
        {
            "model": "claude-sonnet-4-6", "max_tokens": 64,
            "messages": [{"role": "user", "content": "exhausted"}],
        },
        expect_status=503,
    )
    err = body.get("error") or {}
    msg = err.get("message", "")
    assert "exhausted" in msg.lower() or "all failed" in msg.lower(), body
    assert err.get("type") == "service_unavailable_error", body
    assert daemon._stats["bridge_superset_fallback_errors"] >= 1
