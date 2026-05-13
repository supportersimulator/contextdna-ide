"""
SYNAPTIC 3-SURGEON HEALTH MONITOR — Detects DEGRADED state when the Neurologist
falls back to the same provider as the Cardiologist (Constitutional Physics #5
violation: "3 distinct LLMs always").

Background
----------
The Neurologist surgeon is a *local* MLX server (Qwen3-4B-4bit on port 5044).
When that server is down, 3-surgeons silently falls back to DeepSeek-chat — the
same vendor/model the Cardiologist already uses. The cross-examination
collapses from 3-of-3 to 2-of-3 distinct backends, but consensus still
"works", so the regression is invisible unless someone runs `3s probe` by hand.

This module turns that invisible regression into an observable signal.

What it does
------------
1. Probes 3-surgeons (cheap: parses the same probe output the CLI emits).
2. Detects DEGRADED state — Neurologist provider == Cardiologist provider, OR
   Neurologist reported as `(fallback)`.
3. Emits a structured DEGRADED signal:
     - JSON line to /tmp/synaptic_3s_health.log (always, observable)
     - Sets a sentinel file at /tmp/synaptic_3s_degraded (presence == degraded)
     - Optional: POST to fleet nerve daemon if reachable (P2 channel)

Designed to be invoked:
- Periodically by Butler / scheduler (cheap, ~1s)
- On-demand from atlas-ops or gains-gate (`python3 memory/synaptic_3s_health.py`)

ZERO SILENT FAILURES: Every exception is logged. Exit code reflects state:
    0 → 3 distinct LLMs (HEALTHY)
    1 → DEGRADED (Neurologist fell back to Cardiologist's provider)
    2 → probe failed (cannot determine state)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOG_PATH = Path("/tmp/synaptic_3s_health.log")
SENTINEL_PATH = Path("/tmp/synaptic_3s_degraded")
NERVE_HEALTH_URL = "http://127.0.0.1:8855/health"
NERVE_MESSAGE_URL = "http://127.0.0.1:8855/message"

# Hard-coded common 3s CLI locations (we don't want to import 3s — it has a
# heavy dependency tree). Subprocess is cheaper and matches what users see.
_THREES_CLI_CANDIDATES = (
    Path.home() / ".claude/plugins/cache/3-surgeons-marketplace/3-surgeons/1.0.0/.venv/bin/3s",
    Path("/usr/local/bin/3s"),
    Path("/opt/homebrew/bin/3s"),
)


def _find_3s_cli() -> Optional[Path]:
    """Locate the 3s CLI binary, preferring the plugin-installed one."""
    which = shutil.which("3s")
    if which:
        return Path(which)
    for candidate in _THREES_CLI_CANDIDATES:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


# Probe output looks like:
#   Cardiologist: OK (provider=deepseek, model=deepseek-chat, 771ms)
#   Neurologist:  OK (fallback) (provider=deepseek, model=deepseek-chat, 691ms)
_LINE_RE = re.compile(
    r"^\s*(Cardiologist|Neurologist):\s+(?P<status>OK|FAIL|ERROR)"
    r"(?:\s+\((?P<flag>fallback[^)]*)\))?"
    r"\s+\(provider=(?P<provider>[^,]+),\s+model=(?P<model>[^,)]+)",
    re.MULTILINE,
)


def parse_probe_output(text: str) -> Dict[str, Dict[str, Any]]:
    """Extract per-surgeon provider/model/fallback flag from `3s probe` output."""
    results: Dict[str, Dict[str, Any]] = {}
    for match in _LINE_RE.finditer(text):
        surgeon = match.group(1).lower()
        results[surgeon] = {
            "status": match.group("status"),
            "fallback": bool(match.group("flag")),
            "provider": match.group("provider").strip(),
            "model": match.group("model").strip(),
        }
    return results


def probe_3s(timeout_s: int = 30) -> Tuple[bool, str]:
    """Run `3s probe` and return (success, raw_stdout). Never raises."""
    cli = _find_3s_cli()
    if cli is None:
        return False, "3s CLI not found in PATH or known plugin locations"
    try:
        proc = subprocess.run(
            [str(cli), "probe"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"3s probe timed out after {timeout_s}s"
    except Exception as exc:  # pragma: no cover — observable failure path
        return False, f"3s probe raised: {type(exc).__name__}: {exc}"


def evaluate(parsed: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Determine HEALTHY/DEGRADED/UNKNOWN from parsed surgeon info."""
    cardio = parsed.get("cardiologist")
    neuro = parsed.get("neurologist")
    if not cardio or not neuro:
        return {
            "state": "UNKNOWN",
            "reason": "missing surgeon line in probe output",
            "cardio": cardio,
            "neuro": neuro,
        }

    same_provider = cardio["provider"] == neuro["provider"]
    neuro_fallback = bool(neuro.get("fallback"))

    if neuro_fallback or same_provider:
        return {
            "state": "DEGRADED",
            "reason": (
                "neurologist using fallback provider"
                if neuro_fallback
                else "cardiologist and neurologist share provider"
            ),
            "cardio": cardio,
            "neuro": neuro,
            "constitutional_physics": "violation:#5_three_distinct_llms",
            "fix": "start local mlx_lm.server on :5044 (bash scripts/warm-mlx-on-boot.sh)",
        }

    return {
        "state": "HEALTHY",
        "reason": "3 distinct LLMs (Atlas + Cardiologist + Neurologist)",
        "cardio": cardio,
        "neuro": neuro,
    }


