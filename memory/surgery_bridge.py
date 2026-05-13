"""Surgery Bridge — direct three_surgeons imports with subprocess fallback.

Tries direct Python imports from the three_surgeons package first. If the package
isn't installed (plugin not available), falls back to subprocess calls via the
`3s` CLI or `scripts/surgery-team.py`. Every fallback is logged (Zero Silent Failures).

Design: all public functions return a dict with at minimum {"status", "output", "path"}.
  - status: "ok" or "error"
  - output: result data (dict, str, or error message)
  - path: "direct" or "subprocess" (which route was taken)

Reversibility: set SURGERY_BRIDGE_MODE env var to control routing:
  - "direct"     → only try direct imports, fail if unavailable
  - "subprocess" → only use subprocess, skip direct imports entirely
  - "auto"       → (default) try direct, fall back to subprocess

Public API:
    cardio_review(topic, **kwargs) -> dict
    neurologist_challenge(topic, **kwargs) -> dict
    ab_validate(description, **kwargs) -> dict
    consensus(claim, **kwargs) -> dict
    get_consensus_for_ab(test_id, hypothesis, config, **kwargs) -> dict
    synaptic_evidence(topic, **kwargs) -> dict
    bridge_status() -> dict
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent

# Plugin version expected by this bridge (must match 3-surgeons pyproject.toml)
_EXPECTED_PLUGIN_VERSION = "1.0.0"

# Routing mode: "auto" (default), "direct", or "subprocess"
_MODE = os.environ.get("SURGERY_BRIDGE_MODE", "auto").lower()

# Max seconds to wait for an import probe or subprocess call
_IMPORT_TIMEOUT = int(os.environ.get("SURGERY_BRIDGE_IMPORT_TIMEOUT", "5"))
_SUBPROCESS_TIMEOUT = int(os.environ.get("SURGERY_BRIDGE_SUBPROCESS_TIMEOUT", "60"))

# Observability counters (thread-safe)
_counters_lock = threading.Lock()
_counters: Dict[str, int] = {
    "direct_calls": 0,
    "subprocess_calls": 0,
    "fallbacks": 0,
    "errors": 0,
}


# ---------------------------------------------------------------------------
# Internal: import detection (cross-platform threading timeout)
# ---------------------------------------------------------------------------
# REVERSIBILITY: old SIGALRM-based import timeout — uncomment to revert
# import signal
# class _ImportTimeout(Exception): pass
# def _alarm_handler(signum, frame): raise _ImportTimeout("import probe timed out")
# Then replace _has_direct_import() with the SIGALRM version from git history.


@lru_cache(maxsize=1)
def _has_direct_import() -> bool:
    """Check if three_surgeons is importable. Cached after first attempt.

    Uses a background thread with timeout to prevent hanging on slow import
    chains (e.g. service connections triggered at import time). Works on all
    platforms (macOS, Linux, Windows) and from any thread (main or worker).
    """
    if _MODE == "subprocess":
        return False

    result: Dict[str, Any] = {"ok": False, "error": None}

    def _try_import():
        try:
            import three_surgeons  # noqa: F401
            result["ok"] = True
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=_try_import, daemon=True)
    t.start()
    t.join(timeout=_IMPORT_TIMEOUT)

    if t.is_alive():
        logger.warning("three_surgeons import timed out after %ds (thread still running)", _IMPORT_TIMEOUT)
        return False

    if result["error"] is not None:
        logger.info("three_surgeons not importable: %s", result["error"])
        return False

    return result["ok"]


def _get_plugin_version() -> str | None:
    """Return the installed three_surgeons version, or None if unavailable."""
    try:
        import importlib.metadata
        return importlib.metadata.version("three-surgeons")
    except Exception as e:
        # ZSF: log and fall through to the in-package import attempt.
        logger.debug("importlib.metadata version lookup failed: %s", e)
    try:
        from three_surgeons import __version__  # type: ignore
        return __version__
    except Exception as e:
        logger.debug("three_surgeons.__version__ import failed: %s", e)
        return None


def _increment(counter: str) -> None:
    """Thread-safe counter increment."""
    with _counters_lock:
        _counters[counter] = _counters.get(counter, 0) + 1


# ---------------------------------------------------------------------------
# Internal: direct import path
# ---------------------------------------------------------------------------

def _call_direct(command: str, topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Call three_surgeons internal Python API for *command*.

    Since the exact internal API structure may vary across versions, we make
    reasonable attempts and raise on any error so the caller can fall through
    to the subprocess path.

    Uses AdapterContext so adapter hooks (cost telemetry, cross-exam logging)
    fire on direct import calls.
    """
    _increment("direct_calls")
    from three_surgeons.adapters import AdapterContext

    try:
        # Commands that use SurgeryTeam get the adapter threaded through.
        # Standalone function calls (cardio_review, challenge, ab_validate)
        # don't construct SurgeryTeam directly here, so adapter integration
        # for those will be added when their internals accept an adapter param.
        # For now, the AdapterContext scopes lifecycle (on_init/close).
        with AdapterContext() as adapter:
            if command == "cardio-review":
                from three_surgeons.core.cardio import cardio_review as _cr  # type: ignore
                result = _cr(topic, **{**kwargs, "adapter": adapter})
                return {"status": "ok", "output": result, "path": "direct"}

            if command == "neurologist-challenge":
                from three_surgeons.core.neurologist import neurologist_challenge as _nc  # type: ignore
                result = _nc(topic, **{**kwargs, "adapter": adapter})
                return {"status": "ok", "output": result, "path": "direct"}

            if command == "ab-validate":
                from three_surgeons.core.cardio import ab_validate as _av  # type: ignore
                result = _av(topic, **{**kwargs, "adapter": adapter})
                return {"status": "ok", "output": result, "path": "direct"}

            if command == "consensus":
                from three_surgeons.core.direct import consensus as _con  # type: ignore
                result = _con(topic, **kwargs)
                return {"status": "ok", "output": result, "path": "direct"}

            if command == "consult":
                # Direct path not wired here — consult requires SurgeryTeam
                # construction which needs config + providers. Fall through
                # to subprocess by raising; auto_consult handles fallback.
                raise NotImplementedError(
                    "consult direct-path not wired; use subprocess fallback"
                )

            raise ValueError(f"Unknown command for direct path: {command}")

    except Exception as exc:
        logger.debug("Direct call failed for %s: %s", command, exc)
        raise


