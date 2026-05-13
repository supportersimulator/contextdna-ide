"""Deep-audit chain segments — 5-phase document audit pipeline.

Decomposes the monolithic cmd_deep_audit into composable @segment functions.
Each segment reads from chain state and writes its results back.
"""
from __future__ import annotations

import json
import os
import glob as glob_mod

from memory.chain_engine import segment
from memory.chain_requirements import CommandRequirements


@segment(
    name="audit-discover",
    requires=CommandRequirements(needs_git=True),
    tags=["audit", "discovery"],
)
def seg_audit_discover(ctx, data: dict) -> dict:
    """Phase 1: Discover relevant documentation files by topic."""
    topic = data.get("topic", "")
    doc_dirs = ["docs/inbox", "docs/vision", "docs/reflect", "docs/dao", "docs/plans"]
    discovered = []

    for doc_dir in doc_dirs:
        full_dir = os.path.join(ctx.git_root or ".", doc_dir)
        if not os.path.isdir(full_dir):
            continue
        for fpath in glob_mod.glob(os.path.join(full_dir, "*.md")):
            fname = os.path.basename(fpath)
            rel_path = os.path.relpath(fpath, ctx.git_root or ".")
            # Score relevance by topic word matches in filename
            topic_words = [w.lower() for w in topic.split() if len(w) > 3]
            fname_lower = fname.lower()
            score = sum(1 for w in topic_words if w in fname_lower)
            discovered.append({
                "path": rel_path,
                "filename": fname,
                "relevance_score": score,
            })

    # Sort by relevance, take top 10
    discovered.sort(key=lambda d: d["relevance_score"], reverse=True)
    selected = discovered[:10]

    return {
        "audit_docs_discovered": len(discovered),
        "audit_docs_selected": selected,
    }


@segment(
    name="audit-read",
    requires=CommandRequirements(needs_git=True),
    tags=["audit", "extraction"],
)
def seg_audit_read(ctx, data: dict) -> dict:
    """Phase 2: Read selected documents and extract content."""
    selected = data.get("audit_docs_selected", [])
    doc_contents = {}
    total_chars = 0
    max_per_file = 8192
    max_total = 60000

    for doc in selected:
        if total_chars >= max_total:
            break
        fpath = os.path.join(ctx.git_root or ".", doc["path"])
        try:
            with open(fpath, "r", errors="ignore") as f:
                content = f.read(max_per_file)
            doc_contents[doc["path"]] = content
            total_chars += len(content)
        except Exception:
            continue

    return {
        "audit_doc_contents": doc_contents,
        "audit_docs_read": len(doc_contents),
        "audit_total_chars": total_chars,
    }


@segment(
    name="audit-extract",
    requires=CommandRequirements(min_llms=1, recommended_llms=2),
    tags=["audit", "llm", "extraction"],
)
def seg_audit_extract(ctx, data: dict) -> dict:
    """Phase 3: Extract planned/recommended features from documents.

    Requires LLM. In test mode, returns structured mock data.
    """
    doc_contents = data.get("audit_doc_contents", {})

    if data.get("_test_mode") or not doc_contents:
        # Test mode or no docs — return structured mock
        return {
            "audit_features": [
                {
                    "name": "example-feature",
                    "description": "Mock feature for testing",
                    "status": "PLANNED",
                    "source": "test",
                    "category": "test",
                    "priority": "medium",
                },
            ],
            "audit_extract_mode": "test",
        }

    # Real mode — build prompt for LLM
    doc_text = "\n\n---\n\n".join(
        f"## {path}\n{content[:4096]}"
        for path, content in list(doc_contents.items())[:5]
    )

    prompt = f"""Analyze these documents and extract all planned, recommended, or partially implemented features.

For each feature, provide:
- name: short identifier
- description: what it does
- status: PLANNED | RECOMMENDED | PARTIAL | UNKNOWN
- source: which document mentions it
- category: area (e.g., orchestration, testing, security)
- priority: high | medium | low

Return as JSON array.

Documents:
{doc_text}"""

    try:
        result = _call_llm(ctx, prompt)
        features = json.loads(result) if isinstance(result, str) else result
        return {
            "audit_features": features,
            "audit_extract_mode": "llm",
        }
    except Exception as e:
        return {
            "audit_features": [],
            "audit_extract_mode": f"error: {e}",
        }


