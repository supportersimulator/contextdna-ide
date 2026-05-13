#!/usr/bin/env python3
"""
RR5 — Superpowers x 3-surgeons E2E autonomy classifier.

Two subcommands:
  filter   : reads SKILL.md paths from stdin, emits TSV
             (path, name, plugin, prompt) for brainstorming-class skills.
  classify : reads a 3s consensus stdout blob from stdin and emits
             a one-line verdict + score + cost as TSV.

The classifier is intentionally simple and explicit — we want to
expose, not paper over, the autonomy boundary of these skills.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

# Words in the description that MARK a skill as "brainstorming-class"
# (it asks 3s to think/decide/compare).
BRAINSTORM_HINTS = (
    "decide", "choose", "compare", "trade-off", "tradeoff",
    "approach", "design", "plan", "explore", "options",
    "before any creative work", "creative work",
    "before implementation", "before touching code",
    "before writing implementation", "before proposing fixes",
    "before committing", "before merging", "before starting",
    "decision", "verify work meets requirements",
    "cross-examination", "review", "before any response",
    "before any other response", "structured options",
    "integrate the work",
)

# Words that mark a skill as PROCEDURAL/TOOL/RIGID — skip.
# Be careful: "implement" appears inside "before implementing", which IS
# a decision skill. Use word-boundary phrases only.
SKIP_HINTS = (
    "browser control", "use the chrome",
    "send or read slack messages",
    "run interactive cli tools", "tmux",
    "use mcp servers", "mcp cli tool",
    "auditing a codebase for semantic duplication",
    "google apps", "configure the claude code harness",
    "rebind", "scan your transcripts",
    "demonstrating plugin workflow",
    # writing-clearly-and-concisely is style enforcement, not a decision.
    "strunk",
)


def parse_skill(path: Path) -> Optional[dict]:
    """Return {name, plugin, description} or None on failure."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    m = re.search(
        r"^---\s*\n(.*?)\n---",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not m:
        return None
    fm = m.group(1)

    name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    if not name_m or not desc_m:
        return None

    name = name_m.group(1).strip().strip('"').strip("'")
    description = desc_m.group(1).strip().strip('"').strip("'")

    # Plugin = third-from-last component:
    #   .../<plugin>/<version>/skills/<name>/SKILL.md
    parts = path.parts
    plugin = parts[-5] if len(parts) >= 5 else "unknown"

    return {
        "name": name,
        "plugin": plugin,
        "description": description,
        "path": str(path),
    }


def is_brainstormy(desc: str) -> bool:
    low = desc.lower()
    if any(skip in low for skip in SKIP_HINTS):
        return False
    return any(hint in low for hint in BRAINSTORM_HINTS)


def make_prompt(skill: dict) -> str:
    """
    Generate a representative *consensus claim* for a skill.

    Honest principle: the claim mirrors the skill's stated decision —
    if the skill is vague, the claim will be vague. That's the point.
    The claim must be answerable yes/no by 3s.
    """
    name = skill["name"]
    desc = skill["description"]
    low = desc.lower()

    # Hand-tuned for the well-known superpowers brainstorming-class skills.
    # Falls back to a generic claim derived from description.
    table = {
        "brainstorming": (
            "For a multi-tenant FastAPI service that needs an audit log, "
            "the better first design is an append-only Postgres table with "
            "a JSONB payload column rather than emitting events to Kafka."
        ),
        "writing-plans": (
            "When given an ambiguous spec for a multi-step CLI tool, "
            "writing a plan with explicit phases and acceptance criteria "
            "produces a better outcome than starting to code immediately."
        ),
        "executing-plans": (
            "When executing a written implementation plan with review "
            "checkpoints, completing each phase fully before starting the "
            "next yields a more reliable outcome than parallelising phases."
        ),
        "systematic-debugging": (
            "When a flaky integration test fails 1 in 20 runs, the higher-"
            "value first move is to capture full logs + reproduce locally "
            "rather than to add a retry."
        ),
        "verification-before-completion": (
            "Before claiming an implementation task is complete, running "
            "the project's full test suite is a stronger verification "
            "signal than self-review of the diff."
        ),
        "requesting-code-review": (
            "For a 600-line refactor PR, asking a peer reviewer to focus "
            "on the public API surface first yields more useful feedback "
            "than asking for a line-by-line review."
        ),
        "receiving-code-review": (
            "When a code reviewer asks you to extract a helper that you "
            "believe is premature abstraction, the right response is to "
            "push back with a concrete reason rather than silently comply."
        ),
        "writing-skills": (
            "A skill's SKILL.md description should state the trigger "
            "condition (when to use it) before the capability (what it "
            "does), because routing agents read the description first."
        ),
        "using-git-worktrees": (
            "For executing an isolated implementation plan that touches "
            "many files, a git worktree is a better workspace choice than "
            "a feature branch in the main checkout."
        ),
        "dispatching-parallel-agents": (
            "When given 3 fully independent refactor tasks across "
            "different files, dispatching 3 parallel agents finishes "
            "faster than running them sequentially."
        ),
        "subagent-driven-development": (
            "For a feature with 4+ independent tasks, subagent-driven "
            "development produces better results than a single agent "
            "doing all tasks sequentially."
        ),
        "finishing-a-development-branch": (
            "When implementation is complete and tests pass, opening a "
            "PR for human review is a safer integration choice than "
            "merging directly to main."
        ),
        "test-driven-development": (
            "For a new pure-function utility, writing tests before the "
            "implementation produces fewer post-merge bugs than writing "
            "tests after."
        ),
        "using-superpowers": (
            "Before answering any non-trivial user request, an agent "
            "should consult the available-skills list rather than relying "
            "on cached training-time knowledge."
        ),
        "driving-claude-code-sessions": (
            "For coordinating 5+ parallel work items, a project-manager "
            "session that delegates is more effective than one session "
            "doing everything inline."
        ),
        "remembering-conversations": (
            "When stuck on a workflow you've solved before, searching "
            "past conversations is more efficient than re-deriving the "
            "solution from scratch."
        ),
        "developing-claude-code-plugins": (
            "When building a Claude Code plugin, structuring it as "
            "skills + a manifest is more maintainable than a single "
            "monolithic SKILL.md."
        ),
        "working-with-claude-code": (
            "When configuring a Claude Code automated behavior (e.g. "
            '"after every commit, run X"), settings.json hooks are the '
            "correct mechanism rather than memory/preferences files."
        ),
        "browsing": (
            "For automating form submission across multiple tabs, "
            "controlling an existing Chrome via DevTools Protocol is "
            "more reliable than spawning a fresh headless browser."
        ),
    }

    if name in table:
        return table[name]

    # Generic fallback — synthesise a binary claim from the description.
    snippet = desc.split(" - ")[0].split(".")[0][:160]
    return (
        f"For the workflow described as: '{snippet}', invoking this "
        f"skill produces a better outcome than skipping it."
    )


