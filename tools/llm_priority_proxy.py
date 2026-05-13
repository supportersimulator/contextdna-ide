#!/usr/bin/env python3
"""
LLM Priority Proxy — Universal no-stampede proxy on port 5045.

Enforces the no-stampede rule structurally on EVERY mac, regardless of whether
a local LLM is currently running. All nodes point 3s neurologist (and any other
LLM consumer) at http://localhost:5045/v1 instead of raw ports 5044/11434.

Chief awareness:
  - mac1: writes proxy telemetry to Redis (proxy:call_log, proxy:stats)
  - mac2/mac3: POST llm_proxy_event packets to mac1 chief ingest (port 8844)
    → chief_synthesis.py and fleet dashboard see all cross-node LLM activity

Modes:
  - mac1: routes through llm_priority_queue.py (Redis-backed, full priority P1-P4)
  - mac2/mac3: threading.Lock serialization, auto-detects backend:
      1. Ollama (port 11434) or MLX (port 5044) — whichever is present
      2. External API fallback (EXTERNAL_LLM_ENDPOINT + EXTERNAL_LLM_API_KEY_ENV)
         Ensures DeepSeek/OpenAI calls still go through the priority lock and
         emit telemetry to ContextDNA chief, never direct-to-API.

Priority mapping via X-Priority header:
  1 = AARON (highest), 2 = ATLAS, 3 = EXTERNAL, 4 = BACKGROUND (default: 2)

External API env vars (mac2/mac3 — set in ~/.zshrc):
  EXTERNAL_LLM_ENDPOINT   e.g. https://api.deepseek.com/v1
  EXTERNAL_LLM_API_KEY_ENV  env var NAME containing the key, e.g. Context_DNA_Deepseek
  EXTERNAL_LLM_MODEL      e.g. deepseek-chat

Usage:
  python3 tools/llm_priority_proxy.py
  LLM_PROXY_PORT=5045 MULTIFLEET_NODE_ID=mac2 python3 tools/llm_priority_proxy.py

LaunchAgent: ~/Library/LaunchAgents/io.contextdna.llm-proxy.plist
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import requests

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

PORT = int(os.environ.get("LLM_PROXY_PORT", "5045"))
CHIEF_INGEST_URL = os.environ.get("CHIEF_INGEST_URL", "http://chief.local:8844/packets")
PID_FILE = os.environ.get("LLM_PROXY_PID_FILE", "/tmp/llm-proxy.pid")
FLEET_ID = "contextdna-main"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [llm-proxy/%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm_proxy")


def _detect_node_id() -> str:
    """Resolve node ID via ``multifleet.fleet_config`` (env > config > hostname).

    OSS adopters whose nodes aren't named mac1/mac2/mac3 only need to populate
    ``.multifleet/config.json`` — no code edit required. Falls back to the
    raw hostname when the fleet_config package isn't importable.
    """
    import socket
    try:
        # REPO_ROOT was already put on sys.path above; tools/ scripts run from
        # outside the installed package in several bootstrap paths.
        sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))
        from multifleet.fleet_config import get_node_id  # type: ignore
        return get_node_id()
    except Exception:
        return socket.gethostname().lower().split(".")[0]


def _chief_node_id() -> Optional[str]:
    """Resolve the chief node ID from config. Cached so the hot path is cheap."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))
        from multifleet.fleet_config import get_chief_id  # type: ignore
        return get_chief_id()
    except Exception:
        return None


NODE_ID = os.environ.get("MULTIFLEET_NODE_ID") or _detect_node_id()
_CHIEF_ID = _chief_node_id()

# ── Mac1 priority queue mode ──
_USE_PRIORITY_QUEUE = False
_pq_generate = None
_Priority = None

try:
    from memory.llm_priority_queue import llm_generate as _pq_fn, Priority as _PriorityEnum
    # Verify Redis is actually reachable — import succeeds on all nodes (repo present),
    # but priority_queue mode is only valid when Redis is running (mac1 only).
    import redis as _redis_check
    _redis_check.Redis(host="127.0.0.1", port=6379, socket_timeout=1).ping()
    _pq_generate = _pq_fn
    _Priority = _PriorityEnum
    _USE_PRIORITY_QUEUE = True
    logger.info("Mode: priority_queue (mac1 Redis-backed)")
except Exception:
    logger.info("Mode: worker_lock (threading.Lock serialization)")

# ── Worker serialization lock ──
_worker_lock = threading.Lock()

# ── Stats (in-process; Redis mirrors on mac1) ──
_stats: dict = {"total": 0, "by_priority": {}, "errors": 0, "backend": None}
_stats_lock = threading.Lock()


