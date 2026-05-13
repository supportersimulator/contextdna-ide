"""
SYNAPTIC PROACTIVE INSIGHT ENGINE -- Notices Patterns Without Being Asked

Generates observations, warnings, and suggestions based on accumulated
knowledge across the entire memory subsystem.

Trigger sources:
  1. Pattern engine -- actionable patterns with confidence >= 0.5
  2. Evolution engine -- belief shifts and contradictions
  3. Personality -- high-confidence wisdom, emotional patterns
  4. Outcome tracker -- repeated failures (same task failing 3+ times)
  5. Session historian -- circling behavior (same task attempted 3+ times)
  6. Pattern engine (ignored) -- patterns surfaced but never acted on
  7. Cross-session drift -- beliefs contradicting observed outcomes

When should_alert_aaron() returns True, write_seed_file() produces a
fleet seed at /tmp/fleet-seed-synaptic-{ts}.md for webhook injection.

Usage:
    from memory.synaptic_proactive import (
        generate_proactive_insights,
        get_proactive_context,
        should_alert_aaron,
        write_seed_file,
    )

    insights = generate_proactive_insights(session_id="abc123")
    if should_alert_aaron(insights):
        path = write_seed_file(insights)
"""

import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("context_dna.synaptic_proactive")

# Observability (Zero Silent Failures)
_failure_count = 0

# Rate limiting state
_last_call_ts: float = 0.0
_MIN_INTERVAL_S = 300  # 5 minutes between full scans
_MAX_INSIGHTS_PER_CALL = 5

# Alert thresholds
REPEATED_FAILURE_THRESHOLD = 3   # same task fails N+ times -> warning
CIRCLING_THRESHOLD = 3           # same task attempted N+ times -> warning
IGNORED_PATTERN_AGE_H = 24      # pattern surfaced but unacted for N hours
DRIFT_CONFIDENCE_DELTA = 0.3    # belief confidence swing > N -> drift alert

# Seed file config
SEED_DIR = Path("/tmp")
SEED_PREFIX = "fleet-seed-synaptic-"

# Deduplication: content hashes of recently surfaced insights
_recent_hashes: Set[str] = set()
_MAX_RECENT_HASHES = 100


@dataclass
class ProactiveInsight:
    """A single proactive insight."""
    type: str          # "warning", "observation", "suggestion", "alert"
    content: str
    confidence: float  # 0.0 - 1.0
    source: str        # which subsystem produced it
    detail: str = ""   # extended explanation for seed files


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.strip().lower().encode()).hexdigest()[:16]


def _is_duplicate(content: str) -> bool:
    h = _hash_content(content)
    if h in _recent_hashes:
        return True
    _recent_hashes.add(h)
    if len(_recent_hashes) > _MAX_RECENT_HASHES:
        _recent_hashes.clear()
        _recent_hashes.add(h)
    return False


# ---------------------------------------------------------------------------
# Source 1: Pattern engine (actionable patterns)
# ---------------------------------------------------------------------------