# ---------------------------------------------------------------------------
# 3s consensus output classifier
# ---------------------------------------------------------------------------

SCORE_RE = re.compile(r"Weighted score:\s*([+-]?[0-9.]+)")
COST_RE = re.compile(r"Total cost:\s*\$([0-9.]+)")
CARDIO_RE = re.compile(
    r"Cardiologist:\s*(\S+)\s*\(confidence=([0-9.]+)\)",
    re.IGNORECASE,
)
NEURO_RE = re.compile(
    r"Neurologist:\s*(\S+)\s*\(confidence=([0-9.]+)\)",
    re.IGNORECASE,
)

# TT1 counter-probe fields (only present when --counter-probe is on).
CP_NEG_SCORE_RE = re.compile(
    r"Counter-probe negation score:\s*([+-]?[0-9.]+)"
)
CP_COST_RE = re.compile(r"Counter-probe cost:\s*\$([0-9.]+)")
CP_VERDICT_RE = re.compile(r"^\s*Verdict:\s*([A-Z\-]+)", re.MULTILINE)


def classify(blob: str) -> dict:
    """
    Map a 3s consensus stdout into a verdict.

    Verdict rules:
      AUTONOMOUS  — at least one surgeon agreed/disagreed with conf >= 0.7
                    AND |weighted| >= 0.5  (real signal, no human needed)
      NEEDS-HUMAN — both surgeons returned but |weighted| < 0.5
                    OR one abstained / low-confidence (mixed signal)
      FAILED      — both surgeons unavailable or no parse
    """
    score_m = SCORE_RE.search(blob)
    cost_m = COST_RE.search(blob)
    cardio_m = CARDIO_RE.search(blob)
    neuro_m = NEURO_RE.search(blob)

    score = float(score_m.group(1)) if score_m else 0.0
    cost = float(cost_m.group(1)) if cost_m else 0.0

    cardio_verdict = cardio_m.group(1).lower() if cardio_m else "missing"
    cardio_conf = float(cardio_m.group(2)) if cardio_m else 0.0
    neuro_verdict = neuro_m.group(1).lower() if neuro_m else "missing"
    neuro_conf = float(neuro_m.group(2)) if neuro_m else 0.0

    cardio_live = cardio_verdict not in ("unavailable", "missing")
    neuro_live = neuro_verdict not in ("unavailable", "missing")

    if not cardio_live and not neuro_live:
        verdict = "FAILED"
    elif abs(score) >= 0.5 and (
        (cardio_live and cardio_conf >= 0.7)
        or (neuro_live and neuro_conf >= 0.7)
    ):
        verdict = "AUTONOMOUS"
    else:
        verdict = "NEEDS-HUMAN"

    return {
        "verdict": verdict,
        "score": score,
        "cost": cost,
        "cardio": f"{cardio_verdict}@{cardio_conf:.2f}",
        "neuro": f"{neuro_verdict}@{neuro_conf:.2f}",
    }