# ── Circuit Breaker ──

class CircuitBreaker:
    """Per-backend circuit breaker to prevent retry storms.

    States: CLOSED (normal) → OPEN (blocking) → HALF_OPEN (probe one request).
    After `failure_threshold` consecutive failures, circuit opens for `cooldown_s`.
    First request after cooldown is allowed (half-open); success closes, failure re-opens.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        # backend_key → {state, consecutive_failures, opened_at}
        self._backends: dict[str, dict] = {}

    def _get(self, key: str) -> dict:
        if key not in self._backends:
            self._backends[key] = {
                "state": self.CLOSED,
                "consecutive_failures": 0,
                "opened_at": 0.0,
            }
        return self._backends[key]

    def allow(self, backend_key: str) -> bool:
        """Return True if requests to this backend are allowed."""
        with self._lock:
            b = self._get(backend_key)
            if b["state"] == self.CLOSED:
                return True
            if b["state"] == self.OPEN:
                if time.time() - b["opened_at"] >= self.cooldown_s:
                    b["state"] = self.HALF_OPEN
                    logger.info(f"Circuit HALF_OPEN for backend '{backend_key}' — allowing probe request")
                    return True
                return False
            # HALF_OPEN — allow the single probe request
            return True

    def record_success(self, backend_key: str):
        with self._lock:
            b = self._get(backend_key)
            if b["state"] != self.CLOSED:
                logger.info(f"Circuit CLOSED for backend '{backend_key}' — recovered after {b['consecutive_failures']} failures")
            b["state"] = self.CLOSED
            b["consecutive_failures"] = 0

    def record_failure(self, backend_key: str):
        with self._lock:
            b = self._get(backend_key)
            b["consecutive_failures"] += 1
            if b["state"] == self.HALF_OPEN:
                # Probe failed — re-open
                b["state"] = self.OPEN
                b["opened_at"] = time.time()
                logger.warning(f"Circuit re-OPENED for backend '{backend_key}' — probe failed (cooldown {self.cooldown_s}s)")
            elif b["consecutive_failures"] >= self.failure_threshold:
                b["state"] = self.OPEN
                b["opened_at"] = time.time()
                logger.warning(
                    f"Circuit OPENED for backend '{backend_key}' — "
                    f"{b['consecutive_failures']} consecutive failures (cooldown {self.cooldown_s}s)"
                )

    def status(self) -> dict:
        """Return breaker state for all tracked backends."""
        with self._lock:
            now = time.time()
            out = {}
            for key, b in self._backends.items():
                remaining = 0.0
                if b["state"] == self.OPEN:
                    remaining = max(0.0, self.cooldown_s - (now - b["opened_at"]))
                out[key] = {
                    "state": b["state"],
                    "consecutive_failures": b["consecutive_failures"],
                    "cooldown_remaining_s": round(remaining, 1),
                }
            return out


_circuit_breaker = CircuitBreaker(
    failure_threshold=int(os.environ.get("CB_FAILURE_THRESHOLD", "5")),
    cooldown_s=float(os.environ.get("CB_COOLDOWN_S", "60")),
)


# ── External API fallback (mac2/mac3 — when no local LLM is running) ──
_EXTERNAL_ENDPOINT = os.environ.get("EXTERNAL_LLM_ENDPOINT", "")
_EXTERNAL_KEY_ENV = os.environ.get("EXTERNAL_LLM_API_KEY_ENV", "")
_EXTERNAL_MODEL = os.environ.get("EXTERNAL_LLM_MODEL", "deepseek-chat")

# Sentinel value — signals "use external API path" instead of a local URL
_BACKEND_EXTERNAL = "external"


# ── Backend detection ──
_BACKEND_CANDIDATES = [
    ("ollama", "http://127.0.0.1:11434"),
    ("mlx", "http://127.0.0.1:5044"),
]


def _probe_backend(base_url: str) -> bool:
    try:
        r = requests.get(f"{base_url}/v1/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _detect_backend() -> Optional[str]:
    for name, url in _BACKEND_CANDIDATES:
        if _probe_backend(url):
            logger.info(f"Backend detected: {name} at {url}")
            return url
    # No local LLM — fall back to external API if configured
    if _EXTERNAL_ENDPOINT and _EXTERNAL_KEY_ENV:
        logger.info(f"No local LLM — using external API: {_EXTERNAL_ENDPOINT} (model: {_EXTERNAL_MODEL})")
        return _BACKEND_EXTERNAL
    logger.info("No local LLM backend detected and no external API configured")
    return None


_backend_url: Optional[str] = None if _USE_PRIORITY_QUEUE else _detect_backend()
_backend_detect_lock = threading.Lock()


def _get_or_refresh_backend() -> Optional[str]:
    global _backend_url
    if _backend_url == _BACKEND_EXTERNAL:
        return _backend_url  # external API is always "available"
    if _backend_url and _probe_backend(_backend_url):
        return _backend_url
    with _backend_detect_lock:
        _backend_url = _detect_backend()
    return _backend_url


# ── Telemetry ──

def _emit_telemetry(caller: str, priority: int, latency_ms: float, model: str, error: bool):
    """Non-blocking telemetry — writes to local Redis when we ARE the chief
    (so the chief owns the shared view), otherwise POSTs to the chief's ingest.

    Identity-agnostic: chief is resolved from ``.multifleet/config.json`` via
    ``fleet_config.get_chief_id()``, so an OSS fleet named laptop-1 / prod-east
    / etc. just works with no code change.
    """
    is_chief = (_CHIEF_ID is not None and NODE_ID == _CHIEF_ID)
    if is_chief:
        _emit_to_redis(caller, priority, latency_ms, model, error)
    else:
        _emit_to_chief(caller, priority, latency_ms, model, error)


def _emit_to_redis(caller: str, priority: int, latency_ms: float, model: str, error: bool):
    """mac1: write to Redis proxy:call_log (1hr TTL) and llm:proxy:requests (last 500) for dual visibility."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        now = time.time()
        entry = json.dumps({
            "nodeId": NODE_ID,
            "caller": caller,
            "priority": priority,
            "latency_ms": round(latency_ms, 1),
            "model": model,
            "error": error,
            "ts": int(now),
        })
        # Key 1: proxy:call_log — 1hr rolling window (new key, fleet dashboard)
        r.zadd("proxy:call_log", {entry: now})
        r.zremrangebyscore("proxy:call_log", "-inf", now - 3600)
        # Key 2: llm:proxy:requests — last 500 entries (mac2-compat, GET /v1/fleet/requests)
        r.zadd("llm:proxy:requests", {entry: now})
        r.zremrangebyrank("llm:proxy:requests", 0, -501)  # keep last 500
        # Stats hash
        r.hincrby("proxy:stats", "total", 1)
        r.hincrby("proxy:stats", f"p{priority}", 1)
        if error:
            r.hincrby("proxy:stats", "errors", 1)
    except Exception:
        pass  # telemetry is best-effort — never block LLM responses