def _write_log(entry: Dict[str, Any]) -> None:
    """Append a JSON line. Best-effort, never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception as exc:
        # Last-resort: stderr so we never silently drop an observability event
        print(f"synaptic_3s_health: log write failed: {exc}", file=sys.stderr)


def _update_sentinel(state: str, evaluation: Dict[str, Any]) -> None:
    """Touch/remove the sentinel file so other tools can detect with a stat."""
    try:
        if state == "DEGRADED":
            SENTINEL_PATH.write_text(
                json.dumps(evaluation, indent=2), encoding="utf-8"
            )
        else:
            SENTINEL_PATH.unlink(missing_ok=True)
    except Exception as exc:
        print(f"synaptic_3s_health: sentinel update failed: {exc}", file=sys.stderr)


def _emit_to_fleet_nerve(evaluation: Dict[str, Any]) -> bool:
    """Best-effort POST to local fleet nerve daemon. Returns True if delivered."""
    try:
        import urllib.error
        import urllib.request

        # Health-check first — don't bother building a POST if the daemon is
        # down (saves ~200ms on every cold call).
        urllib.request.urlopen(NERVE_HEALTH_URL, timeout=1).read()  # noqa: S310

        body = json.dumps(
            {
                "type": "context",
                "to": "atlas",
                "payload": {
                    "subject": "[3S DEGRADED] Neurologist fell back to Cardiologist provider",
                    "body": json.dumps(evaluation, indent=2),
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            NERVE_MESSAGE_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()  # noqa: S310
        return True
    except Exception:
        # Daemon down or unreachable is normal; we already wrote the log line.
        return False


def run_check(emit_to_fleet: bool = True) -> Dict[str, Any]:
    """Top-level entry point. Probes, evaluates, emits, returns the evaluation."""
    started = time.time()
    ok, raw = probe_3s()
    if not ok:
        evaluation = {
            "state": "UNKNOWN",
            "reason": "probe failed",
            "raw": raw[-500:],  # tail to keep the log line bounded
        }
    else:
        evaluation = evaluate(parse_probe_output(raw))

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_ms": int((time.time() - started) * 1000),
        **evaluation,
    }
    _write_log(entry)
    _update_sentinel(evaluation["state"], evaluation)
    if emit_to_fleet and evaluation["state"] == "DEGRADED":
        entry["fleet_nerve_delivered"] = _emit_to_fleet_nerve(evaluation)
    return entry


def main(argv: list[str]) -> int:
    """CLI entry. Prints JSON, exits 0 (HEALTHY) / 1 (DEGRADED) / 2 (UNKNOWN)."""
    quiet = "--quiet" in argv or "-q" in argv
    no_fleet = "--no-fleet" in argv
    result = run_check(emit_to_fleet=not no_fleet)
    if not quiet:
        print(json.dumps(result, indent=2))
    return {"HEALTHY": 0, "DEGRADED": 1, "UNKNOWN": 2}.get(result["state"], 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