def classify_cp(blob: str) -> dict:
    """
    WW5 — TT1-aware classifier.

    Honors the engine's authoritative `Verdict:` line emitted by
    `3s consensus --counter-probe`. Maps engine verdicts to demo verdicts:

      GENUINE              -> AUTONOMOUS (real reasoning + counter-probe flipped)
      PARTIAL              -> NEEDS-HUMAN (only one surgeon flipped)
      NO-GENUINE-CONSENSUS -> NEEDS-HUMAN (sycophantic — both agreed both ways)
      NO-SIGNAL            -> NEEDS-HUMAN (weighted score too low)

    Falls back to the same liveness check as classify() for FAILED.
    Cost combines pos pass + counter-probe cost.
    """
    score_m = SCORE_RE.search(blob)
    cost_m = COST_RE.search(blob)
    cardio_m = CARDIO_RE.search(blob)
    neuro_m = NEURO_RE.search(blob)
    cp_score_m = CP_NEG_SCORE_RE.search(blob)
    cp_cost_m = CP_COST_RE.search(blob)
    cp_verdict_m = CP_VERDICT_RE.search(blob)

    score = float(score_m.group(1)) if score_m else 0.0
    cost = float(cost_m.group(1)) if cost_m else 0.0
    cp_score = float(cp_score_m.group(1)) if cp_score_m else 0.0
    cp_cost = float(cp_cost_m.group(1)) if cp_cost_m else 0.0

    cardio_verdict = cardio_m.group(1).lower() if cardio_m else "missing"
    cardio_conf = float(cardio_m.group(2)) if cardio_m else 0.0
    neuro_verdict = neuro_m.group(1).lower() if neuro_m else "missing"
    neuro_conf = float(neuro_m.group(2)) if neuro_m else 0.0

    cardio_live = cardio_verdict not in ("unavailable", "missing")
    neuro_live = neuro_verdict not in ("unavailable", "missing")

    engine_verdict = (
        cp_verdict_m.group(1).upper().strip() if cp_verdict_m else ""
    )

    if not cardio_live and not neuro_live:
        verdict = "FAILED"
    elif engine_verdict == "GENUINE":
        verdict = "AUTONOMOUS"
    elif engine_verdict in (
        "NO-GENUINE-CONSENSUS",
        "PARTIAL",
        "NO-SIGNAL",
    ):
        verdict = "NEEDS-HUMAN"
    else:
        # Engine didn't emit a verdict line — fall back to baseline rule.
        if abs(score) >= 0.5 and (
            (cardio_live and cardio_conf >= 0.7)
            or (neuro_live and neuro_conf >= 0.7)
        ):
            verdict = "AUTONOMOUS"
        else:
            verdict = "NEEDS-HUMAN"

    combined_cost = cost + cp_cost

    return {
        "verdict": verdict,
        "score": score,
        "neg_score": cp_score,
        "cost": combined_cost,
        "cardio": f"{cardio_verdict}@{cardio_conf:.2f}",
        "neuro": f"{neuro_verdict}@{neuro_conf:.2f}",
        "engine_verdict": engine_verdict or "MISSING",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_filter() -> int:
    rows: list[dict] = []
    for line in sys.stdin:
        p = line.strip()
        if not p:
            continue
        skill = parse_skill(Path(p))
        if not skill:
            continue
        if not is_brainstormy(skill["description"]):
            continue
        skill["prompt"] = make_prompt(skill)
        rows.append(skill)

    # Stable order: plugin then name.
    rows.sort(key=lambda r: (r["plugin"], r["name"]))
    for r in rows:
        # TSV — bash splits cleanly with IFS=$'\t'.
        # Replace tabs/newlines defensively.
        prompt = r["prompt"].replace("\t", " ").replace("\n", " ")
        print(f"{r['path']}\t{r['name']}\t{r['plugin']}\t{prompt}")
    return 0


def cmd_classify() -> int:
    blob = sys.stdin.read()
    result = classify(blob)
    print(
        f"{result['verdict']}\t{result['score']:.2f}\t"
        f"{result['cost']:.4f}\t{result['cardio']}\t{result['neuro']}"
    )
    return 0


def cmd_classify_cp() -> int:
    """TT1-aware classify: single counter-probe pass, engine verdict wins."""
    blob = sys.stdin.read()
    result = classify_cp(blob)
    # TSV: verdict, pos_score, neg_score, combined_cost, cardio, neuro, engine
    print(
        f"{result['verdict']}\t{result['score']:.2f}\t"
        f"{result['neg_score']:.2f}\t{result['cost']:.4f}\t"
        f"{result['cardio']}\t{result['neuro']}\t{result['engine_verdict']}"
    )
    return 0


def main(argv: list[str]) -> int:
    valid = ("filter", "classify", "classify-cp")
    if len(argv) < 2 or argv[1] not in valid:
        print(
            "usage: demo_superpowers_3s_classify.py "
            "{filter|classify|classify-cp}",
            file=sys.stderr,
        )
        return 2
    if argv[1] == "filter":
        return cmd_filter()
    if argv[1] == "classify-cp":
        return cmd_classify_cp()
    return cmd_classify()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