def _emit_to_chief(caller: str, priority: int, latency_ms: float, model: str, error: bool):
    """Workers: POST llm_proxy_event packet to mac1 chief ingest (port 8844)."""
    if not CHIEF_INGEST_URL:
        return
    try:
        packet = {
            "type": "llm_proxy_event",
            "nodeId": NODE_ID,
            "fleetId": FLEET_ID,
            "caller": caller,
            "priority": priority,
            "latency_ms": round(latency_ms, 1),
            "model": model,
            "error": error,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        requests.post(CHIEF_INGEST_URL, json=packet, timeout=3)
    except Exception:
        pass  # chief offline — best-effort, don't block


# ── LLM call handlers ──

def _messages_to_prompts(messages: list) -> tuple[str, str]:
    """Convert OpenAI messages array to (system_prompt, user_prompt)."""
    system_parts = []
    user_parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(f"[{role}]: {content}")
    return "\n".join(system_parts), "\n".join(user_parts)


def _wrap_response(content: str, model: str) -> dict:
    """Wrap plain text in OpenAI-compatible chat completion response."""
    return {
        "id": f"proxy-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content or ""},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _call_via_priority_queue(body: dict, priority: int, caller: str) -> dict:
    """mac1: route through llm_priority_queue.py with full Redis-backed priority."""
    system_prompt, user_prompt = _messages_to_prompts(body.get("messages", []))
    prio = _Priority(priority) if _Priority else priority
    timeout = float(body.get("timeout_s", body.get("timeout", 120)))

    # Auto-select profile from max_tokens hint (cherry-picked from mac3)
    max_tokens = body.get("max_tokens")
    if max_tokens and max_tokens <= 128:
        profile = "classify"
    elif max_tokens and max_tokens <= 512:
        profile = "extract"
    elif max_tokens and max_tokens <= 1024:
        profile = "extract_deep"
    else:
        profile = "deep"

    result = _pq_generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        priority=prio,
        profile=profile,
        caller=caller,
        timeout_s=timeout,
    )
    return _wrap_response(result or "", body.get("model", "qwen3-4b"))


def _call_via_lock(body: dict, backend_url: str) -> dict:
    """Workers: serialize via Lock, forward raw to backend."""
    with _worker_lock:
        r = requests.post(
            f"{backend_url}/v1/chat/completions",
            json=body,
            timeout=float(body.get("timeout", 120)),
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


def _call_via_external(body: dict) -> dict:
    """Workers: no local LLM — route to external API (DeepSeek/OpenAI) via Lock.

    Still serializes through _worker_lock so priority ordering is preserved and
    telemetry flows to chief_ingest. The Lock here is lightweight (external calls
    don't share a GPU) but keeps the routing layer uniform across all nodes.
    """
    api_key = os.environ.get(_EXTERNAL_KEY_ENV, "")
    if not api_key:
        raise RuntimeError(
            f"External LLM key env var '{_EXTERNAL_KEY_ENV}' is not set — "
            f"set it in ~/.zshrc and restart the proxy"
        )
    # Use the model from the request body if present, else the configured default
    request_body = dict(body)
    if not request_body.get("model"):
        request_body["model"] = _EXTERNAL_MODEL

    with _worker_lock:
        r = requests.post(
            f"{_EXTERNAL_ENDPOINT}/chat/completions",
            json=request_body,
            timeout=float(body.get("timeout", 120)),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        r.raise_for_status()
        return r.json()


# ── HTTP handler ──

_PRIORITY_NAMES = {"aaron": 1, "atlas": 2, "external": 3, "background": 4}


def _parse_priority(headers) -> int:
    val = headers.get("X-Priority") or headers.get("x-priority")
    if val:
        # Accept string names ("aaron", "atlas", "external", "background") or ints (1-4)
        if isinstance(val, str) and val.lower() in _PRIORITY_NAMES:
            return _PRIORITY_NAMES[val.lower()]
        try:
            p = int(val)
            if 1 <= p <= 4:
                return p
        except (ValueError, TypeError):
            pass
    return 2  # Default: ATLAS


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug(f"{self.address_string()} {fmt % args}")

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            self._json(200, {
                "status": "ok",
                "nodeId": NODE_ID,
                "mode": "priority_queue" if _USE_PRIORITY_QUEUE else (
                    "worker_lock_external" if _backend_url == _BACKEND_EXTERNAL else "worker_lock"
                ),
                "backend": _EXTERNAL_ENDPOINT if _backend_url == _BACKEND_EXTERNAL else _backend_url,
                "port": PORT,
                "stats": _stats,
                "circuit_breakers": _circuit_breaker.status(),
            })
        elif self.path == "/stats":
            self._json(200, {"nodeId": NODE_ID, **_stats})
        elif self.path.startswith("/v1/models"):
            # Report the actual model name so 3s probe doesn't WARN about mismatch
            if _USE_PRIORITY_QUEUE:
                model_id = "qwen3-4b"
            elif _backend_url == _BACKEND_EXTERNAL:
                model_id = _EXTERNAL_MODEL or "deepseek-chat"
            else:
                model_id = "local-llm"
            self._json(200, {
                "object": "list",
                "data": [{"id": model_id, "object": "model", "owned_by": "proxy"}],
            })
        elif self.path.startswith("/v1/fleet/requests"):
            # mac2-compat: recent request log from Redis llm:proxy:requests
            try:
                import redis
                r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
                raw = r.zrevrange("llm:proxy:requests", 0, 49, withscores=False)
                entries = [json.loads(e) for e in raw]
                self._json(200, {"nodeId": NODE_ID, "count": len(entries), "requests": entries})
            except Exception as e:
                self._json(500, {"error": str(e), "nodeId": NODE_ID})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "endpoint not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, Exception) as e:
            self._json(400, {"error": f"invalid JSON: {e}"})
            return

        priority = _parse_priority(self.headers)
        caller = self.headers.get("X-Caller", self.headers.get("x-caller", "unknown"))
        model = body.get("model", "unknown")
        t0 = time.time()

        try:
            if _USE_PRIORITY_QUEUE:
                # Priority queue mode (mac1) — circuit breaker on the queue itself
                cb_key = "priority_queue"
                if not _circuit_breaker.allow(cb_key):
                    self._json(503, {
                        "error": f"circuit breaker OPEN for {cb_key} — too many consecutive failures",
                        "nodeId": NODE_ID,
                        "circuit_breakers": _circuit_breaker.status(),
                    })
                    return
                try:
                    result = _call_via_priority_queue(body, priority, caller)
                    _circuit_breaker.record_success(cb_key)
                except Exception:
                    _circuit_breaker.record_failure(cb_key)
                    raise
            else:
                result = self._call_with_circuit_breaker(body)

            latency_ms = (time.time() - t0) * 1000
            with _stats_lock:
                _stats["total"] += 1
                p_key = str(priority)
                _stats["by_priority"][p_key] = _stats["by_priority"].get(p_key, 0) + 1

            threading.Thread(
                target=_emit_telemetry,
                args=(caller, priority, latency_ms, model, False),
                daemon=True,
            ).start()

            self._json(200, result)

        except requests.HTTPError as e:
            self._handle_error(e, priority, caller, latency_ms=(time.time() - t0) * 1000, model=model)
        except Exception as e:
            self._handle_error(e, priority, caller, latency_ms=(time.time() - t0) * 1000, model=model)

    def _call_with_circuit_breaker(self, body: dict) -> dict:
        """Route through backends with circuit breaker protection.

        Order: local backends (ollama/mlx) → external API.
        If a backend's circuit is open, skip it and try the next.
        """
        # Try local backends first
        for name, url in _BACKEND_CANDIDATES:
            cb_key = f"local:{name}"
            if not _circuit_breaker.allow(cb_key):
                logger.debug(f"Skipping {name} — circuit open")
                continue
            if not _probe_backend(url):
                continue
            try:
                result = _call_via_lock(body, url)
                _circuit_breaker.record_success(cb_key)
                return result
            except Exception as exc:
                _circuit_breaker.record_failure(cb_key)
                logger.warning(f"Backend {name} failed (circuit breaker tracking): {exc}")
                continue

        # Try external API fallback
        if _EXTERNAL_ENDPOINT and _EXTERNAL_KEY_ENV:
            cb_key = "external"
            if _circuit_breaker.allow(cb_key):
                try:
                    result = _call_via_external(body)
                    _circuit_breaker.record_success(cb_key)
                    return result
                except Exception as exc:
                    _circuit_breaker.record_failure(cb_key)
                    logger.warning(f"External API failed (circuit breaker tracking): {exc}")
            else:
                logger.debug("Skipping external API — circuit open")

        # All backends exhausted or circuit-broken
        raise RuntimeError(
            "all backends unavailable — local LLMs down or circuit-broken, "
            "external API down or circuit-broken. Check /health for circuit breaker state."
        )

    def _handle_error(self, e: Exception, priority: int, caller: str, latency_ms: float, model: str):
        with _stats_lock:
            _stats["errors"] = _stats.get("errors", 0) + 1
        threading.Thread(
            target=_emit_telemetry,
            args=(caller, priority, latency_ms, model, True),
            daemon=True,
        ).start()
        logger.error(f"Proxy error (caller={caller}, p={priority}): {e}")
        self._json(500, {"error": str(e), "nodeId": NODE_ID})


def main():
    global _stats, PORT

    parser = argparse.ArgumentParser(description="LLM Priority Proxy")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--bind", default="0.0.0.0", help="Address to bind to (default: 0.0.0.0)")
    parser.add_argument("--daemon", action="store_true", help="Write PID file to LLM_PROXY_PID_FILE (default: /tmp/llm-proxy.pid)")
    args = parser.parse_args()
    PORT = args.port

    if args.daemon:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        logger.info(f"PID {os.getpid()} written to {PID_FILE}")

    _stats["backend"] = _backend_url

    logger.info(f"LLM Priority Proxy | nodeId={NODE_ID} | port={PORT}")
    if _USE_PRIORITY_QUEUE:
        logger.info(f"  mode    : priority_queue (Redis-backed)")
    elif _backend_url == _BACKEND_EXTERNAL:
        logger.info(f"  mode    : worker_lock_external → {_EXTERNAL_ENDPOINT} (model: {_EXTERNAL_MODEL})")
    else:
        logger.info(f"  mode    : worker_lock (threading.Lock)")
    logger.info(f"  backend : {_EXTERNAL_ENDPOINT if _backend_url == _BACKEND_EXTERNAL else (_backend_url or 'none (will probe on first request)')}")
    logger.info(f"  telemetry→ {'Redis proxy:call_log + llm:proxy:requests' if NODE_ID == 'mac1' else CHIEF_INGEST_URL}")

    server = ThreadingHTTPServer((args.bind, PORT), ProxyHandler)
    logger.info(f"Listening on {args.bind}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Proxy stopped.")
    finally:
        if args.daemon and os.path.exists(PID_FILE):
            os.remove(PID_FILE)


if __name__ == "__main__":
    main()