@segment(
    name="audit-crosscheck",
    requires=CommandRequirements(min_llms=1, recommended_llms=2),
    tags=["audit", "llm", "verification"],
)
def seg_audit_crosscheck(ctx, data: dict) -> dict:
    """Phase 4: Cross-check features against codebase evidence.

    Requires LLM. In test mode, returns mock verdicts.
    """
    features = data.get("audit_features", [])

    if data.get("_test_mode") or not features:
        verdicts = []
        for f in features:
            verdicts.append({
                "feature": f.get("name", "unknown"),
                "verdict": "NOT_BUILT",
                "confidence": 0.5,
                "evidence": "test mode — no real analysis",
                "ab_candidate": True,
            })
        return {
            "audit_verdicts": verdicts,
            "audit_crosscheck_mode": "test",
            "audit_ab_candidates": [v for v in verdicts if v.get("ab_candidate")],
        }

    # Real mode — would call LLM with codebase context
    # For now, mark all as needing verification
    verdicts = []
    for f in features:
        verdicts.append({
            "feature": f.get("name", "unknown"),
            "verdict": "UNCERTAIN",
            "confidence": 0.3,
            "evidence": "Needs LLM cross-check with codebase",
            "ab_candidate": False,
        })

    return {
        "audit_verdicts": verdicts,
        "audit_crosscheck_mode": "basic",
        "audit_ab_candidates": [v for v in verdicts if v.get("ab_candidate")],
    }


@segment(
    name="audit-report",
    requires=CommandRequirements(),
    tags=["audit", "reporting"],
)
def seg_audit_report(ctx, data: dict) -> dict:
    """Phase 5: Format and summarize audit results."""
    features = data.get("audit_features", [])
    verdicts = data.get("audit_verdicts", [])
    ab_candidates = data.get("audit_ab_candidates", [])
    docs_read = data.get("audit_docs_read", 0)
    total_chars = data.get("audit_total_chars", 0)

    # Build summary
    status_counts = {}
    for f in features:
        s = f.get("status", "UNKNOWN")
        status_counts[s] = status_counts.get(s, 0) + 1

    verdict_counts = {}
    for v in verdicts:
        vd = v.get("verdict", "UNKNOWN")
        verdict_counts[vd] = verdict_counts.get(vd, 0) + 1

    summary = {
        "docs_read": docs_read,
        "total_chars_analyzed": total_chars,
        "features_found": len(features),
        "status_breakdown": status_counts,
        "verdict_breakdown": verdict_counts,
        "ab_candidates_count": len(ab_candidates),
    }

    return {
        "audit_summary": summary,
        "audit_complete": True,
    }


def _call_llm(ctx, prompt: str) -> str:
    """Attempt to call an available LLM via the priority queue.

    Routed via LLM priority queue for DeepSeek fallback (2026-04-19).
    The priority queue handles backend selection (local mlx_lm, DeepSeek,
    OpenAI fallback) so audit callers no longer need provider-specific
    logic. Raises on failure so the caller's `except Exception` path marks
    the segment as an error.
    """
    # Routed via LLM priority queue for DeepSeek fallback (2026-04-19)
    from memory.llm_priority_queue import llm_generate, Priority

    result = llm_generate(
        system_prompt="You are a documentation analyst. Return valid JSON only, no commentary.",
        user_prompt=prompt,
        priority=Priority.BACKGROUND,
        profile="deep",
        caller="chain_segments_audit._call_llm",
        timeout_s=60.0,
    )
    if not result:
        raise RuntimeError("LLM priority queue returned no result for audit extraction")
    return result
