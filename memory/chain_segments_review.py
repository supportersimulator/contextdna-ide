"""Review chain segments — plan-review and pre-implementation review.

plan-review: Evaluates strategic alignment of plans against vision/reflect/dao docs.
pre-impl: Gate check before implementation begins.
"""
from __future__ import annotations

import os
import re

from memory.chain_engine import segment
from memory.chain_requirements import CommandRequirements


@segment(
    name="plan-review",
    requires=CommandRequirements(needs_git=True),
    tags=["review", "strategic", "alignment"],
)
def seg_plan_review(ctx, data: dict) -> dict:
    """Evaluate a plan's strategic alignment against vision and reflect docs.

    Reads from vision/ for strategic direction, reflect/ for current state,
    and dao/ for idealized patterns. Produces a verdict.
    """
    topic = data.get("topic", "")
    plan_content = data.get("plan_content", "")

    # Collect strategic context from 4-folder system
    strategic_context = {}
    alignment_signals = []
    misalignment_signals = []

    folder_purposes = {
        "docs/vision": "strategic direction",
        "docs/reflect": "current state snapshot",
        "docs/dao": "idealized proven state",
        "docs/inbox": "raw unprocessed items",
    }

    for folder, purpose in folder_purposes.items():
        full_path = os.path.join(ctx.git_root or ".", folder)
        if not os.path.isdir(full_path):
            continue

        docs = []
        for fname in os.listdir(full_path):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(full_path, fname)
            try:
                with open(fpath, "r", errors="ignore") as f:
                    content = f.read(2048)
                docs.append({"file": fname, "preview": content[:500]})
            except Exception:
                continue

        strategic_context[folder] = {
            "purpose": purpose,
            "doc_count": len(docs),
            "docs": docs[:5],  # Top 5 per folder
        }

    # Analyze plan content or topic for alignment signals
    analysis_text = (plan_content or topic).lower()

    # Check for alignment with vision docs
    if strategic_context.get("docs/vision", {}).get("doc_count", 0) > 0:
        alignment_signals.append("Vision docs exist — can verify strategic alignment")

    # Check for reflect/dao gap awareness
    reflect_count = strategic_context.get("docs/reflect", {}).get("doc_count", 0)
    dao_count = strategic_context.get("docs/dao", {}).get("doc_count", 0)
    if reflect_count > 0 and dao_count > 0:
        alignment_signals.append("Both reflect/ and dao/ populated — gap analysis possible")
    elif reflect_count == 0 and dao_count == 0:
        misalignment_signals.append("No reflect/ or dao/ docs — cannot verify ground truth")

    # Check for prerequisite awareness
    prerequisite_words = ["depends on", "requires", "after", "prerequisite", "blocked by"]
    has_prerequisites = any(w in analysis_text for w in prerequisite_words)
    if has_prerequisites:
        alignment_signals.append("Plan acknowledges prerequisites")

    # Check for scope signals
    scope_words = ["refactor", "rewrite", "new subsystem", "architecture", "redesign"]
    is_large_scope = any(w in analysis_text for w in scope_words)
    if is_large_scope:
        misalignment_signals.append("Large scope detected — verify this aligns with current priorities")

    # Determine verdict
    if len(misalignment_signals) > len(alignment_signals) and len(misalignment_signals) > 1:
        verdict = "MISALIGNED"
    elif is_large_scope and reflect_count == 0:
        verdict = "MISSING_PREREQUISITE"
    elif not plan_content and not topic:
        verdict = "INSUFFICIENT_CONTEXT"
    else:
        verdict = "ALIGNED"

    return {
        "plan_review_verdict": verdict,
        "plan_review_alignment_signals": alignment_signals,
        "plan_review_misalignment_signals": misalignment_signals,
        "plan_review_strategic_context": {
            folder: {"purpose": info["purpose"], "doc_count": info["doc_count"]}
            for folder, info in strategic_context.items()
        },
        "plan_review_recommendation": {
            "ALIGNED": "Proceed with implementation",
            "MISALIGNED": "Review plan against vision docs before proceeding",
            "PREMATURE": "Address prerequisites first",
            "REDUNDANT": "Check if this duplicates existing work",
            "MISSING_PREREQUISITE": "Populate reflect/ docs to establish ground truth first",
            "INSUFFICIENT_CONTEXT": "Provide plan content or topic for meaningful review",
        }.get(verdict, "Review manually"),
    }


@segment(
    name="pre-impl",
    requires=CommandRequirements(),
    tags=["review", "gate"],
)
def seg_pre_impl(ctx, data: dict) -> dict:
    """Pre-implementation gate — check if work should proceed.

    Aggregates signals from prior segments (risk-scan, contradiction-scan,
    plan-review) to make a go/no-go recommendation.
    """
    blockers = []
    warnings = []

    # Check plan-review verdict
    verdict = data.get("plan_review_verdict", "ALIGNED")
    if verdict in ("MISALIGNED", "MISSING_PREREQUISITE"):
        blockers.append(f"Plan review verdict: {verdict}")
    elif verdict in ("PREMATURE", "REDUNDANT"):
        warnings.append(f"Plan review verdict: {verdict}")

    # Check risk level
    risk_level = data.get("risk_level", "low")
    if risk_level == "high":
        warnings.append(f"Risk level is HIGH — ensure adequate review")

    # Check contradictions
    contradictions = data.get("contradictions", [])
    if contradictions:
        blockers.append(f"{len(contradictions)} contradiction(s) found in docs")

    # Check gains-gate
    gains_pass = data.get("gains_gate_pass", True)
    if not gains_pass:
        blockers.append("Gains gate failed — infrastructure issues")

    # Determine proceed/block
    should_proceed = len(blockers) == 0

    return {
        "pre_impl_proceed": should_proceed,
        "pre_impl_blockers": blockers,
        "pre_impl_warnings": warnings,
        "pre_impl_summary": (
            "Clear to proceed" if should_proceed
            else f"BLOCKED: {'; '.join(blockers)}"
        ),
    }
