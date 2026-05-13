#!/usr/bin/env python3
"""Basic CLI for testing chain orchestration.

Usage:
    python memory/chain_cli.py presets
    python memory/chain_cli.py suggest --trigger plan_file_detected
    python memory/chain_cli.py run full-3s --topic "auth review"
    python memory/chain_cli.py history [--chain-id ID]
    python memory/chain_cli.py telemetry [--chain-id ID]
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.chain_modes import ModeAuthority, PRESETS
from memory.chain_engine import ChainExecutor, ChainState, SEGMENT_REGISTRY
from memory.chain_requirements import RuntimeContext
from memory.chain_telemetry import ChainTelemetry, ExecutionRecord
from memory.chain_config import ChainConfig

# Register all available segments
import memory.chain_segments_init  # noqa: F401


_telemetry = ChainTelemetry(backend="memory")


def _detect_runtime_context() -> RuntimeContext:
    import subprocess
    try:
        git_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        git_available = True
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_available = False
        git_root = None

    llms = []
    # LLM_PROVIDER flag: "deepseek" | "openai" | "anthropic" (default). Gates cardiologist provider.
    _provider = os.environ.get("LLM_PROVIDER", "anthropic")
    _has_openai = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("Context_DNA_OPENAI"))
    _has_deepseek = bool(
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("Context_DNA_Deep_Seek")
        or os.environ.get("Context_DNA_Deepseek")
    )
    if _provider == "deepseek" and _has_deepseek:
        llms.append("cardiologist-deepseek")
    elif _has_openai:
        llms.append("cardiologist-gpt4")
    elif _has_deepseek:
        # Fallback: deepseek available even if flag unset
        llms.append("cardiologist-deepseek")
    if _has_deepseek:
        llms.append("neurologist-deepseek")
    if os.environ.get("ANTHROPIC_API_KEY"):
        llms.append("atlas-claude")

    state = None
    try:
        import redis
        rc = redis.Redis()
        rc.ping()
        state = rc
    except Exception:
        state = "memory"

    return RuntimeContext(
        healthy_llms=llms, state=state, evidence=state,
        git_available=git_available, git_root=git_root,
    )


def cmd_presets(args):
    print("Chain Presets:")
    print("=" * 50)
    for name, segments in PRESETS.items():
        print(f"\n  {name} ({len(segments)} segments):")
        for i, seg in enumerate(segments, 1):
            registered = "✓" if seg in SEGMENT_REGISTRY else "○"
            print(f"    {i}. [{registered}] {seg}")
    print(f"\n✓ = registered, ○ = not yet implemented")
    print(f"\nRegistered segments: {len(SEGMENT_REGISTRY)}")


def cmd_suggest(args):
    ma = ModeAuthority()
    suggestion = ma.suggest(trigger=args.trigger)
    if suggestion:
        print(f"Suggestion: {suggestion.message}")
        print(f"  Mode: {suggestion.mode}")
        print(f"  Segments: {', '.join(PRESETS.get(suggestion.mode, []))}")
    else:
        print(f"No suggestion for trigger '{args.trigger}'")


def cmd_run(args):
    ma = ModeAuthority()
    try:
        segments = ma.resolve(args.mode)
    except KeyError:
        print(f"Error: Unknown preset '{args.mode}'")
        print(f"Available: {', '.join(PRESETS.keys())}")
        sys.exit(1)

    ctx = _detect_runtime_context()
    executor = ChainExecutor(halt_on_error=False)

    print(f"Chain: {args.mode} ({len(segments)} segments)")
    print(f"Topic: {args.topic}")
    print(f"LLMs detected: {', '.join(ctx.healthy_llms) or 'none'}")
    print(f"Git: {'yes' if ctx.git_available else 'no'}")
    print(f"State: {'redis' if ctx.state and ctx.state != 'memory' else 'memory'}")
    print("=" * 50)

    state = executor.run(segments, ctx, initial_data={"topic": args.topic})

    total_ms = sum(state.segment_times_ns.values()) / 1_000_000
    rec = ExecutionRecord.create(
        chain_id=args.mode,
        segments_run=[s for s in segments if s not in [sk[0] for sk in state.skipped]],
        segments_skipped=[sk[0] for sk in state.skipped],
        success=not state.halted and len(state.errors) == 0,
        duration_ms=total_ms,
        duration_by_segment={k: v / 1_000_000 for k, v in state.segment_times_ns.items()},
        project_id=ctx.git_root or "unknown",
        failed_segment=state.halt_reason.split(":")[0] if state.halted else None,
    )
    _telemetry.record(rec)

    print(f"\nResults:")
    if state.segment_results:
        for name, result in state.segment_results.items():
            print(f"  ✓ {name}: {result}")
    if state.skipped:
        print(f"\nSkipped:")
        for name, reason in state.skipped:
            print(f"  ○ {name}: {reason}")
    if state.degraded:
        print(f"\nDegraded:")
        for name, notes in state.degraded:
            print(f"  ⚠ {name}: {', '.join(notes)}")
    if state.errors:
        print(f"\nErrors:")
        for err in state.errors:
            print(f"  ✗ {err}")
    if state.halted:
        print(f"\n⛔ Halted: {state.halt_reason}")

    print(f"\nDuration: {total_ms:.1f}ms")
    print(f"Execution ID: {rec.execution_id}")


def cmd_history(args):
    chain_id = getattr(args, "chain_id", None)
    if chain_id:
        records = _telemetry.recent_executions(chain_id)
    else:
        records = []
        for cid, recs in _telemetry._records.items():
            records.extend(recs)
        records.sort(key=lambda r: r.timestamp, reverse=True)

    if not records:
        print("No execution history (in-memory store — resets on restart)")
        return

    print(f"Recent Executions ({len(records)}):")
    print("=" * 50)
    for rec in records[:20]:
        status = "✓" if rec.success else "✗"
        print(f"  [{status}] {rec.chain_id} — {rec.duration_ms:.0f}ms — {rec.timestamp[:19]}")
        print(f"      Ran: {', '.join(rec.segments_run)}")
        if rec.segments_skipped:
            print(f"      Skipped: {', '.join(rec.segments_skipped)}")


def cmd_telemetry(args):
    chain_id = getattr(args, "chain_id", None)
    chain_ids = [chain_id] if chain_id else list(_telemetry._records.keys())

    if not chain_ids:
        print("No telemetry data (in-memory store — resets on restart)")
        return

    for cid in chain_ids:
        records = _telemetry.recent_executions(cid)
        patterns = _telemetry.detect_patterns(cid)
        print(f"\nChain: {cid}")
        print(f"  Executions: {len(records)}")
        if records:
            avg_ms = sum(r.duration_ms for r in records) / len(records)
            success_rate = sum(1 for r in records if r.success) / len(records)
            print(f"  Avg duration: {avg_ms:.0f}ms")
            print(f"  Success rate: {success_rate:.0%}")
        if patterns:
            print(f"  Detected patterns:")
            for p in patterns:
                print(f"    [{p.grade.value}] {', '.join(p.segments)} "
                      f"(freq={p.frequency:.0%}, n={p.observations})")
        else:
            print(f"  No patterns detected yet")


def main():
    parser = argparse.ArgumentParser(description="Chain Orchestration CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("presets", help="List available chain presets")

    suggest_p = subparsers.add_parser("suggest", help="Get mode suggestion")
    suggest_p.add_argument("--trigger", required=True)

    run_p = subparsers.add_parser("run", help="Execute a chain preset")
    run_p.add_argument("mode", help="Preset name")
    run_p.add_argument("--topic", default="", help="Topic/context")

    history_p = subparsers.add_parser("history", help="Show execution history")
    history_p.add_argument("--chain-id", default=None)

    telemetry_p = subparsers.add_parser("telemetry", help="Show telemetry")
    telemetry_p.add_argument("--chain-id", default=None)

    args = parser.parse_args()
    {"presets": cmd_presets, "suggest": cmd_suggest, "run": cmd_run,
     "history": cmd_history, "telemetry": cmd_telemetry}[args.command](args)


if __name__ == "__main__":
    main()
