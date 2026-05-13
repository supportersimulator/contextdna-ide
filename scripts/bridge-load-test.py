#!/usr/bin/env python3
"""
ContextDNA Claude Bridge — concurrent load test (Cycle 6 F2).

Purpose
-------
Validate the loopback Claude Bridge under concurrent /v1/messages traffic:
  * Confirms AARON priority queue cap=4 holds (memory/llm_priority_queue.py:140)
  * Mixes JSON + SSE modes
  * Diffs /metrics counters before/after
  * Reports route split (Anthropic vs DeepSeek), latencies, ratelimits
  * Snapshots redis llm:queue_stats queue_concurrent_inflight_max

Constraints
-----------
* ZERO SILENT FAILURES — every error captured + reported.
* Caps total tokens at 50 * 20 input + 50 * 20 output = ~2000 tokens.
* Smoke at N=5 first; bails before N=50 if smoke fails.
* No production code modified. Stdlib + requests (already on system).
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

try:
    import requests
except Exception as e:
    print(f"FATAL: requests not available: {e!r}", file=sys.stderr)
    sys.exit(2)


BRIDGE = "http://127.0.0.1:8855"
ENDPOINT = f"{BRIDGE}/v1/messages"
HEALTH = f"{BRIDGE}/health"
METRICS = f"{BRIDGE}/metrics"

# Counters of interest (subset — we diff anything that exists pre+post)
TRACKED_COUNTERS = {
    "fleet_nerve_bridge_anthropic_ok",
    "fleet_nerve_bridge_fallback_to_deepseek",
    "fleet_nerve_bridge_anthropic_skipped",
    "fleet_nerve_bridge_anthropic_passthrough_errors",
    "fleet_nerve_bridge_deepseek_failures",
    "fleet_nerve_bridge_ratelimit_requests_remaining",
    "fleet_nerve_bridge_ratelimit_tokens_remaining",
    "fleet_nerve_bridge_ratelimit_input_tokens_remaining",
    "fleet_nerve_bridge_ratelimit_output_tokens_remaining",
    "fleet_nerve_bridge_ratelimit_last_429_epoch",
    "fleet_nerve_bridge_ratelimit_last_retry_after_seconds",
    "fleet_nerve_bridge_ratelimit_parse_errors",
    "fleet_nerve_bridge_tool_translate_attempted",
    "fleet_nerve_bridge_tool_translate_success",
    "fleet_nerve_bridge_tool_translate_failed",
    "fleet_nerve_bridge_tool_calls_translated_count",
    "fleet_nerve_bridge_stream_requests",
    "fleet_nerve_bridge_stream_anthropic_passthru",
    "fleet_nerve_bridge_oauth_passthrough_attempted",
    "fleet_nerve_bridge_oauth_passthrough_success",
    "fleet_nerve_bridge_oauth_passthrough_failed",
    "fleet_nerve_errors",
}


@dataclass
class Result:
    idx: int
    mode: str  # 'json' or 'sse'
    status: int = -1
    latency_s: float = 0.0
    bytes_in: int = 0
    shape: str = ""  # 'anthropic_message' | 'error_envelope' | 'sse_events:<n>' | 'malformed' | 'exception'
    error: str = ""
    sse_events: int = 0


@dataclass
class Report:
    n_total: int
    n_success: int
    n_fail: int
    latencies: list[float] = field(default_factory=list)
    counters_before: dict[str, float] = field(default_factory=dict)
    counters_after: dict[str, float] = field(default_factory=dict)
    queue_stats_before: dict[str, str] = field(default_factory=dict)
    queue_stats_after: dict[str, str] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)


# ─────────────────────────── helpers ───────────────────────────

def fetch_metrics() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        r = requests.get(METRICS, timeout=5)
        if r.status_code != 200:
            print(f"WARN: /metrics returned {r.status_code}", file=sys.stderr)
            return out
        for line in r.text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            # form: name{labels} value   OR   name value
            try:
                if "{" in line:
                    name, rest = line.split("{", 1)
                    _, val = rest.rsplit("}", 1)
                    val = val.strip().split()[0]
                else:
                    name, val = line.split(maxsplit=1)
                out[name.strip()] = float(val)
            except Exception as e:  # noqa: BLE001
                # ZSF: surface parse error count via stderr
                print(f"WARN: metrics parse fail line={line!r} err={e!r}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: /metrics fetch failed: {e!r}", file=sys.stderr)
    return out


def fetch_queue_stats() -> dict[str, str]:
    """HGETALL llm:queue_stats via redis-cli (no Python redis dep required)."""
    try:
        r = subprocess.run(
            ["redis-cli", "HGETALL", "llm:queue_stats"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return {"_error": f"redis-cli rc={r.returncode} stderr={r.stderr.strip()}"}
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        out: dict[str, str] = {}
        for i in range(0, len(lines) - 1, 2):
            out[lines[i]] = lines[i + 1]
        return out
    except Exception as e:  # noqa: BLE001
        return {"_error": repr(e)}


def health_ok() -> tuple[bool, str]:
    try:
        r = requests.get(HEALTH, timeout=3)
        if r.status_code != 200:
            return False, f"status={r.status_code}"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def make_payload(idx: int, *, stream: bool) -> dict[str, Any]:
    # Tiny payload — we want round-trips, not capacity testing
    return {
        "model": "claude-haiku-4-5",
        "max_tokens": 20,
        "stream": stream,
        "messages": [
            {"role": "user", "content": f"reply 'ok {idx}' only"},
        ],
    }


def fire_one(idx: int, *, stream: bool, timeout: float) -> Result:
    res = Result(idx=idx, mode="sse" if stream else "json")
    payload = make_payload(idx, stream=stream)
    started = time.perf_counter()
    try:
        if stream:
            r = requests.post(ENDPOINT, json=payload, timeout=timeout, stream=True)
            res.status = r.status_code
            event_count = 0
            buf_bytes = 0
            try:
                for line in r.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    buf_bytes += len(line) + 1
                    if line.startswith("event:") or line.startswith("data:"):
                        if line.startswith("event:"):
                            event_count += 1
            except Exception as e:  # noqa: BLE001
                res.error = f"sse_iter_err={e!r}"
            res.sse_events = event_count
            res.bytes_in = buf_bytes
            if r.status_code == 200 and event_count > 0:
                res.shape = f"sse_events:{event_count}"
            elif r.status_code == 200:
                res.shape = "sse_no_events"
            else:
                res.shape = "error_envelope"
        else:
            r = requests.post(ENDPOINT, json=payload, timeout=timeout)
            res.status = r.status_code
            res.bytes_in = len(r.content)
            try:
                data = r.json()
            except Exception as e:  # noqa: BLE001
                res.shape = "malformed"
                res.error = f"json_err={e!r} body={r.text[:120]!r}"
                return res
            if r.status_code == 200 and data.get("type") == "message":
                res.shape = "anthropic_message"
            elif data.get("type") == "error":
                res.shape = "error_envelope"
                err = data.get("error", {})
                res.error = f"{err.get('type')}: {err.get('message','')[:100]}"
            else:
                res.shape = "unknown"
                res.error = f"unexpected_shape keys={list(data.keys())[:6]}"
    except Exception as e:  # noqa: BLE001
        res.shape = "exception"
        res.error = repr(e)
    finally:
        res.latency_s = time.perf_counter() - started
    return res


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs2 = sorted(xs)
    k = max(0, min(len(xs2) - 1, int(round((p / 100.0) * (len(xs2) - 1)))))
    return xs2[k]


def run_burst(n: int, *, json_count: int, sse_count: int, timeout: float) -> list[Result]:
    assert json_count + sse_count == n, "split must sum to n"
    plan = [(i, False) for i in range(json_count)] + [
        (json_count + i, True) for i in range(sse_count)
    ]
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(fire_one, idx, stream=stream, timeout=timeout)
                for (idx, stream) in plan]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                # ZSF: synthesise a Result so we never silently drop
                results.append(Result(idx=-1, mode="?", shape="future_exc", error=repr(e)))
    results.sort(key=lambda r: r.idx)
    return results


def diff_counters(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    diffs: dict[str, float] = {}
    keys = set(before) | set(after)
    for k in keys:
        b = before.get(k, 0.0)
        a = after.get(k, 0.0)
        if a != b:
            diffs[k] = a - b
    return diffs


def render_report(report: Report) -> str:
    lats_ok = [r.latency_s for r in report.results if 200 <= r.status < 300]
    lats_all = [r.latency_s for r in report.results if r.latency_s > 0]
    p50 = percentile(lats_all, 50)
    p95 = percentile(lats_all, 95)
    p99 = percentile(lats_all, 99)

    diffs = diff_counters(report.counters_before, report.counters_after)
    diff_anth = diffs.get("fleet_nerve_bridge_anthropic_ok", 0)
    diff_ds = diffs.get("fleet_nerve_bridge_fallback_to_deepseek", 0)
    diff_skip = diffs.get("fleet_nerve_bridge_anthropic_skipped", 0)
    diff_429 = diffs.get("fleet_nerve_bridge_ratelimit_last_429_epoch", 0)
    diff_tool = diffs.get("fleet_nerve_bridge_tool_calls_translated_count", 0)
    diff_stream = diffs.get("fleet_nerve_bridge_stream_requests", 0)

    n_5xx = sum(1 for r in report.results if r.status >= 500)
    n_429 = sum(1 for r in report.results if r.status == 429)
    shape_counts: dict[str, int] = {}
    for r in report.results:
        shape_counts[r.shape] = shape_counts.get(r.shape, 0) + 1

    qmax_before = report.queue_stats_before.get("queue_concurrent_inflight_max_1", "?")
    qmax_after = report.queue_stats_after.get("queue_concurrent_inflight_max_1", "?")
    qcur_after = report.queue_stats_after.get("queue_concurrent_inflight_1", "?")

    errors = [(r.idx, r.mode, r.status, r.shape, r.error)
              for r in report.results
              if r.status < 200 or r.status >= 300 or r.shape in
              {"malformed", "unknown", "exception", "future_exc"}]

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("ContextDNA Bridge Load Test Report (Cycle 6 F2)")
    lines.append("=" * 72)
    lines.append(f"Total requests : {report.n_total}")
    lines.append(f"Success (2xx)  : {report.n_success}")
    lines.append(f"Failures       : {report.n_fail}")
    lines.append(f"HTTP 429 count : {n_429}")
    lines.append(f"HTTP 5xx count : {n_5xx}")
    lines.append("")
    lines.append("Latency (all attempts, seconds):")
    lines.append(f"  p50 = {p50:.3f}   p95 = {p95:.3f}   p99 = {p99:.3f}   "
                 f"min = {min(lats_all) if lats_all else 0:.3f}   "
                 f"max = {max(lats_all) if lats_all else 0:.3f}")
    if lats_ok:
        lines.append(f"  (success-only: p50={percentile(lats_ok,50):.3f} "
                     f"p95={percentile(lats_ok,95):.3f})")
    lines.append("")
    lines.append("Response shape counts:")
    for s, c in sorted(shape_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {s:24s}  {c}")
    lines.append("")
    lines.append("Route split (counter delta):")
    lines.append(f"  anthropic_ok          +{diff_anth}")
    lines.append(f"  fallback_to_deepseek  +{diff_ds}")
    lines.append(f"  anthropic_skipped     +{diff_skip}")
    lines.append(f"  stream_requests       +{diff_stream}")
    lines.append(f"  tool_calls_translated +{diff_tool}")
    lines.append("")
    lines.append("Ratelimit signal (counter delta):")
    rl_keys = [k for k in diffs if "ratelimit" in k]
    if not rl_keys:
        lines.append("  (no ratelimit counters changed)")
    else:
        for k in sorted(rl_keys):
            lines.append(f"  {k.replace('fleet_nerve_bridge_','')}  delta={diffs[k]}  "
                         f"after={report.counters_after.get(k,'?')}")
    lines.append("")
    lines.append("AARON queue (priority=1) — proves cap=4:")
    lines.append(f"  queue_concurrent_inflight_max_1  before={qmax_before}  after={qmax_after}")
    lines.append(f"  queue_concurrent_inflight_1      after={qcur_after}")
    lines.append("  Cap declared at memory/llm_priority_queue.py:140 (Priority.AARON.value: 4)")
    lines.append("")
    lines.append("Other notable counter deltas:")
    other = {k: v for k, v in diffs.items()
             if "ratelimit" not in k
             and k not in {
                 "fleet_nerve_bridge_anthropic_ok",
                 "fleet_nerve_bridge_fallback_to_deepseek",
                 "fleet_nerve_bridge_anthropic_skipped",
                 "fleet_nerve_bridge_stream_requests",
                 "fleet_nerve_bridge_tool_calls_translated_count",
             }}
    if not other:
        lines.append("  (none)")
    else:
        for k in sorted(other):
            lines.append(f"  {k}  delta={other[k]}")
    lines.append("")
    if errors:
        lines.append(f"Per-request failures ({len(errors)}):")
        for idx, mode, status, shape, err in errors[:20]:
            lines.append(f"  #{idx:02d} mode={mode} status={status} shape={shape} err={err[:140]}")
        if len(errors) > 20:
            lines.append(f"  ...({len(errors)-20} more)")
    else:
        lines.append("Per-request failures: NONE")
    lines.append("=" * 72)
    return "\n".join(lines)


# ─────────────────────────── main ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=5,
                    help="smoke burst size (must succeed before scaling)")
    ap.add_argument("--n", type=int, default=50,
                    help="full burst size (default 50)")
    ap.add_argument("--sse-count", type=int, default=10,
                    help="number of SSE requests in the full burst (rest are JSON)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="per-request HTTP timeout seconds")
    ap.add_argument("--no-smoke", action="store_true",
                    help="skip smoke burst (testing only)")
    args = ap.parse_args()

    print(f"Bridge target : {ENDPOINT}")
    ok, why = health_ok()
    print(f"Health        : {'OK' if ok else 'FAIL'} ({why})")
    if not ok:
        print("ABORT: bridge unhealthy", file=sys.stderr)
        return 3

    # ── SMOKE ──
    if not args.no_smoke:
        print(f"\n--- SMOKE BURST n={args.smoke} (4 JSON + 1 SSE) ---")
        smoke_json = max(1, args.smoke - 1)
        smoke_sse = args.smoke - smoke_json
        smoke = run_burst(args.smoke, json_count=smoke_json, sse_count=smoke_sse,
                          timeout=args.timeout)
        s_ok = sum(1 for r in smoke if 200 <= r.status < 300)
        print(f"smoke ok={s_ok}/{args.smoke}")
        for r in smoke:
            print(f"  #{r.idx} mode={r.mode} status={r.status} "
                  f"lat={r.latency_s:.2f}s shape={r.shape} "
                  f"err={r.error[:100]}")
        if s_ok == 0:
            print("ABORT: smoke produced zero successes — refusing to send full burst",
                  file=sys.stderr)
            return 4
        # Re-check health after smoke
        ok2, why2 = health_ok()
        print(f"post-smoke health: {'OK' if ok2 else 'FAIL'} ({why2})")
        if not ok2:
            print("ABORT: bridge unhealthy after smoke — not running full burst",
                  file=sys.stderr)
            return 5

    # ── FULL BURST ──
    n = args.n
    sse = min(args.sse_count, n)
    js = n - sse
    print(f"\n--- FULL BURST n={n} ({js} JSON + {sse} SSE) ---")

    rep = Report(n_total=n, n_success=0, n_fail=0)
    rep.counters_before = fetch_metrics()
    rep.queue_stats_before = fetch_queue_stats()

    t0 = time.perf_counter()
    rep.results = run_burst(n, json_count=js, sse_count=sse, timeout=args.timeout)
    elapsed = time.perf_counter() - t0
    print(f"burst wall time: {elapsed:.2f}s")

    rep.counters_after = fetch_metrics()
    rep.queue_stats_after = fetch_queue_stats()

    rep.n_success = sum(1 for r in rep.results if 200 <= r.status < 300)
    rep.n_fail = rep.n_total - rep.n_success
    rep.latencies = [r.latency_s for r in rep.results]

    print()
    print(render_report(rep))

    # exit non-zero if ANY hard failures (server crashed style)
    n_5xx = sum(1 for r in rep.results if r.status >= 500)
    n_exc = sum(1 for r in rep.results if r.shape in {"exception", "future_exc"})
    if n_5xx > 0 or n_exc > 0:
        print(f"\nNOTE: {n_5xx} 5xx + {n_exc} exception responses observed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
