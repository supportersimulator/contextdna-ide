#!/usr/bin/env python3
"""
RACE W4 — Webhook end-to-end performance benchmark + regression guard.

Measures cold-path (caches cleared) and warm-path (caches hot) latency for
the 9-section webhook injection pipeline (memory/persistent_hook_structure.py).

Outputs:
- Human-readable table to stderr (so JSON on stdout is clean)
- JSON to stdout for machine-readable consumption (CI gates, dashboards)

Per-section timings are extracted from the `section_perf` log line shipped
in commit 1a6cc912 (s0=<ms> s1=<ms> ... s10=<ms>). The line is captured by
attaching a temporary stderr handler to the python `logging` root logger
during each run.

Cache scope (cleared at cold-path):
  contextdna:s2:cache:*           (S2 wisdom — DeepSeek-Lite/Professor)
  contextdna:s2:stats:*           (counters, optional reset)
  contextdna:s4:blueprint:cache:* (S4 blueprint cache, RACE T2)
  contextdna:s4:*                 (legacy s4 section cache via _make_cache_key)
  contextdna:s6:cache:*           (S6 synaptic deep voice)
  contextdna:s8:cache:*           (S8 8th intelligence)
  contextdna:s1:*                 (S1 foundation)
  webhook:*                       (any defensive misc keys)

Usage:
    python3 scripts/webhook-bench.py
    python3 scripts/webhook-bench.py --warm-iterations 20 --json-out bench.json
    python3 scripts/webhook-bench.py --cold-iterations 5 --prompt "deploy ..."
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure repo root on path so memory.* imports resolve from the worktree.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Cache key prefixes/patterns to clear before cold-path runs.
# Sourced from:
#   memory/persistent_hook_structure.py (_S2_CACHE_KEY_PREFIX, _S4_BLUEPRINT_*)
#   memory/synaptic_deep_voice.py       (_S6_*, _S8_*)
#   memory/webhook_utils.py             (_make_cache_key → contextdna:<prefix>:...)
CACHE_KEY_PATTERNS: Tuple[str, ...] = (
    "contextdna:s1:*",
    "contextdna:s2:cache:*",
    "contextdna:s4:*",
    "contextdna:s4:blueprint:cache:*",
    "contextdna:s6:cache:*",
    "contextdna:s8:cache:*",
    "webhook:*",
)

# Default test prompt — must be > 5 words so the short-prompt bypass does
# NOT trigger (see persistent_hook_structure.py line ~3940).
DEFAULT_PROMPT = (
    "benchmark webhook end-to-end perf for the nine section injection "
    "pipeline regression guard"
)

# section_perf format (commit 1a6cc912):
#   "section_perf parallel_ms=<n> missing=<csv|none> s0=<ms> s1=<ms> ..."
SECTION_PERF_RE = re.compile(
    r"section_perf\s+parallel_ms=(?P<parallel>\d+)\s+missing=(?P<missing>\S+)\s+(?P<timings>.*)"
)
SECTION_TIMING_RE = re.compile(r"(s\d+)=(\d+)")


def _percentile(values: List[float], pct: float) -> float:
    """Return percentile from values without numpy (small N)."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_vals = sorted(values)
    # Nearest-rank method (ceil), simple + reproducible for small N.
    k = max(1, int(round((pct / 100.0) * len(sorted_vals))))
    return float(sorted_vals[min(k - 1, len(sorted_vals) - 1)])


def _clear_caches() -> Dict[str, int]:
    """DEL all webhook-related Redis cache keys. Returns deleted counts.

    ZSF: any failure is reported, never silently swallowed.
    """
    counts: Dict[str, int] = {}
    try:
        from memory.redis_cache import get_redis_client  # type: ignore
    except Exception as e:  # pragma: no cover
        print(f"[bench] FATAL: cannot import redis_cache: {e}", file=sys.stderr)
        raise

    client = get_redis_client()
    if client is None:
        print("[bench] FATAL: redis client unavailable", file=sys.stderr)
        raise RuntimeError("redis unavailable")

    for pattern in CACHE_KEY_PATTERNS:
        deleted = 0
        # SCAN over keyspace (KEYS is O(N) blocking — avoid in hot ops).
        try:
            for key in client.scan_iter(match=pattern, count=500):
                try:
                    client.delete(key)
                    deleted += 1
                except Exception as e:
                    print(
                        f"[bench] WARN: del failed for {key!r}: {e}",
                        file=sys.stderr,
                    )
        except Exception as e:
            print(
                f"[bench] WARN: scan failed for pattern {pattern!r}: {e}",
                file=sys.stderr,
            )
        counts[pattern] = deleted
    return counts


@contextmanager
def _capture_logs():
    """Capture root-logger output during a webhook run.

    The section_perf line is emitted via _safe_log("info", ...) which routes
    through the standard `logging` module (see persistent_hook_structure.py).
    We attach a temporary StreamHandler at INFO level so we can grep its text.
    """
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    prev_level = root.level
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


def _parse_section_perf(captured: str) -> Tuple[Optional[int], Dict[str, int], List[str]]:
    """Extract parallel_ms, per-section timings, and missing list from logs."""
    parallel_ms: Optional[int] = None
    timings: Dict[str, int] = {}
    missing: List[str] = []
    for line in captured.splitlines():
        m = SECTION_PERF_RE.search(line)
        if not m:
            continue
        parallel_ms = int(m.group("parallel"))
        miss = m.group("missing")
        if miss and miss != "none":
            missing = miss.split(",")
        for tm in SECTION_TIMING_RE.finditer(m.group("timings")):
            timings[tm.group(1)] = int(tm.group(2))
        # Use the LAST section_perf line in case multiple injections fired.
    return parallel_ms, timings, missing


