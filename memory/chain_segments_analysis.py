"""Analysis chain segments — risk-scan and contradiction-scan.

Risk-scan evaluates complexity and risk signals in proposed changes.
Contradiction-scan checks if work contradicts existing docs/patterns.
"""
from __future__ import annotations

import os
import re
import subprocess

from memory.chain_engine import segment
from memory.chain_requirements import CommandRequirements


# Risk signal patterns — keywords that elevate risk level
RISK_SIGNALS = {
    "high": [
        r"\bauth\b", r"\bcrypto\b", r"\bsecret\b", r"\bpassword\b", r"\btoken\b",
        r"\bmigration\b", r"\bschema\b", r"\bdrop\b", r"\bdelete\b", r"\btruncate\b",
        r"\bproduction\b", r"\bdeploy\b", r"\bforce.push\b",
    ],
    "medium": [
        r"\bconfig\b", r"\byaml\b", r"\b\.env\b", r"\bapi\b", r"\bendpoint\b",
        r"\bdatabase\b", r"\bquery\b", r"\bindex\b", r"\bpermission\b",
    ],
}


def _scan_text_for_risk(text: str) -> tuple[str, list[str]]:
    """Scan text for risk signals. Returns (risk_level, matched_signals)."""
    text_lower = text.lower()
    matched = []

    for pattern in RISK_SIGNALS["high"]:
        if re.search(pattern, text_lower):
            matched.append(f"HIGH: {pattern}")

    for pattern in RISK_SIGNALS["medium"]:
        if re.search(pattern, text_lower):
            matched.append(f"MEDIUM: {pattern}")

    if any(m.startswith("HIGH:") for m in matched):
        return "high", matched
    elif any(m.startswith("MEDIUM:") for m in matched):
        return "medium", matched
    return "low", matched


@segment(
    name="risk-scan",
    requires=CommandRequirements(),
    tags=["analysis", "safety"],
)
def seg_risk_scan(ctx, data: dict) -> dict:
    """Evaluate risk level of proposed work based on topic and context."""
    topic = data.get("topic", "")
    risk_level, signals = _scan_text_for_risk(topic)

    # If we have git context, scan recent changes too
    changed_files = []
    if ctx.git_available:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=ctx.git_root,
                timeout=5,
            )
            changed_files = [f for f in result.stdout.strip().split("\n") if f]
            # Scan file paths for risk signals
            for f in changed_files:
                f_level, f_signals = _scan_text_for_risk(f)
                if f_level == "high" and risk_level != "high":
                    risk_level = "high"
                elif f_level == "medium" and risk_level == "low":
                    risk_level = "medium"
                signals.extend(f_signals)
        except Exception:
            pass

    # File count escalation
    if len(changed_files) > 10:
        risk_level = "high"
        signals.append(f"HIGH: {len(changed_files)} files changed (>10)")
    elif len(changed_files) > 3:
        if risk_level == "low":
            risk_level = "medium"
        signals.append(f"MEDIUM: {len(changed_files)} files changed (>3)")

    return {
        "risk_level": risk_level,
        "risk_signals": signals,
        "risk_files_changed": len(changed_files),
        "risk_escalation_mode": {
            "low": "Light",
            "medium": "Standard",
            "high": "Full",
        }.get(risk_level, "Standard"),
    }


@segment(
    name="contradiction-scan",
    requires=CommandRequirements(needs_git=True),
    tags=["analysis", "alignment"],
)
def seg_contradiction_scan(ctx, data: dict) -> dict:
    """Check if proposed work contradicts existing vision/reflect/dao docs."""
    topic = data.get("topic", "")
    contradictions = []
    docs_checked = 0

    if not topic:
        return {
            "contradictions": [],
            "contradiction_aligned": True,
            "contradiction_docs_checked": 0,
        }

    # Scan docs folders for potential contradictions
    doc_dirs = ["docs/inbox", "docs/vision", "docs/reflect", "docs/dao", "docs/plans"]
    topic_words = set(topic.lower().split())

    for doc_dir in doc_dirs:
        full_path = os.path.join(ctx.git_root or ".", doc_dir)
        if not os.path.isdir(full_path):
            continue

        for fname in os.listdir(full_path):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(full_path, fname)
            try:
                with open(fpath, "r", errors="ignore") as f:
                    content = f.read(4096)  # First 4KB only
                docs_checked += 1

                content_lower = content.lower()
                # Check for topic relevance (at least 2 words match)
                matches = sum(1 for w in topic_words if w in content_lower and len(w) > 3)
                if matches < 2:
                    continue

                # Check for contradiction signals
                contradiction_patterns = [
                    (r"(?:NEVER|never|MUST NOT|must not|DO NOT|do not|FORBIDDEN|forbidden)\s+\w+",
                     "prohibition"),
                    (r"(?:DEPRECATED|deprecated|REMOVED|removed|SUPERSEDED|superseded)",
                     "superseded"),
                    (r"(?:INSTEAD|instead|REPLACED BY|replaced by|USE .+ INSTEAD)",
                     "replacement"),
                ]
                for pattern, kind in contradiction_patterns:
                    found = re.findall(pattern, content)
                    if found:
                        contradictions.append({
                            "file": os.path.join(doc_dir, fname),
                            "kind": kind,
                            "matches": found[:3],  # Limit to 3 matches
                        })
            except Exception:
                continue

    return {
        "contradictions": contradictions,
        "contradiction_aligned": len(contradictions) == 0,
        "contradiction_docs_checked": docs_checked,
    }
