#!/usr/bin/env python3
"""
WEBHOOK QUALITY METRICS — Aggregate statistics for injection quality.

Reads from Redis sorted sets populated by _record_quality_metrics() in
persistent_hook_structure.py. Provides rolling-window analytics:
- Payload size distribution (chars + tokens)
- Section fill rates (per-section presence %)
- Latency distribution
- Risk level and depth distribution

Usage:
    python memory/webhook_quality_metrics.py              # Full report
    python memory/webhook_quality_metrics.py --json       # JSON output
    python memory/webhook_quality_metrics.py --section 2  # Section 2 fill rate
"""

import json
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# Redis keys follow pattern: injection:metrics:{metric_name}
# Values in sorted sets: "{injection_id}|{value}" with score = timestamp

METRICS_PREFIX = "injection:metrics:"
ROLLING_WINDOW = 500  # Last N injections


def _get_redis():
    """Get Redis client or None."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _parse_zset_values(entries: List[Tuple[str, float]]) -> List[Tuple[str, str, float]]:
    """Parse sorted set entries into (injection_id, value, timestamp) tuples."""
    results = []
    for member, score in entries:
        parts = member.split("|", 1)
        if len(parts) == 2:
            results.append((parts[0], parts[1], score))
    return results


def get_payload_stats(r=None, hours: float = 24) -> Dict[str, Any]:
    """Get payload size statistics over a time window."""
    if r is None:
        r = _get_redis()
    if not r:
        return {"error": "Redis unavailable"}

    cutoff = time.time() - (hours * 3600)
    entries = r.zrangebyscore(f"{METRICS_PREFIX}payload_chars", cutoff, "+inf", withscores=True)
    parsed = _parse_zset_values(entries)

    if not parsed:
        return {"count": 0, "message": "No data in window"}

    sizes = [int(v) for _, v, _ in parsed]
    tokens_entries = r.zrangebyscore(f"{METRICS_PREFIX}payload_tokens", cutoff, "+inf", withscores=True)
    tokens_parsed = _parse_zset_values(tokens_entries)
    token_counts = [int(v) for _, v, _ in tokens_parsed] if tokens_parsed else []

    return {
        "count": len(sizes),
        "chars": {
            "min": min(sizes),
            "max": max(sizes),
            "avg": sum(sizes) // len(sizes),
            "median": sorted(sizes)[len(sizes) // 2],
        },
        "tokens_est": {
            "min": min(token_counts) if token_counts else 0,
            "max": max(token_counts) if token_counts else 0,
            "avg": sum(token_counts) // len(token_counts) if token_counts else 0,
        },
        "hours": hours,
    }


def get_section_fill_rates(r=None, hours: float = 24) -> Dict[str, Dict[str, Any]]:
    """Get per-section fill rates (% of injections where section had content)."""
    if r is None:
        r = _get_redis()
    if not r:
        return {"error": "Redis unavailable"}

    cutoff = time.time() - (hours * 3600)
    sections = ["section_0", "section_1", "section_2", "section_3",
                "section_4", "section_5", "section_6", "section_7",
                "section_8", "section_10"]

    section_labels = {
        "section_0": "Safety", "section_1": "Foundation", "section_2": "Wisdom",
        "section_3": "Awareness", "section_4": "Deep Context", "section_5": "Protocol",
        "section_6": "Synaptic->Atlas", "section_7": "Full Library",
        "section_8": "8th Intelligence", "section_10": "Strategic Vision",
    }

    results = {}
    for sec in sections:
        entries = r.zrangebyscore(f"{METRICS_PREFIX}fill:{sec}", cutoff, "+inf", withscores=True)
        parsed = _parse_zset_values(entries)
        if not parsed:
            results[sec] = {"label": section_labels.get(sec, sec), "total": 0, "filled": 0, "rate": 0.0}
            continue

        total = len(parsed)
        filled = sum(1 for _, v, _ in parsed if v == "filled")
        results[sec] = {
            "label": section_labels.get(sec, sec),
            "total": total,
            "filled": filled,
            "empty": total - filled,
            "rate": round(filled / total * 100, 1),
        }

    return results


def get_latency_stats(r=None, hours: float = 24) -> Dict[str, Any]:
    """Get generation latency statistics."""
    if r is None:
        r = _get_redis()
    if not r:
        return {"error": "Redis unavailable"}

    cutoff = time.time() - (hours * 3600)
    entries = r.zrangebyscore(f"{METRICS_PREFIX}latency_ms", cutoff, "+inf", withscores=True)
    parsed = _parse_zset_values(entries)

    if not parsed:
        return {"count": 0, "message": "No data in window"}

    latencies = [int(v) for _, v, _ in parsed]
    sorted_lat = sorted(latencies)
    p50 = sorted_lat[len(sorted_lat) // 2]
    p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if len(sorted_lat) >= 20 else sorted_lat[-1]
    p99 = sorted_lat[int(len(sorted_lat) * 0.99)] if len(sorted_lat) >= 100 else sorted_lat[-1]

    return {
        "count": len(latencies),
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "avg_ms": sum(latencies) // len(latencies),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "hours": hours,
    }


def get_risk_distribution(r=None, hours: float = 24) -> Dict[str, int]:
    """Get risk level distribution."""
    if r is None:
        r = _get_redis()
    if not r:
        return {"error": "Redis unavailable"}

    cutoff = time.time() - (hours * 3600)
    entries = r.zrangebyscore(f"{METRICS_PREFIX}risk_level", cutoff, "+inf", withscores=True)
    parsed = _parse_zset_values(entries)
    counts = Counter(v for _, v, _ in parsed)
    return dict(counts)


def get_depth_distribution(r=None, hours: float = 24) -> Dict[str, int]:
    """Get injection depth distribution (FULL vs ABBREV)."""
    if r is None:
        r = _get_redis()
    if not r:
        return {"error": "Redis unavailable"}

    cutoff = time.time() - (hours * 3600)
    entries = r.zrangebyscore(f"{METRICS_PREFIX}depth", cutoff, "+inf", withscores=True)
    parsed = _parse_zset_values(entries)
    counts = Counter(v for _, v, _ in parsed)
    return dict(counts)


def full_report(hours: float = 24, as_json: bool = False) -> str:
    """Generate a full quality metrics report."""
    r = _get_redis()
    if not r:
        return "Redis unavailable — no metrics to report."

    payload = get_payload_stats(r, hours)
    fill_rates = get_section_fill_rates(r, hours)
    latency = get_latency_stats(r, hours)
    risk = get_risk_distribution(r, hours)
    depth = get_depth_distribution(r, hours)

    if as_json:
        return json.dumps({
            "payload": payload,
            "fill_rates": fill_rates,
            "latency": latency,
            "risk_distribution": risk,
            "depth_distribution": depth,
            "window_hours": hours,
        }, indent=2)

    # Human-readable report
    lines = []
    lines.append(f"=== WEBHOOK INJECTION QUALITY METRICS (last {hours}h) ===")
    lines.append("")

    # Payload
    if payload.get("count", 0) > 0:
        lines.append(f"PAYLOAD SIZE ({payload['count']} injections)")
        c = payload["chars"]
        t = payload["tokens_est"]
        lines.append(f"  Chars:  min={c['min']}  avg={c['avg']}  median={c['median']}  max={c['max']}")
        lines.append(f"  Tokens: min={t['min']}  avg={t['avg']}  max={t['max']}")
    else:
        lines.append("PAYLOAD SIZE: No data")
    lines.append("")

    # Fill rates
    lines.append("SECTION FILL RATES")
    if isinstance(fill_rates, dict) and "error" not in fill_rates:
        for sec_key in sorted(fill_rates.keys()):
            info = fill_rates[sec_key]
            if info["total"] > 0:
                bar = "#" * int(info["rate"] / 5) + "." * (20 - int(info["rate"] / 5))
                lines.append(f"  {info['label']:20s} [{bar}] {info['rate']:5.1f}% ({info['filled']}/{info['total']})")
            else:
                lines.append(f"  {info['label']:20s} [no data]")
    lines.append("")

    # Latency
    if latency.get("count", 0) > 0:
        lines.append(f"LATENCY ({latency['count']} samples)")
        lines.append(f"  p50={latency['p50_ms']}ms  p95={latency['p95_ms']}ms  p99={latency['p99_ms']}ms  max={latency['max_ms']}ms")
    else:
        lines.append("LATENCY: No data")
    lines.append("")

    # Risk + Depth
    if risk:
        lines.append(f"RISK DISTRIBUTION: {risk}")
    if depth:
        lines.append(f"DEPTH DISTRIBUTION: {depth}")

    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    hours = 24
    for i, a in enumerate(args):
        if a == "--hours" and i + 1 < len(args):
            hours = float(args[i + 1])

    print(full_report(hours=hours, as_json=as_json))