def _insights_from_patterns() -> List[ProactiveInsight]:
    """Source insights from the pattern engine's actionable patterns."""
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        engine = get_pattern_engine()
        patterns = engine.get_actionable(limit=5)
        results = []
        for p in patterns:
            if p.confidence < 0.5:
                continue
            insight_type = "warning" if p.pattern_type in (
                "recurring_error", "architecture_drift"
            ) else "suggestion" if p.actionable else "observation"
            content = p.title
            if p.suggested_action:
                content += f" -- {p.suggested_action}"
            results.append(ProactiveInsight(
                type=insight_type,
                content=content,
                confidence=p.confidence,
                source="pattern_engine",
                detail=p.description[:300] if p.description else "",
            ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Pattern insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 2: Evolution engine (belief shifts)
# ---------------------------------------------------------------------------

def _insights_from_beliefs() -> List[ProactiveInsight]:
    """Source insights from high-confidence, recently-changed beliefs."""
    try:
        from memory.synaptic_evolution_engine import get_evolution_tracker
        tracker = get_evolution_tracker()
        beliefs = tracker.get_all_beliefs()
        results = []
        for b in beliefs:
            if b.confidence < 0.7 or b.evidence_count < 2:
                continue
            results.append(ProactiveInsight(
                type="observation",
                content=f"[{b.domain}] {b.belief}",
                confidence=b.confidence,
                source="evolution_engine",
            ))
        timeline = tracker.get_timeline(limit=5)
        for ev in timeline:
            if ev.trigger_type == "contradiction":
                results.append(ProactiveInsight(
                    type="warning",
                    content=(
                        f"Belief shift on {ev.topic}: "
                        f"was '{ev.before_belief[:60]}', "
                        f"now '{ev.after_belief[:60]}'"
                    ),
                    confidence=ev.after_confidence,
                    source="evolution_engine",
                ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Belief insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 3: Personality (wisdom + emotional patterns)
# ---------------------------------------------------------------------------

def _insights_from_personality() -> List[ProactiveInsight]:
    """Source insights from personality wisdom and emotional patterns."""
    try:
        from memory.synaptic_personality import get_personality
        personality = get_personality()
        results = []
        for w in personality.get_wisdom(limit=5):
            if w.confidence >= 0.7 and w.validation_count >= 2:
                results.append(ProactiveInsight(
                    type="suggestion",
                    content=w.insight,
                    confidence=w.confidence,
                    source="personality_wisdom",
                ))
        for ep in personality.get_emotional_patterns():
            if ep.effectiveness >= 0.7 and ep.observation_count >= 2:
                results.append(ProactiveInsight(
                    type="observation",
                    content=f"When {ep.trigger} -- {ep.response_style}",
                    confidence=ep.effectiveness,
                    source="personality_emotional",
                ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Personality insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 4: Outcome tracker (repeated failures)
# ---------------------------------------------------------------------------

def _insights_from_outcome_tracker() -> List[ProactiveInsight]:
    """Detect repeated failures: same task failing 3+ times."""
    try:
        from memory.outcome_tracker import get_outcome_tracker
        tracker = get_outcome_tracker()
        completed = tracker.get_completed_outcomes(limit=50)
        if not completed:
            return []

        # Group failures by task keyword (first 40 chars normalized)
        failure_groups: Dict[str, list] = {}
        for o in completed:
            if not o.get("success"):
                key = o["task"][:40].strip().lower()
                failure_groups.setdefault(key, []).append(o)

        results = []
        for task_key, failures in failure_groups.items():
            count = len(failures)
            if count >= REPEATED_FAILURE_THRESHOLD:
                # Build specific diagnosis
                approaches = [f["approach"][:60] for f in failures[:3]]
                actual = failures[0].get("actual_outcome", "unknown")[:80]
                content = (
                    f"Task '{failures[0]['task'][:50]}' has failed {count} times. "
                    f"Last failure: {actual}"
                )
                detail = (
                    f"Approaches tried: {'; '.join(approaches)}. "
                    f"The root cause may differ from what was attempted -- "
                    f"consider stepping back to re-diagnose."
                )
                results.append(ProactiveInsight(
                    type="warning",
                    content=content,
                    confidence=min(0.95, 0.5 + count * 0.1),
                    source="outcome_tracker",
                    detail=detail,
                ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Outcome tracker insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 5: Session historian (circling behavior)
# ---------------------------------------------------------------------------

def _insights_from_session_historian() -> List[ProactiveInsight]:
    """Detect circling: same task/topic attempted across 3+ sessions."""
    try:
        from memory.session_historian import SessionHistorian
        historian = SessionHistorian()
        # Get recent session summaries from the archive
        archive_dir = Path.home() / ".context-dna" / "session-archive"
        if not archive_dir.exists():
            return []

        # Read recent session JSON files (last 10)
        session_files = sorted(archive_dir.glob("*.json"), reverse=True)[:10]
        if len(session_files) < 2:
            return []

        # Extract task/topic tokens from session summaries
        session_topics: List[List[str]] = []
        for sf in session_files:
            try:
                data = json.loads(sf.read_text())
                summary = data.get("summary", "") or ""
                # Extract significant words (>4 chars, lowercased)
                words = [
                    w.lower() for w in summary.split()
                    if len(w) > 4 and w.isalpha()
                ]
                session_topics.append(words)
            except Exception:
                continue

        if len(session_topics) < 2:
            return []

        # Count word frequency across sessions
        word_sessions: Dict[str, int] = Counter()
        for words in session_topics:
            for w in set(words):  # unique per session
                word_sessions[w] += 1

        results = []
        # Find words appearing in 3+ sessions (circling signal)
        circling_words = [
            (w, c) for w, c in word_sessions.items()
            if c >= CIRCLING_THRESHOLD
            and w not in {"session", "atlas", "aaron", "synaptic", "context",
                          "memory", "webhook", "should", "could", "would",
                          "about", "unique", "content", "worked", "fixing",
                          "system", "errors", "these", "there", "their",
                          "which", "other", "after", "before", "every"}
        ]
        if circling_words:
            top = sorted(circling_words, key=lambda x: -x[1])[:3]
            topics = ", ".join(f"'{w}' ({c} sessions)" for w, c in top)
            results.append(ProactiveInsight(
                type="warning",
                content=f"Circling detected: topics recurring across sessions: {topics}",
                confidence=0.7,
                source="session_historian",
                detail=(
                    "The same topics keep appearing without resolution. "
                    "This may indicate: (a) the root cause hasn't been found, "
                    "(b) the fix isn't sticking, or (c) a deeper architectural issue."
                ),
            ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Session historian insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 6: Ignored warnings (surfaced patterns not acted on)
# ---------------------------------------------------------------------------

def _insights_from_ignored_patterns() -> List[ProactiveInsight]:
    """Detect patterns that were surfaced but never acted on."""
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        engine = get_pattern_engine()
        recent = engine.get_recent(limit=20)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=IGNORED_PATTERN_AGE_H)

        results = []
        for p in recent:
            # Pattern was surfaced (shown) but has no linked resolution
            if not p.surfaced:
                continue
            try:
                surfaced_dt = datetime.fromisoformat(
                    p.surfaced_at.replace("Z", "+00:00")
                ) if p.surfaced_at else None
            except Exception:
                surfaced_dt = None

            if surfaced_dt and surfaced_dt < cutoff and p.actionable:
                results.append(ProactiveInsight(
                    type="warning",
                    content=(
                        f"Ignored pattern (surfaced {int((now - surfaced_dt).total_seconds() / 3600)}h ago): "
                        f"{p.title}"
                    ),
                    confidence=p.confidence,
                    source="ignored_pattern",
                    detail=f"Action suggested: {p.suggested_action}" if p.suggested_action else "",
                ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Ignored pattern insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 7: Cross-session drift (beliefs vs outcomes)
# ---------------------------------------------------------------------------

def _insights_from_cross_session_drift() -> List[ProactiveInsight]:
    """Detect drift: beliefs with large confidence swings across sessions."""
    try:
        from memory.synaptic_personality import get_personality
        personality = get_personality()
        updates = personality.get_recent_belief_updates(limit=10)
        results = []
        for u in updates:
            if abs(u.confidence_delta) >= DRIFT_CONFIDENCE_DELTA:
                direction = "strengthened" if u.confidence_delta > 0 else "weakened"
                results.append(ProactiveInsight(
                    type="alert",
                    content=(
                        f"Cross-session drift on '{u.topic}': "
                        f"belief {direction} by {abs(u.confidence_delta):.0%} -- "
                        f"now '{u.after_state[:60]}'"
                    ),
                    confidence=0.8,
                    source="cross_session_drift",
                    detail=f"Evidence: {u.evidence[:200]}",
                ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("Cross-session drift insight gathering failed: %s", e)
        return []


def _insights_from_north_star_decay() -> List[ProactiveInsight]:
    """Detect overdue North Star priorities — Aaron's locked-sequence vision items.

    Surfaces non-decaying priorities (multi-fleet, 3-surgeons, etc.) that haven't
    been reviewed within their cycle. Direct response to Aaron's stated pain
    (chat 2026-04-25): vision items drift while tactical work absorbs focus.
    """
    try:
        from memory.north_star import overdue
        results = []
        for entry in overdue():
            age = entry.get("age_days", 0)
            cycle = entry.get("review_cycle_days", 7)
            results.append(ProactiveInsight(
                type="warning",
                content=(
                    f"North Star priority overdue: '{entry['name']}' "
                    f"({age:.1f}d since review, cycle={cycle}d). "
                    f"Vision items must not silently fossilize."
                ),
                confidence=0.95,
                source="north_star_decay",
                detail=(
                    f"id={entry['id']} rank={entry['rank']} | "
                    f"Mark reviewed: python3 -m memory.north_star reviewed {entry['id']}"
                ),
            ))
        return results
    except Exception as e:
        global _failure_count
        _failure_count += 1
        logger.debug("North Star decay insight gathering failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def generate_proactive_insights(
    session_id: Optional[str] = None,
    active_file: Optional[str] = None,
    recent_prompt: Optional[str] = None,
    force: bool = False,
) -> List[ProactiveInsight]:
    """
    Generate proactive insights from all Synaptic subsystems.

    Rate limited: max 1 call per 5 minutes, max 5 insights per call.
    Deduplicates against recently surfaced insights.

    Args:
        session_id: Current session identifier (for logging).
        active_file: Currently active file path (future: relevance filtering).
        recent_prompt: Most recent user prompt (future: relevance filtering).
        force: Bypass rate limit (for testing / manual triggers).

    Returns:
        List of ProactiveInsight objects, max 5.
    """
    global _last_call_ts, _failure_count

    now = time.monotonic()
    if not force and now - _last_call_ts < _MIN_INTERVAL_S and _last_call_ts > 0:
        return []
    _last_call_ts = now

    all_insights: List[ProactiveInsight] = []

    # Original 3 sources (mac3 foundation)
    all_insights.extend(_insights_from_patterns())
    all_insights.extend(_insights_from_beliefs())
    all_insights.extend(_insights_from_personality())

    # New sources (Session 5)
    all_insights.extend(_insights_from_outcome_tracker())
    all_insights.extend(_insights_from_session_historian())
    all_insights.extend(_insights_from_ignored_patterns())
    all_insights.extend(_insights_from_cross_session_drift())

    # North Star decay (post-Session 5, addresses Aaron's vision-drift pain)
    all_insights.extend(_insights_from_north_star_decay())

    # Deduplicate
    unique = [i for i in all_insights if not _is_duplicate(i.content)]

    # Sort: alerts > warnings > suggestions > observations, then by confidence
    type_priority = {"alert": 0, "warning": 1, "suggestion": 2, "observation": 3}
    unique.sort(key=lambda i: (type_priority.get(i.type, 9), -i.confidence))

    return unique[:_MAX_INSIGHTS_PER_CALL]


def should_alert_aaron(insights: Optional[List[ProactiveInsight]] = None) -> bool:
    """
    Determine if the current insights warrant alerting Aaron.

    Returns True when:
    - Any alert-severity insight exists
    - 2+ warning-severity insights exist
    - Any single insight has confidence >= 0.9
    """
    if insights is None:
        insights = generate_proactive_insights(force=True)
    if not insights:
        return False

    alerts = [i for i in insights if i.type == "alert"]
    warnings = [i for i in insights if i.type == "warning"]
    high_conf = [i for i in insights if i.confidence >= 0.9]

    return len(alerts) > 0 or len(warnings) >= 2 or len(high_conf) > 0


def write_seed_file(
    insights: List[ProactiveInsight],
    seed_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Write a fleet seed file for webhook injection when Synaptic has
    proactive insights that warrant Aaron's attention.

    Format: /tmp/fleet-seed-synaptic-{timestamp}.md

    Returns the path written, or None if nothing to write.
    """
    if not insights:
        return None

    target_dir = seed_dir or SEED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{SEED_PREFIX}{ts}.md"
    path = target_dir / filename

    lines = [
        f"# Synaptic Proactive Alert -- {ts}",
        "",
        "**Synaptic noticed something without being asked.**",
        "",
    ]
    for i, insight in enumerate(insights, 1):
        icon = {"alert": "!!!", "warning": "!!", "suggestion": ">", "observation": "-"}.get(
            insight.type, "-"
        )
        lines.append(f"{icon} **[{insight.type.upper()}]** {insight.content}")
        lines.append(f"   Confidence: {insight.confidence:.0%} | Source: {insight.source}")
        if insight.detail:
            lines.append(f"   Detail: {insight.detail}")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by Synaptic Proactive Insight Engine*")

    path.write_text("\n".join(lines))
    logger.info("Synaptic seed file written: %s (%d insights)", path, len(insights))
    return path


def get_proactive_context() -> str:
    """
    Convenience: format proactive insights for webhook injection.

    Returns a concise string suitable for S6/S8 sections.
    """
    insights = generate_proactive_insights()
    if not insights:
        return ""

    lines = ["Proactive insights:"]
    for i in insights:
        icon = {"alert": "!!!", "warning": "!", "suggestion": ">", "observation": "-"}.get(
            i.type, "-"
        )
        lines.append(
            f"  {icon} [{i.type}] {i.content} "
            f"(conf: {i.confidence:.0%}, src: {i.source})"
        )
    return "\n".join(lines)


def run_proactive_check() -> Dict:
    """
    Full proactive check: generate insights, evaluate alert threshold,
    write seed file if needed.

    Returns dict with insights, alert status, and seed path.
    Suitable for scheduler or manual invocation.
    """
    insights = generate_proactive_insights(force=True)
    alert = should_alert_aaron(insights)
    seed_path = None
    if alert:
        seed_path = write_seed_file(insights)

    return {
        "insights_count": len(insights),
        "insights": [asdict(i) for i in insights],
        "should_alert": alert,
        "seed_file": str(seed_path) if seed_path else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "failure_count": _failure_count,
    }


# ---------------------------------------------------------------------------
# CLI: python -m memory.synaptic_proactive [list|emit-s6|emit-s8|reset-cache]
# ---------------------------------------------------------------------------

def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m memory.synaptic_proactive",
        description="Synaptic Proactive Insight Engine CLI",
    )
    parser.add_argument(
        "command",
        choices=["list", "emit-s6", "emit-s8", "reset-cache", "status"],
        help="Action to perform.",
    )
    parser.add_argument(
        "--limit", type=int, default=2,
        help="Max insights to emit (default 2).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON output (for `list` and `status`).",
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        insights = generate_proactive_insights(force=True)
        if args.json:
            print(json.dumps([asdict(i) for i in insights], indent=2))
        else:
            if not insights:
                print("(no proactive insights at this time)")
            for i in insights:
                print(f"[{i.type:12s}] conf={i.confidence:.0%} src={i.source:24s} {i.content}")
        return 0

    if args.command in ("emit-s6", "emit-s8"):
        from memory.synaptic_emission import emit_top_insights
        insights = generate_proactive_insights(force=True)
        audience = "s6" if args.command == "emit-s6" else "s8"
        block = emit_top_insights(insights, audience=audience, limit=args.limit)
        print(block or f"(no insights to emit for {audience.upper()})")
        return 0

    if args.command == "reset-cache":
        from memory.synaptic_emission import reset_emission_cache
        n = reset_emission_cache()
        # Also clear in-process dedup
        _recent_hashes.clear()
        print(f"Cleared {n} cached emissions and in-process dedup hashes.")
        return 0

    if args.command == "status":
        from memory.synaptic_emission import emission_status
        status = {
            "proactive_failure_count": _failure_count,
            "recent_dedup_hashes": len(_recent_hashes),
            "min_interval_s": _MIN_INTERVAL_S,
            "max_insights_per_call": _MAX_INSIGHTS_PER_CALL,
            "emission": emission_status(),
        }
        print(json.dumps(status, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
