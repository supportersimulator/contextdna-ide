#!/usr/bin/env python3
"""North Star priority vector CLI — TBG seed for directional truth scoring.

Per docs/plans/2026-04-25-tbg-north-star-architecture.md, this CLI surfaces
Aaron's locked-in priority vector and gates work to advance it. Future commits
extend this with persistent SQLite storage; this seed CLI is intentionally
small (no DB required) and operates from an in-source priority list so it can
be invoked from `gains-gate.sh` and `3s` consults without bootstrap cost.

Subcommands:
    show           Print the priority vector + brief.
    classify TXT   Map a task description to its likely priority slot.
    drift-check    Scan recent git commits and flag those outside the 5 slots.

Exit codes:
    0  success / no drift detected
    1  drift detected (drift-check) or unknown command
    2  invalid input

This module follows the Zero Silent Failures invariant — every classification
miss is reported, never swallowed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Locked priority vector — order is INVARIANT per Aaron's lock-in sequence.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Priority:
    rank: int
    name: str
    slug: str
    brief: str
    keywords: tuple[str, ...]
    paths: tuple[str, ...] = field(default_factory=tuple)


PRIORITIES: tuple[Priority, ...] = (
    Priority(
        rank=1,
        name="Multi-Fleet",
        slug="multi-fleet",
        brief="Coordinated fleet daemons, NATS bus, chief relay, audit pipeline.",
        keywords=(
            "fleet", "multi-fleet", "multifleet", "nats", "chief", "node",
            "audit", "fleet-state", "fleet-check", "discord", "relay",
            "inbox", "seed-file", "wake-on-lan", "corrigibility-ledger",
        ),
        paths=("multi-fleet/", ".fleet-messages/", "tools/fleet_nerve_nats.py"),
    ),
    Priority(
        rank=2,
        name="3-Surgeons",
        slug="3-surgeons",
        brief="3-distinct-LLM consensus: Atlas + Cardiologist + Neurologist.",
        keywords=(
            "3-surgeons", "3s", "surgeon", "cardiologist", "neurologist",
            "consensus", "cross-examine", "ab-test", "ab_test", "consult",
            "deepseek", "qwen", "cardio", "surgery", "cap_ab", "sentinel",
        ),
        paths=("3-surgeons/", "memory/surgery_bridge.py"),
    ),
    Priority(
        rank=3,
        name="ContextDNA IDE",
        slug="contextdna-ide",
        brief="Context DNA capture, webhook, IDE event bus, panels.",
        keywords=(
            "contextdna", "context-dna", "context_dna", "webhook", "ide",
            "panel", "panels", "bridge", "event-bus", "capture", "injection",
            "supervisor", "claude-bridge", "anthropic-compat",
        ),
        paths=("context-dna/", "contextdna/", "ContextDNASupervisor/"),
    ),
    Priority(
        rank=4,
        name="Full Local Ops",
        slug="full-local-ops",
        brief="Local-first inference, MLX/DeepSeek routing, gains-gate, memory.",
        keywords=(
            "local", "mlx", "llm", "priority-queue", "llm_priority",
            "gains-gate", "gains_gate", "memory", "synaptic", "professor",
            "brain", "wal", "wisdom", "cardio-gate", "corrigibility-gate",
            "session_gold", "historian", "rehydrate",
        ),
        paths=("memory/", "scripts/gains-gate.sh", "scripts/cardio-gate.sh"),
    ),
    Priority(
        rank=5,
        name="ER Simulator",
        slug="er-simulator",
        brief="Event-driven audio, ECG, medical accuracy, sub-1% CPU.",
        keywords=(
            "er-simulator", "ersim", "ecg", "_ecg", "vitals", "patient",
            "shift", "trades", "scenario", "telemetry-audio", "ersimulator",
            "sim-frontend", "simulator-core", "ersim-voice",
        ),
        paths=(
            "ersim-voice-stack/", "shift-trades/", "sim-frontend/",
            "simulator-core/",
        ),
    ),
)


def find_priority(slug_or_name: str) -> Priority | None:
    needle = slug_or_name.strip().lower()
    for p in PRIORITIES:
        if p.slug == needle or p.name.lower() == needle:
            return p
    return None


# ---------------------------------------------------------------------------
# Classification (keyword-overlap scoring — A-axis seed, no LLM call)
# ---------------------------------------------------------------------------

@dataclass
class ClassifyResult:
    priority: Priority | None
    score: int
    matched: tuple[str, ...]
    runner_up: Priority | None
    runner_up_score: int

    def to_dict(self) -> dict:
        return {
            "priority": self.priority.name if self.priority else None,
            "slug": self.priority.slug if self.priority else None,
            "rank": self.priority.rank if self.priority else None,
            "score": self.score,
            "matched_keywords": list(self.matched),
            "runner_up": self.runner_up.name if self.runner_up else None,
            "runner_up_score": self.runner_up_score,
        }


_TOKEN_RE = re.compile(r"[a-z0-9_\-]+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def classify(text: str) -> ClassifyResult:
    """Map free-form task text to a priority slot via keyword overlap.

    Returns a ClassifyResult. priority=None if no keywords matched (drift).
    Ties are broken by lower rank (higher priority wins).
    """
    if not text or not text.strip():
        return ClassifyResult(None, 0, (), None, 0)

    tokens = _tokenize(text)
    scored: list[tuple[int, Priority, tuple[str, ...]]] = []
    for p in PRIORITIES:
        matched = tuple(sorted(k for k in p.keywords if k in tokens))
        # Also allow exact-substring path hits (e.g. "multi-fleet/foo.py").
        path_hits = tuple(sorted(
            path.rstrip("/").lower() for path in p.paths
            if path.rstrip("/").lower() in text.lower()
        ))
        all_matches = tuple(sorted(set(matched) | set(path_hits)))
        score = len(matched) + len(path_hits)
        if score > 0:
            scored.append((score, p, all_matches))

    if not scored:
        return ClassifyResult(None, 0, (), None, 0)

    # Highest score wins; tie-break by lower rank (more important).
    scored.sort(key=lambda row: (-row[0], row[1].rank))
    best_score, best_priority, best_matches = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    return ClassifyResult(
        priority=best_priority,
        score=best_score,
        matched=best_matches,
        runner_up=runner_up[1] if runner_up else None,
        runner_up_score=runner_up[0] if runner_up else 0,
    )


# ---------------------------------------------------------------------------
# Drift check — scan recent git commits for orphan work
# ---------------------------------------------------------------------------

@dataclass
class DriftCommit:
    sha: str
    subject: str
    date: str
    classification: ClassifyResult


def _git_log(repo: Path, since_days: int) -> list[tuple[str, str, str]]:
    """Return [(sha, iso-date, subject)] for commits in the last `since_days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cmd = [
        "git", "-C", str(repo),
        "log", f"--since={since}",
        "--pretty=format:%H%x09%cI%x09%s",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        # Zero silent failures: surface git breakage to the caller.
        raise RuntimeError(f"git log failed: {exc.stderr.strip()}") from exc

    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def drift_check(
    repo: Path | str | None = None,
    since_days: int = 7,
    log_rows: Sequence[tuple[str, str, str]] | None = None,
) -> list[DriftCommit]:
    """Return commits in `since_days` that don't classify into any priority.

    Pass `log_rows` to inject pre-collected git log output for testing.
    """
    if log_rows is None:
        repo_path = Path(repo or _default_repo()).resolve()
        log_rows = _git_log(repo_path, since_days)

    drift: list[DriftCommit] = []
    for sha, date, subject in log_rows:
        result = classify(subject)
        if result.priority is None:
            drift.append(DriftCommit(sha[:12], subject, date, result))
    return drift


def _default_repo() -> Path:
    """Walk up from this file to find the repo root (contains .git)."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_show(as_json: bool = False) -> int:
    if as_json:
        payload = [
            {"rank": p.rank, "name": p.name, "slug": p.slug, "brief": p.brief}
            for p in PRIORITIES
        ]
        print(json.dumps(payload, indent=2))
        return 0

    print("North Star priority vector — locked sequence (INVARIANT)")
    print("=" * 64)
    for p in PRIORITIES:
        print(f"  {p.rank}. {p.name:<18} [{p.slug}]")
        print(f"     {p.brief}")
    print()
    print("Source: docs/plans/2026-04-25-tbg-north-star-architecture.md")
    return 0


def _print_classify(text: str, as_json: bool) -> int:
    result = classify(text)
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.priority else 1

    if result.priority is None:
        print(f"DRIFT: '{text}' did not match any priority slot.")
        print("  Either rephrase to align with a North Star priority,")
        print("  or accept this is exploratory work outside the locked sequence.")
        return 1

    print(f"Best match: {result.priority.rank}. {result.priority.name}")
    print(f"  slug:    {result.priority.slug}")
    print(f"  score:   {result.score}")
    print(f"  matched: {', '.join(result.matched) or '(none)'}")
    if result.runner_up:
        print(
            f"  runner-up: {result.runner_up.name} "
            f"(score {result.runner_up_score})"
        )
    return 0


def _print_drift(since_days: int, as_json: bool, repo: Path | None) -> int:
    try:
        drift = drift_check(repo=repo, since_days=since_days)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(
            [
                {
                    "sha": d.sha,
                    "date": d.date,
                    "subject": d.subject,
                }
                for d in drift
            ],
            indent=2,
        ))
        return 1 if drift else 0

    if not drift:
        print(f"OK: no drift in last {since_days} day(s). All commits map to a priority.")
        return 0

    print(f"DRIFT: {len(drift)} commit(s) in last {since_days} day(s) did not classify.")
    print("-" * 64)
    for d in drift:
        print(f"  {d.sha}  {d.date}  {d.subject}")
    print()
    print("Action: rephrase commit subjects to reference a priority,")
    print("        or confirm the work advances a locked priority.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="north_star.py",
        description="North Star priority vector CLI (TBG seed).",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("show", help="print the locked priority vector")

    p_classify = sub.add_parser("classify", help="map a task description to a slot")
    p_classify.add_argument("text", nargs="+", help="task description")

    p_drift = sub.add_parser("drift-check", help="flag recent commits outside the vector")
    p_drift.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    p_drift.add_argument("--repo", default=None, help="repo path (default: auto-detect)")

    args = parser.parse_args(argv)

    if args.cmd in (None, "show"):
        return _print_show(as_json=args.json)
    if args.cmd == "classify":
        return _print_classify(" ".join(args.text), as_json=args.json)
    if args.cmd == "drift-check":
        repo = Path(args.repo).resolve() if args.repo else None
        return _print_drift(since_days=args.days, as_json=args.json, repo=repo)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