def _run_once(prompt: str, mode: str) -> Dict[str, object]:
    """Run a single webhook generation; return latency + per-section data."""
    # Import lazily so cache clearing happens before any module-level state.
    from memory.persistent_hook_structure import generate_context_injection  # type: ignore

    with _capture_logs() as buf:
        t0 = time.monotonic()
        try:
            result = generate_context_injection(prompt, mode)
            ok = True
            err = None
        except Exception as e:
            result = None
            ok = False
            err = repr(e)
        wall_ms = int((time.monotonic() - t0) * 1000)
        captured = buf.getvalue()

    parallel_ms, timings, missing = _parse_section_perf(captured)

    return {
        "wall_ms": wall_ms,
        "parallel_ms": parallel_ms,
        "section_timings_ms": timings,
        "missing_sections": missing,
        "ok": ok,
        "error": err,
        "sections_included": list(getattr(result, "sections_included", []) or []) if result else [],
        "volume_tier": getattr(result, "volume_tier", None) if result else None,
    }


def _summarize(label: str, runs: List[Dict[str, object]]) -> Dict[str, object]:
    walls = [r["wall_ms"] for r in runs if r.get("ok")]
    summary: Dict[str, object] = {
        "label": label,
        "n": len(runs),
        "n_ok": len(walls),
        "wall_ms": {
            "min": min(walls) if walls else 0,
            "p50": _percentile(walls, 50),
            "p95": _percentile(walls, 95),
            "p99": _percentile(walls, 99),
            "max": max(walls) if walls else 0,
            "mean": statistics.mean(walls) if walls else 0,
        },
        "runs": runs,
    }

    # Per-section worst-case across runs.
    worst: Dict[str, int] = {}
    for r in runs:
        for sec, ms in (r.get("section_timings_ms") or {}).items():
            if ms > worst.get(sec, 0):
                worst[sec] = ms
    summary["section_worst_ms"] = worst
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Webhook E2E perf benchmark")
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt to inject (must be >5 words to avoid short-prompt bypass).",
    )
    parser.add_argument("--mode", default="hybrid", choices=["layered", "greedy", "hybrid", "minimal"])
    parser.add_argument("--cold-iterations", type=int, default=3)
    parser.add_argument("--warm-iterations", type=int, default=10)
    parser.add_argument("--json-out", default=None, help="Write JSON report to file")
    parser.add_argument("--no-cold", action="store_true", help="Skip cold-path runs")
    parser.add_argument("--no-warm", action="store_true", help="Skip warm-path runs")
    args = parser.parse_args()

    if len(args.prompt.split()) <= 5:
        print(
            "[bench] FATAL: prompt must be >5 words "
            "(short-prompt bypass would skip injection).",
            file=sys.stderr,
        )
        return 2

    started_at = time.time()
    report: Dict[str, object] = {
        "schema": "webhook-bench/v1",
        "started_at": started_at,
        "prompt": args.prompt,
        "mode": args.mode,
        "cold": None,
        "warm": None,
    }

    print(f"[bench] prompt={args.prompt!r} mode={args.mode}", file=sys.stderr)

    # ----- COLD -----
    if not args.no_cold:
        print(f"[bench] cold path: clearing caches + {args.cold_iterations} runs", file=sys.stderr)
        cleared = _clear_caches()
        report["cache_cleared"] = cleared
        cold_runs: List[Dict[str, object]] = []
        for i in range(args.cold_iterations):
            # Each cold run requires the caches re-cleared (run #2 would be warm
            # otherwise — that defeats the purpose of measuring cold p95).
            if i > 0:
                _clear_caches()
            r = _run_once(args.prompt, args.mode)
            print(
                f"[bench]   cold run {i + 1}: wall={r['wall_ms']}ms "
                f"parallel={r.get('parallel_ms')} ok={r['ok']}",
                file=sys.stderr,
            )
            cold_runs.append(r)
        report["cold"] = _summarize("cold", cold_runs)

    # ----- WARM -----
    if not args.no_warm:
        # Prime caches first if cold was skipped.
        if args.no_cold:
            print("[bench] warm path: priming caches with 1 unmeasured run", file=sys.stderr)
            _run_once(args.prompt, args.mode)

        print(f"[bench] warm path: {args.warm_iterations} runs (caches hot)", file=sys.stderr)
        warm_runs: List[Dict[str, object]] = []
        for i in range(args.warm_iterations):
            r = _run_once(args.prompt, args.mode)
            print(
                f"[bench]   warm run {i + 1}: wall={r['wall_ms']}ms "
                f"parallel={r.get('parallel_ms')} ok={r['ok']}",
                file=sys.stderr,
            )
            warm_runs.append(r)
        report["warm"] = _summarize("warm", warm_runs)

    report["finished_at"] = time.time()
    report["duration_s"] = round(report["finished_at"] - started_at, 2)

    # Human table to stderr.
    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("WEBHOOK BENCH RESULTS", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    for key in ("cold", "warm"):
        s = report.get(key)
        if not s:
            continue
        w = s["wall_ms"]
        print(
            f"{key.upper():4s}  n={s['n']:<3d} ok={s['n_ok']:<3d}  "
            f"min={w['min']:>6d}ms  p50={w['p50']:>6.0f}ms  "
            f"p95={w['p95']:>6.0f}ms  p99={w['p99']:>6.0f}ms  "
            f"max={w['max']:>6d}ms",
            file=sys.stderr,
        )
        worst = s.get("section_worst_ms") or {}
        if worst:
            top = sorted(worst.items(), key=lambda kv: -kv[1])[:5]
            top_str = " ".join(f"{k}={v}ms" for k, v in top)
            print(f"      worst sections: {top_str}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    out = json.dumps(report, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(out)
        print(f"[bench] wrote JSON to {args.json_out}", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