# ---------------------------------------------------------------------------
# Internal: subprocess fallback
# ---------------------------------------------------------------------------

def _call_subprocess(command: str, topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Run command via `3s` CLI (preferred) or `scripts/surgery-team.py`."""
    _increment("subprocess_calls")
    try:
        cli = shutil.which("3s")
        if cli:
            cmd = [cli, command, topic]
        else:
            script = str(REPO_ROOT / "scripts" / "surgery-team.py")
            cmd = ["python3", script, command, topic]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            cwd=str(REPO_ROOT),
        )

        if proc.returncode != 0:
            logger.warning(
                "Subprocess %s exited %d: %s",
                command, proc.returncode, proc.stderr[:500],
            )
            return {
                "status": "error",
                "output": proc.stderr or proc.stdout,
                "path": "subprocess",
                "returncode": proc.returncode,
            }

        # Try to parse JSON output; fall back to raw text
        raw = proc.stdout.strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = raw

        return {"status": "ok", "output": parsed, "path": "subprocess"}

    except subprocess.TimeoutExpired:
        _increment("errors")
        logger.error("Subprocess %s timed out after %ds", command, _SUBPROCESS_TIMEOUT)
        return {"status": "error", "output": "timeout", "path": "subprocess"}
    except Exception as exc:
        _increment("errors")
        logger.error("Subprocess %s failed: %s", command, exc)
        return {"status": "error", "output": str(exc), "path": "subprocess"}


# ---------------------------------------------------------------------------
# Internal: router with fallback + logging
# ---------------------------------------------------------------------------

def _call_with_fallback(command: str, topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Try direct import, fall back to subprocess. Logs WARNING on fallback.

    Routing controlled by SURGERY_BRIDGE_MODE env var:
      "direct"     → direct only, error if unavailable
      "subprocess" → subprocess only, skip direct entirely
      "auto"       → try direct, fall back to subprocess (default)
    """
    # --- Direct-only mode ---
    if _MODE == "direct":
        if not _has_direct_import():
            return {"status": "error", "output": "three_surgeons not importable (mode=direct)", "path": "direct"}
        return _call_direct(command, topic, **kwargs)

    # --- Subprocess-only mode ---
    if _MODE == "subprocess":
        return _call_subprocess(command, topic, **kwargs)

    # --- Auto mode (default): try direct, fall back to subprocess ---
    if _has_direct_import():
        try:
            return _call_direct(command, topic, **kwargs)
        except Exception as exc:
            _increment("fallbacks")
            logger.warning(
                "Direct call failed for '%s', falling back to subprocess: %s",
                command, exc,
            )

    if not _has_direct_import():
        _increment("fallbacks")
        logger.warning(
            "three_surgeons not importable — using subprocess fallback for '%s'",
            command,
        )
    return _call_subprocess(command, topic, **kwargs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cardio_review(topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Run a cardiologist cross-examination review on *topic*."""
    return _call_with_fallback("cardio-review", topic, **kwargs)


def neurologist_challenge(topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Run a neurologist corrigibility challenge on *topic*."""
    return _call_with_fallback("neurologist-challenge", topic, **kwargs)


def ab_validate(description: str, **kwargs: Any) -> Dict[str, Any]:
    """Quick 3-surgeon A/B validation for *description*."""
    return _call_with_fallback("ab-validate", description, **kwargs)


def consensus(claim: str, **kwargs: Any) -> Dict[str, Any]:
    """Get confidence-weighted consensus on *claim*."""
    return _call_with_fallback("consensus", claim, **kwargs)


# ---------------------------------------------------------------------------
# RACE AB1 — auto_consult: brainstorming-skill auto-invocation entrypoint
# ---------------------------------------------------------------------------
# When Atlas hits BLOCKING phase on a creative-class prompt (per the
# `classify_task_for_skill()` classifier in synaptic_deep_voice.py), the
# brainstorming skill currently ONLY explores intent — no automatic 3s
# cross-examination. Aaron has to type `3s consult` by hand. This wires the
# auto-invocation: pass the prompt + a depth budget, get back a verdict
# string Atlas can render into S6 as `[3S_VERDICT: …]`.
#
# Depth budgets:
#   "light"    — 30s subprocess timeout, default for brainstorming auto-call
#   "standard" — 120s, used when caller explicitly opts in
#   "full"     — 300s, full cross-exam (rarely used from auto-path)
#
# Zero Silent Failures:
#   - Any subprocess error / timeout → status="error" + counter incremented
#   - Caller is expected to render gracefully (no [3S_VERDICT: …] line)
#   - We NEVER raise out of auto_consult — the brainstorming path must not
#     block on a degraded surgeon backend.

_DEPTH_TIMEOUTS_S = {
    "light": 30,
    "standard": 120,
    "full": 300,
}

_AUTO_CONSULT_COUNTERS = {
    "calls": 0,
    "successes": 0,
    "errors": 0,
    "timeouts": 0,
}
_auto_consult_lock = threading.Lock()


def _auto_consult_incr(key: str) -> None:
    with _auto_consult_lock:
        _AUTO_CONSULT_COUNTERS[key] = _AUTO_CONSULT_COUNTERS.get(key, 0) + 1


def get_auto_consult_counters() -> Dict[str, int]:
    """Return cumulative auto_consult counters (calls/successes/errors/timeouts)."""
    with _auto_consult_lock:
        return dict(_AUTO_CONSULT_COUNTERS)


def _summarize_consult_output(raw: Any, max_chars: int = 400) -> str:
    """Squash a consult result into a single-line verdict summary.

    Accepts dict (parsed JSON), str (raw text), or anything else. Returns a
    short string suitable for prepending to S6 as `[3S_VERDICT: <summary>]`.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        # Prefer explicit summary fields; fall back to concatenated reports.
        for k in ("summary", "verdict", "synthesis", "consensus"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:max_chars].replace("\n", " ")
        cardio = raw.get("cardiologist_report") or raw.get("cardiologist") or ""
        neuro = raw.get("neurologist_report") or raw.get("neurologist") or ""
        joined = " | ".join(s.strip() for s in (cardio, neuro) if isinstance(s, str) and s.strip())
        if joined:
            return joined[:max_chars].replace("\n", " ")
        # No usable text in the dict — treat as empty (degraded backend).
        return ""
    if isinstance(raw, str):
        # Subprocess returned plain text (CLI human-readable output). Take the
        # first non-empty line that isn't a header/separator as the summary.
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("---") or line.lower().startswith("consulting on"):
                continue
            return line[:max_chars]
        return raw.strip()[:max_chars]
    return str(raw)[:max_chars]


def auto_consult(
    prompt: str,
    depth: str = "light",
    min_words: int = 6,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Auto-invoke 3s consult on a brainstorming-class prompt.

    Wired into the brainstorming skill path (memory/synaptic_deep_voice.py)
    so creative prompts entering BLOCKING phase get an automatic multi-model
    cross-examination instead of waiting for Aaron to type `3s consult`.

    Args:
        prompt: The user's creative prompt (e.g. "let's add X feature").
        depth:  Budget bucket — "light" (30s), "standard" (120s), "full" (300s).
        min_words: Minimum word count to trigger auto-consult. Short prompts
                   (<6 words) are skipped — they don't carry enough context
                   for the surgeons to add value, and we'd just burn budget.

    Returns:
        Dict with at minimum:
          - status: "ok" / "error" / "skipped"
          - summary: single-line verdict suitable for [3S_VERDICT: …] line
          - output: raw consult output (dict or str)
          - depth: which budget was used
          - reason: present on "skipped"/"error" with the why

    ZSF: NEVER raises. All failure modes return status="error" + a reason,
    and bump the appropriate counter (errors/timeouts).
    """
    _auto_consult_incr("calls")

    # Word-count gate. ≥6 words per the brainstorming auto-invoke spec.
    word_count = len((prompt or "").split())
    if word_count < min_words:
        return {
            "status": "skipped",
            "summary": "",
            "reason": f"prompt too short ({word_count} words, need ≥{min_words})",
            "depth": depth,
        }

    timeout_s = _DEPTH_TIMEOUTS_S.get(depth, _DEPTH_TIMEOUTS_S["light"])

    # Honor SURGERY_BRIDGE_SUBPROCESS_TIMEOUT as the absolute ceiling so a
    # misbehaving CLI can't hang the brainstorming path past the env limit.
    effective_timeout = min(timeout_s, _SUBPROCESS_TIMEOUT)

    # Build the consult invocation. We thread the depth-specific timeout in
    # via the env var the subprocess path reads. Saving/restoring the prior
    # value avoids leaking state across calls.
    prior = os.environ.get("SURGERY_BRIDGE_SUBPROCESS_TIMEOUT")
    os.environ["SURGERY_BRIDGE_SUBPROCESS_TIMEOUT"] = str(effective_timeout)
    try:
        result = _call_with_fallback("consult", prompt, **kwargs)
    except Exception as exc:
        # _call_with_fallback already swallows most errors, but we belt-and-
        # suspender the entire path so the brainstorming skill never sees a
        # raised exception from auto_consult.
        _auto_consult_incr("errors")
        logger.warning("auto_consult unexpected error: %s", exc)
        return {
            "status": "error",
            "summary": "",
            "reason": str(exc),
            "depth": depth,
        }
    finally:
        if prior is None:
            os.environ.pop("SURGERY_BRIDGE_SUBPROCESS_TIMEOUT", None)
        else:
            os.environ["SURGERY_BRIDGE_SUBPROCESS_TIMEOUT"] = prior

    status = result.get("status", "error")
    if status == "error":
        # Distinguish timeouts so observability shows real cause.
        out_str = str(result.get("output", ""))
        if "timeout" in out_str.lower():
            _auto_consult_incr("timeouts")
        else:
            _auto_consult_incr("errors")
        return {
            "status": "error",
            "summary": "",
            "reason": out_str[:200],
            "depth": depth,
            "output": result.get("output"),
        }

    summary = _summarize_consult_output(result.get("output"))
    if not summary:
        # Surgeons returned ok but no usable text — count as error so we
        # surface degraded backends in the counter, but caller treats it
        # the same as success-with-empty-verdict (no line emitted).
        _auto_consult_incr("errors")
        return {
            "status": "error",
            "summary": "",
            "reason": "empty consult output",
            "depth": depth,
            "output": result.get("output"),
        }

    _auto_consult_incr("successes")
    return {
        "status": "ok",
        "summary": summary,
        "depth": depth,
        "output": result.get("output"),
        "path": result.get("path"),
    }


def get_consensus_for_ab(
    test_id: str,
    hypothesis: str,
    config: Dict[str, Any],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Get 3-surgeon consensus specifically for an A/B test conclusion.

    Combines *test_id*, *hypothesis*, and *config* into a structured prompt
    and routes through the consensus command.
    """
    prompt = (
        f"A/B test '{test_id}': hypothesis='{hypothesis}'. "
        f"Config: {json.dumps(config, default=str)}. "
        "Should this test be concluded as successful, failed, or inconclusive?"
    )
    result = _call_with_fallback("consensus", prompt, **kwargs)
    # Enrich with AB metadata
    result["test_id"] = test_id
    result["hypothesis"] = hypothesis
    return result


# ---------------------------------------------------------------------------
# Synaptic evidence source
# ---------------------------------------------------------------------------

def synaptic_evidence(topic: str, **kwargs: Any) -> Dict[str, Any]:
    """Gather Synaptic evidence for surgeon cross-examinations.

    Lazy-imports synaptic_surgeon_adapter. Returns formatted evidence
    dict compatible with surgeon consumption. Degrades gracefully if
    Synaptic modules are unavailable.

    Args:
        topic: The subject to gather evidence on.
        max_items: Max evidence items (default 10, passed via kwargs).

    Returns:
        {"status": "ok"/"error", "output": {...}, "path": "synaptic"}
    """
    try:
        from memory.synaptic_surgeon_adapter import get_surgeon_adapter
        adapter = get_surgeon_adapter()

        max_items = kwargs.pop("max_items", 10)
        evidence_items = adapter.get_evidence_for_topic(topic, max_items=max_items)
        perspective = adapter.get_surgical_perspective(topic)
        consensus_fmt = adapter.format_for_consensus(topic)

        return {
            "status": "ok",
            "output": {
                "evidence": [
                    {
                        "source": e.source,
                        "type": e.evidence_type,
                        "title": e.title,
                        "description": e.description,
                        "confidence": e.confidence,
                        "session_span": e.session_span,
                    }
                    for e in evidence_items
                ],
                "perspective": perspective,
                "consensus_format": consensus_fmt,
                "evidence_count": len(evidence_items),
            },
            "path": "synaptic",
        }
    except Exception as exc:
        _increment("errors")
        logger.warning("Synaptic evidence gather failed: %s", exc)
        return {
            "status": "error",
            "output": f"Synaptic unavailable: {exc}",
            "path": "synaptic",
        }


# ---------------------------------------------------------------------------
# Diagnostic: bridge_status()
# ---------------------------------------------------------------------------

def bridge_status() -> Dict[str, Any]:
    """Return comprehensive bridge status for diagnostics.

    Reports: active mode, import availability, CLI availability, plugin version,
    version compatibility, and cumulative call/fallback/error counters.
    Designed for `3s bridge-status` CLI command and programmatic health checks.
    """
    cli_path = shutil.which("3s")
    plugin_version = _get_plugin_version()
    importable = _has_direct_import()

    # Version compatibility
    version_ok = None
    version_msg = None
    if plugin_version is not None:
        if plugin_version == _EXPECTED_PLUGIN_VERSION:
            version_ok = True
            version_msg = f"matches expected {_EXPECTED_PLUGIN_VERSION}"
        else:
            version_ok = False
            version_msg = (
                f"MISMATCH: installed={plugin_version}, "
                f"expected={_EXPECTED_PLUGIN_VERSION}"
            )
            logger.warning("Plugin version mismatch: %s", version_msg)

    # Determine effective routing
    if _MODE == "direct":
        effective = "direct" if importable else "direct (UNAVAILABLE — will error)"
    elif _MODE == "subprocess":
        effective = "subprocess"
    else:  # auto
        if importable:
            effective = "direct (auto — import available)"
        elif cli_path:
            effective = "subprocess via 3s CLI (auto — import unavailable)"
        else:
            effective = "subprocess via surgery-team.py (auto — no import, no CLI)"

    with _counters_lock:
        counters = dict(_counters)

    return {
        "mode": _MODE,
        "effective_route": effective,
        "direct_import_available": importable,
        "cli_available": cli_path is not None,
        "cli_path": cli_path,
        "plugin_version": plugin_version,
        "expected_version": _EXPECTED_PLUGIN_VERSION,
        "version_compatible": version_ok,
        "version_detail": version_msg,
        "import_timeout_s": _IMPORT_TIMEOUT,
        "subprocess_timeout_s": _SUBPROCESS_TIMEOUT,
        "counters": counters,
    }
