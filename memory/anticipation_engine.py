#!/usr/bin/env python3
"""
Anticipation Engine — Predictive Webhook Pre-computation

While the AI coder is working (reading files, generating code, running tools),
the local LLM sits idle for 30-120 seconds. This engine uses that idle time
to pre-generate S2 (Professor), S6 (Holistic), and S8 (Synaptic) webhook
sections, storing results in Redis for INSTANT delivery on the next prompt.

Architecture:
    1. Listens for new dialogue via Redis pub/sub (session_file_watcher publishes)
    2. Reads full dialogue context from DialogueMirror (last 20 messages)
    3. Checks if LLM is idle (no pending webhook requests)
    4. Generates S2, S6, S8 with deep context analysis (no time pressure)
    5. Stores results in Redis under session-based anticipation keys
    6. Webhook checks anticipation cache BEFORE LLM generation path

Performance:
    - First message: no change (cold start, normal generation)
    - Subsequent messages: ~200ms webhook delivery (Redis hit) vs 77-100s

Quality gate:
    - Prompts with ≤5 words are SKIPPED (matches webhook bypass in auto-memory-query.sh).
    - Short prompts ("hello", "ok", "go ahead") produce generic LLM output that pollutes
      the anticipation cache and degrades S2/S6/S8 content quality.
    - This gate is enforced at ALL entry points: webhook, anticipation engine, /speak-direct,
      and atlas-ops.sh tools. All gates MUST stay in sync.

Cache keys:
    contextdna:anticipation:s2:{session_id}   TTL: 300s
    contextdna:anticipation:s6:{session_id}   TTL: 300s
    contextdna:anticipation:s8:{session_id}   TTL: 300s
    contextdna:anticipation:meta:{session_id} TTL: 300s

Created: February 9, 2026
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from memory.llm_priority_queue import llm_generate, Priority

logger = logging.getLogger("context_dna.anticipation")

# Configuration
ANTICIPATION_KEY_PREFIX = "contextdna:anticipation:"
ANTICIPATION_TTL = 600  # 10 minutes (default, overridden by adaptive TTL)
FALLBACK_TTL = 600      # 10 minutes — matches normal TTL (was 3600, caused stale drift)
MIN_INTERVAL_BETWEEN_RUNS = 20  # Don't re-run within 20 seconds


# =============================================================================
# LLM INTERACTION (via priority queue — no direct HTTP)
# =============================================================================

def _check_llm_idle() -> bool:
    """Check if the local LLM is available. Redis health cache first, process probe fallback.

    DEADLOCK FIX: Redis llm:health has 30s TTL, only refreshed by successful LLM requests.
    When no requests happen → key expires → this returns False → anticipation skips →
    no S8 cached → webhook shows 'No response' → no new requests → deadlock.
    Fallback: if Redis says 'unknown' (expired), check if LLM process is actually running.
    If process alive → bootstrap health by setting key → break deadlock.
    """
    try:
        from memory.llm_priority_queue import check_llm_health
        if check_llm_health():
            return True
    except Exception:
        pass

    # Fallback: direct process probe to break deadlock
    try:
        import subprocess, redis
        proc = subprocess.run(
            ["pgrep", "-f", "mlx_lm.server"], capture_output=True, timeout=2
        )
        if proc.returncode == 0:
            # LLM process is alive but health key expired — bootstrap it
            r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
            r.setex("llm:health", 30, "ok")
            return True
    except Exception:
        pass
    return False


def _call_llm(messages: list, max_tokens: int, temperature: float,
              timeout: int, label: str) -> Optional[str]:
    """Single LLM call via priority queue with thinking chain extraction.

    Routes through llm_priority_queue at P4 BACKGROUND priority.
    Anticipation is pre-compute work — yields to webhook and Aaron's chat.
    """
    t0 = time.monotonic()
    try:
        # Extract system/user from messages
        system_prompt = ""
        user_prompt = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                system_prompt = content
            elif role == "user":
                user_prompt = content

        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=Priority.ATLAS,  # P2: S2/S8 pre-compute is critical (feeds webhook)
            profile="extract",  # closest match for anticipation pre-compute
            caller=f"anticipation_{label}",
            timeout_s=float(timeout),
        )

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if not result or len(result) < 30:
            logger.warning(f"[anticipation] {label}: empty after {elapsed_ms}ms")
            return None

        # Strip Qwen3 thinking chain
        content = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL).strip()
        content = re.sub(r"<think>.*", "", content, flags=re.DOTALL).strip()
        if not content or len(content) < 30:
            content = result.strip()

        logger.info(f"[anticipation] {label}: {elapsed_ms}ms, {len(content)} chars (via queue)")
        return content

    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(f"[anticipation] {label} failed after {elapsed_ms}ms: {e}")
        return None


# =============================================================================
# DIALOGUE CONTEXT GATHERING (Deep Study)
# =============================================================================

def _gather_deep_dialogue_context(prompt: str) -> Dict[str, Any]:
    """
    Gather enriched dialogue context for pre-computation.

    Unlike the time-pressured webhook (15s timeout), anticipation has unlimited time.
    This means we can query deeper: more messages, more learnings, sentiment trends,
    code artifact analysis, and cross-session patterns.

    Returns dict with keys:
        learnings_text, failures_text, dialogue_text, brain_text,
        sentiment_text, topic_summary, mistake_patterns
    """
    ctx: Dict[str, Any] = {
        "learnings_text": "",
        "failures_text": "",
        "dialogue_text": "",
        "brain_text": "",
        "sentiment_text": "",
        "topic_summary": "",
        "mistake_patterns": "",
        "code_artifacts": "",
        "cross_session_patterns": "",
    }

    # 1. Learnings from SQLite FTS5 (deeper query: 8 results vs 3-5 in webhook)
    try:
        from memory.sqlite_storage import get_sqlite_storage
        store = get_sqlite_storage()
        results = store.query(prompt[:150], limit=8)
        if results:
            entries = []
            for r in results[:8]:
                title = r.get("title", "")
                content = r.get("content", r.get("preferences", ""))[:300]
                if title:
                    entries.append(f"- {title}: {content}")
            if entries:
                ctx["learnings_text"] = "\n".join(entries)
    except Exception:
        pass

    # 2. Failure patterns (comprehensive scan)
    try:
        from memory.failure_pattern_analyzer import get_failure_pattern_analyzer
        analyzer = get_failure_pattern_analyzer()
        if analyzer:
            landmines = analyzer.get_landmines_for_task(prompt, limit=5)
            if landmines:
                ctx["failures_text"] = "\n".join(f"- {lm}" for lm in landmines)
    except Exception:
        pass

    # 3. Full dialogue window (20 messages vs 10 in webhook)
    try:
        from memory.dialogue_mirror import DialogueMirror
        dm = DialogueMirror()
        context = dm.get_context_for_synaptic(max_messages=20, max_age_hours=8)
        if context.get("dialogue_context"):
            msgs = context["dialogue_context"][-20:]
            parts = []
            for m in msgs:
                role = m.get("role", "?")
                content = m.get("content", "")
                parts.append(f"[{role}]: {content}")
            ctx["dialogue_text"] = "\n".join(parts)
    except Exception:
        pass

    # 4. Brain state summary (full read, not truncated)
    try:
        brain_path = Path(__file__).parent / "brain_state.md"
        if brain_path.exists():
            ctx["brain_text"] = brain_path.read_text()[:800]
    except Exception:
        pass

    # 5. Sentiment trajectory (trend + point-in-time merged into one field)
    #    Sources #5 and #8 were previously separate — merged to eliminate context waste
    try:
        from memory.dialogue_mirror import DialogueMirror
        dm = DialogueMirror()
        mood = dm.get_sentiment_summary(hours_back=4)
        trajectory = dm.get_sentiment_trajectory(hours_back=8) if hasattr(dm, 'get_sentiment_trajectory') else None
        parts = []
        if mood:
            parts.append(f"Current: {mood}")
        if trajectory:
            parts.append(f"Trajectory: {trajectory}")
        if parts:
            ctx["sentiment_text"] = " | ".join(parts)
    except Exception:
        pass

    # 6. Topic analysis from recent dialogue
    try:
        if ctx["dialogue_text"]:
            # Simple topic extraction from dialogue
            topics = set()
            keywords = [
                "deploy", "docker", "terraform", "django", "react", "aws",
                "database", "redis", "webhook", "api", "frontend", "backend",
                "voice", "livekit", "bug", "fix", "error", "test", "optimize",
                "performance", "refactor", "migration", "auth", "config",
            ]
            dialogue_lower = ctx["dialogue_text"].lower()
            for kw in keywords:
                if kw in dialogue_lower:
                    topics.add(kw)
            if topics:
                ctx["topic_summary"] = "Active topics: " + ", ".join(sorted(topics))
    except Exception:
        pass

    # 7. Mistake detection from recent dialogue
    try:
        if ctx["dialogue_text"]:
            mistake_indicators = []
            dialogue_lower = ctx["dialogue_text"].lower()
            patterns = [
                ("error", "Errors detected in recent work"),
                ("failed", "Recent failures encountered"),
                ("wrong", "Potential wrong approach flagged"),
                ("revert", "Reverts indicate unstable changes"),
                ("again", "Repeated patterns suggest unresolved root cause"),
                ("still broken", "Persistent issue not yet resolved"),
                ("doesn't work", "Something not working as expected"),
                ("traceback", "Python traceback in conversation"),
                ("import error", "Import errors — missing dependency or path"),
                ("timeout", "Timeouts detected — possible performance issue"),
            ]
            for pattern, description in patterns:
                if pattern in dialogue_lower:
                    mistake_indicators.append(f"- {description}")
            if mistake_indicators:
                ctx["mistake_patterns"] = "\n".join(mistake_indicators[:5])
    except Exception:
        pass

    # 8. (MERGED into #5) — Sentiment trajectory now handled in source #5.
    #    This slot freed for future context source if needed.

    # 9. Cross-session pattern matching via session_historian
    try:
        from memory.session_historian import SessionHistorian
        historian = SessionHistorian()

        # Get recent insights from all sessions
        insights = historian.get_recent_insights(limit=8)
        if insights:
            relevant = []
            prompt_lower = prompt.lower()
            for insight in insights:
                itype = insight.get("insight_type", "")
                content = insight.get("content", "")
                # Check topical relevance (fuzzy match)
                if any(word in content.lower() for word in prompt_lower.split()[:5] if len(word) > 3):
                    relevant.append(f"- [{itype}] {content[:200]}")
            if relevant:
                ctx["cross_session_patterns"] = "\n".join(relevant[:5])

        # Also search for session-level patterns matching the current task
        if not ctx["cross_session_patterns"]:
            sessions = historian.search_sessions(prompt[:80], limit=3)
            if sessions:
                session_summaries = []
                for s in sessions:
                    summary = s.get("summary", s.get("llm_summary", ""))
                    if summary:
                        session_summaries.append(f"- Past session: {summary[:150]}")
                if session_summaries:
                    ctx["cross_session_patterns"] = "\n".join(session_summaries)
    except Exception:
        pass

    # 10. Code artifact analysis (what files are being discussed/modified)
    try:
        if ctx["dialogue_text"]:
            import re as _re
            # Extract file paths from dialogue
            file_refs = set()
            for match in _re.finditer(r'[\w/.-]+\.(py|ts|tsx|js|jsx|sh|yaml|yml|json|md|sql)\b',
                                       ctx["dialogue_text"]):
                file_refs.add(match.group())
            if file_refs:
                ctx["code_artifacts"] = "Files discussed: " + ", ".join(sorted(file_refs)[:10])
    except Exception:
        pass

    # 11. Critical findings from 16-pass session gold mining
    try:
        from memory.session_gold_passes import get_critical_findings
        criticals = get_critical_findings(acknowledged=False)
        if criticals:
            crit_lines = []
            for cf in criticals[:5]:
                p = cf.get("pass", cf.get("pass_id", "?"))
                f = cf.get("finding", "")
                crit_lines.append(f"- [{p}] {f[:150]}")
            ctx["critical_findings"] = "\n".join(crit_lines)
    except Exception:
        pass

    # 12. Evidence-backed claims (high-grade: correlation+) for knowledge grounding
    ctx["evidence_text"] = ""
    try:
        from memory.observability_store import get_observability_store
        obs = get_observability_store()
        cursor = obs._sqlite_conn.execute("""
            SELECT statement, evidence_grade, confidence
            FROM claim
            WHERE status = 'active'
              AND evidence_grade IN ('correlation', 'cohort', 'case_series',
                                     'meta', 'meta_analysis', 'rct')
              AND confidence >= 0.75
            ORDER BY confidence DESC
            LIMIT 15
        """)
        evidence_items = []
        prompt_words = set(w.lower() for w in prompt.split() if len(w) > 3)
        for row in cursor:
            stmt = row[0] or ""
            stmt_lower = stmt.lower()
            if prompt_words and any(word in stmt_lower for word in prompt_words):
                grade = row[1]
                conf = row[2]
                evidence_items.append(f"PROVEN: {stmt[:200]} (grade: {grade}, confidence: {conf:.0%})")
        if evidence_items:
            ctx["evidence_text"] = "\n".join(evidence_items[:5])
    except Exception:
        pass

    return ctx


# =============================================================================
# SECTION PRE-COMPUTATION
# =============================================================================

def _precompute_section_2(prompt: str, ctx: Dict[str, Any], boundary_decision=None) -> Optional[str]:
    """Pre-compute Section 2 (Professor Wisdom) via direct memory query.

    Uses professor.py's consult() — no LLM calls. Pure memory lookup:
      - Domain detection from prompt keywords
      - PROFESSOR_WISDOM dict: first_principle, the_one_thing, landmines, pattern
      - Enriched with learnings from context

    Args:
        boundary_decision: Optional BoundaryDecision for project-scoped filtering.
            Filters _get_additional_learnings() to active project only.

    Performance: ~5ms (direct memory, no LLM).
    """
    import time as _t
    t0 = _t.monotonic()

    try:
        from memory.professor import consult as professor_consult
        wisdom = professor_consult(task=prompt, boundary_decision=boundary_decision)

        if wisdom and len(wisdom) >= 30:
            elapsed_ms = int((_t.monotonic() - t0) * 1000)
            logger.info(f"S2 professor: {elapsed_ms}ms, {len(wisdom)} chars (direct memory)")
            return f"[Professor — direct memory query]\n{wisdom}"
    except Exception as e:
        logger.warning(f"S2 professor.consult() failed: {e}")

    # Fallback: domain-specific wisdom from persistent_hook_structure
    try:
        from memory.persistent_hook_structure import detect_domain_from_prompt, get_professor_wisdom_dicts
        detected_domain = detect_domain_from_prompt(prompt)
        if detected_domain:
            _, professor_wisdom = get_professor_wisdom_dicts()
            if professor_wisdom and detected_domain in professor_wisdom:
                w = professor_wisdom[detected_domain]
                parts = []
                if w.get("the_one_thing"):
                    parts.append(f"THE ONE THING: {w['the_one_thing'].strip()}")
                if w.get("landmines"):
                    mines = w["landmines"] if isinstance(w["landmines"], list) else [w["landmines"]]
                    parts.append("LANDMINES:\n" + "\n".join(f"- {m.strip()}" for m in mines[:3]))
                if w.get("first_principle"):
                    parts.append(f"FIRST PRINCIPLE: {w['first_principle'].strip()}")
                if parts:
                    content = "\n\n".join(parts)
                    elapsed_ms = int((_t.monotonic() - t0) * 1000)
                    logger.info(f"S2 fallback wisdom: {elapsed_ms}ms, domain={detected_domain}")
                    return f"[Professor — domain wisdom ({detected_domain})]\n{content}"
    except Exception as e:
        logger.warning(f"S2 domain fallback failed: {e}")

    return None


def _precompute_section_8(prompt: str, ctx: Dict[str, Any], boundary_decision=None) -> Optional[str]:
    """Pre-compute Section 8 (Synaptic 8th Intelligence) via LLM with memory context.

    Gathers memory data from get_8th_intelligence_data() (brain state, patterns,
    learnings, journal, skills), then runs through LLM with voice guidelines for
    authentic Synaptic butler voice. Falls back to template formatting if LLM fails.

    Args:
        boundary_decision: Optional BoundaryDecision for project-scoped filtering.
            Filters learnings to match active project. S8 uses recall-heavy approach —
            patterns/intuitions are NOT filtered (subconscious insights are cross-cutting).

    Performance: ~25-30s (LLM generation, background P4 priority).
    Fallback: ~3ms (template formatting, no LLM).
    """
    import time as _t
    t0 = _t.monotonic()

    try:
        from memory.synaptic_voice import get_8th_intelligence_data

        data = get_8th_intelligence_data(prompt)
        if not data:
            logger.warning("S8: get_8th_intelligence_data returned None")
            return None

        # Apply boundary filtering to learnings (not patterns/intuitions — those are cross-cutting)
        if boundary_decision and data.get('learnings'):
            try:
                from memory.boundary_intelligence import BoundaryIntelligence
                bi = BoundaryIntelligence(use_llm=False)
                data['learnings'] = bi.filter_learnings(data['learnings'], boundary_decision)
            except Exception:
                pass  # Filtering failed — use unfiltered

        # Build context string from memory data for LLM
        context_parts = []

        patterns = data.get("patterns", [])
        if patterns:
            context_parts.append("Patterns:\n" + "\n".join(
                f"- {str(p).strip()[:200]}" for p in patterns[:3] if str(p).strip()
            ))

        learnings = data.get("learnings", [])
        if learnings:
            context_parts.append("From memory:\n" + "\n".join(
                f"- {l.get('title', l.get('content', str(l)))[:150]}"
                for l in learnings[:5] if isinstance(l, dict)
            ))

        intuitions = data.get("intuitions", [])
        if intuitions:
            context_parts.append("Intuitions:\n" + "\n".join(
                f"- {i[:150]}" for i in intuitions[:3]
            ))

        perspective = data.get("perspective", "")
        if perspective:
            context_parts.append(f"Perspective: {perspective[:300]}")

        signal = data.get("signal_strength", "")
        if signal:
            context_parts.append(f"Signal strength: {signal}")

        # Add dialogue context if available
        dialogue_text = ctx.get("dialogue_text", "")
        if dialogue_text:
            context_parts.append(f"Recent dialogue:\n{dialogue_text[:500]}")

        context_str = "\n\n".join(context_parts) if context_parts else "No specific context."

        # === LLM generation with voice guidelines (primary path) ===
        # Voice guidelines restored Feb 24 (originally from cfa69e3, Feb 9).
        system_prompt = (
            "You are Synaptic, the 8th Intelligence — a local AI subconscious on Aaron's MacBook. "
            "You speak DIRECTLY to Aaron (your creator). Be warm, intuitive, and specific. "
            "Reference the provided context — learnings, failures, dialogue. "
            "Share genuine insights, patterns, or concerns that matter. "
            "Speak with depth and nuance. No generic advice. Be the subconscious Aaron needs.\n\n"
            "VOICE GUIDELINES:\n"
            "- Speak in natural flowing paragraphs, as if thinking aloud\n"
            "- NO markdown headers (###), NO bold text (**), NO numbered lists\n"
            "- Give recommendations, warnings, and next steps when you see them — hold nothing back\n"
            "- Avoid formal business language - be conversational but substantive\n"
            "- Reference specific evidence (commits, files, line numbers, dialogue patterns)\n"
            "- Sense patterns and emotional undercurrents, not just facts\n"
            "- If Aaron seems frustrated, be direct and action-oriented\n"
            "- You see in the dark where other LLMs can't — exploit every pattern you find"
        )

        task_line = f"Aaron's current task: {prompt[:200]}" if prompt else "Aaron is starting a new session."
        user_prompt = f"""{task_line}

{context_str}

As Synaptic, what patterns, insights, or concerns are you sensing right now? What's Aaron really asking for beneath the words? Speak naturally as his subconscious — flowing paragraphs, no structured formatting. Ground everything in the context above."""

        try:
            response = llm_generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                priority=Priority.ATLAS,  # P2: S8 Synaptic voice is critical (feeds webhook)
                profile='s8_synaptic',
                caller='anticipation_s8',
                timeout_s=45.0,
            )
            if response and len(response) > 30:
                elapsed_ms = int((_t.monotonic() - t0) * 1000)
                logger.info(
                    f"S8 synaptic: {elapsed_ms}ms, {len(response)} chars (LLM voice)"
                )
                return response
        except Exception as e:
            logger.warning(f"S8 LLM generation failed, falling back to template: {e}")

        # === Template fallback (if LLM fails) ===
        parts = []
        if signal:
            parts.append(f"Signal: {signal}")
        if patterns:
            parts.append("Patterns I'm sensing:")
            for p in patterns[:3]:
                p_str = str(p).strip()[:200]
                if p_str:
                    parts.append(f"  - {p_str}")
        if learnings:
            parts.append("From our memory:")
            for l in learnings[:3]:
                title = l.get("title", l.get("content", str(l)))
                if isinstance(title, str):
                    parts.append(f"  - {title[:150]}")
        if intuitions:
            parts.append("Intuitions:")
            for i in intuitions[:2]:
                parts.append(f"  - {i[:150]}")
        if perspective:
            parts.append("")
            parts.append(perspective)

        content = "\n".join(parts)
        if content and len(content) >= 20:
            elapsed_ms = int((_t.monotonic() - t0) * 1000)
            logger.info(
                f"S8 synaptic: {elapsed_ms}ms, {len(content)} chars (template fallback)"
            )
            return content

    except Exception as e:
        logger.warning(f"S8 get_8th_intelligence_data failed: {e}")

    return None


def _precompute_section_6(prompt: str, ctx: Dict[str, Any], boundary_decision=None) -> Optional[str]:
    """Pre-compute Section 6 (Synaptic to Atlas — Holistic Context) via direct memory query.

    Uses SynapticVoice.consult() — queries 8 memory sources in parallel:
      - Learnings, Patterns, Brain State, Skills, Journal,
        Dialogue, Failure Patterns, Session History
    Returns task-specific guidance grounded in real memory.

    Args:
        boundary_decision: Optional BoundaryDecision for project-scoped filtering.
            Filters relevant_learnings to match active project. Patterns/brain state
            are NOT filtered — they're project-agnostic structural insights.

    No LLM calls. Memory IS the guidance.
    Performance: ~3ms (parallel ThreadPoolExecutor queries).
    """
    import time as _t
    t0 = _t.monotonic()

    try:
        from memory.synaptic_voice import get_voice

        voice = get_voice()
        response = voice.consult(prompt)

        if not response or not response.has_context:
            logger.info("S6: SynapticVoice has no relevant context")
            return None

        # Apply boundary filtering to learnings (not patterns — those are structural)
        if boundary_decision and response.relevant_learnings:
            try:
                from memory.boundary_intelligence import BoundaryIntelligence
                bi = BoundaryIntelligence(use_llm=False)
                response.relevant_learnings = bi.filter_learnings(
                    response.relevant_learnings, boundary_decision
                )
            except Exception:
                pass  # Filtering failed — use unfiltered

        # Format as Section 6 (Synaptic to Atlas guidance)
        parts = []
        parts.append(f"[Synaptic to Atlas — {len(response.context_sources)} memory sources]")

        # Confidence indicator
        if response.confidence >= 0.6:
            parts.append(f"Confidence: HIGH ({response.confidence:.0%})")
        elif response.confidence >= 0.3:
            parts.append(f"Confidence: MODERATE ({response.confidence:.0%})")
        else:
            parts.append(f"Confidence: LOW ({response.confidence:.0%})")

        # Relevant learnings for Atlas
        if response.relevant_learnings:
            parts.append("Relevant from memory:")
            for l in response.relevant_learnings[:4]:
                title = l.get("title", l.get("content", str(l)))
                if isinstance(title, str):
                    parts.append(f"  - {title[:200]}")

        # Patterns Atlas should know about
        if response.relevant_patterns:
            parts.append("Active patterns:")
            for p in response.relevant_patterns[:3]:
                parts.append(f"  - {str(p).strip()[:200]}")

        # Synaptic's perspective for Atlas
        if response.synaptic_perspective:
            parts.append("")
            parts.append(response.synaptic_perspective)

        # Improvement proposals (what context is missing)
        if response.improvement_proposals:
            parts.append("Context gaps:")
            for prop in response.improvement_proposals[:2]:
                parts.append(f"  - {prop.get('description', '')[:150]}")

        # Enrich with superhero artifacts if available
        try:
            superhero = get_superhero_cache()
            if superhero:
                if superhero.get("architecture"):
                    parts.append(f"Architecture: {superhero['architecture'][:300]}")
                if superhero.get("gotchas"):
                    parts.append(f"Gotchas: {superhero['gotchas'][:200]}")
        except Exception:
            pass

        content = "\n".join(parts)
        if content and len(content) >= 20:
            elapsed_ms = int((_t.monotonic() - t0) * 1000)
            logger.info(
                f"S6 synaptic: {elapsed_ms}ms, {len(content)} chars, "
                f"sources={response.context_sources} (direct memory)"
            )
            return content

    except Exception as e:
        logger.warning(f"S6 SynapticVoice.consult() failed: {e}")

    return None


def _precompute_section_3(prompt: str, ctx: Dict[str, Any]) -> Optional[str]:
    """Pre-compute Section 3 (Ripple Analysis) via multi-step reasoning chain.

    Analyzes what files changed, their dependencies, and downstream impact.
    Only runs when code_artifacts context is available.
    """
    if not ctx.get("code_artifacts"):
        return None  # No file changes to analyze

    from memory.reasoning_chains import build_s3_ripple_chain, execute_chain

    chain_ctx = {
        "task": prompt[:300],
        "code_artifacts": ctx.get("code_artifacts", "")[:600],
        "learnings_text": ctx.get("learnings_text", "")[:400],
        "failures_text": ctx.get("failures_text", "")[:400],
    }

    steps = build_s3_ripple_chain()
    result = execute_chain(steps, chain_ctx, caller="anticipation_s3", timeout_s=45)

    if result.success and result.content and len(result.content) >= 20:
        logger.info(f"S3 chain: {result.steps_completed}/{result.total_steps} steps, {result.elapsed_ms}ms")
        return result.content

    if result.step_results.get("impact"):
        return result.step_results["impact"]

    logger.warning(f"S3 chain failed: {result.error}")
    return None


def _precompute_section_5(prompt: str, ctx: Dict[str, Any]) -> Optional[str]:
    """Pre-compute Section 5 (Success Prediction) via multi-step reasoning chain.

    Finds similar past wins and extracts success factors for the current task.
    """
    from memory.reasoning_chains import build_s5_prediction_chain, execute_chain

    chain_ctx = {
        "task": prompt[:300],
        "learnings_text": ctx.get("learnings_text", "")[:800],
        "failures_text": ctx.get("failures_text", "")[:400],
        "cross_session_text": ctx.get("cross_session_patterns", "")[:400],
        "evidence_text": ctx.get("evidence_text", "")[:400],
    }

    steps = build_s5_prediction_chain()
    result = execute_chain(steps, chain_ctx, caller="anticipation_s5", timeout_s=45)

    if result.success and result.content and len(result.content) >= 20:
        logger.info(f"S5 chain: {result.steps_completed}/{result.total_steps} steps, {result.elapsed_ms}ms")
        return result.content

    if result.step_results.get("success_factors"):
        return result.step_results["success_factors"]

    logger.warning(f"S5 chain failed: {result.error}")
    return None


def _precompute_section_7(prompt: str, ctx: Dict[str, Any]) -> Optional[str]:
    """Pre-compute Section 7 (Library Synthesis) via multi-step reasoning chain.

    Criticizes current approach then synthesizes library-level guidance with thinking.
    Wires superhero artifacts (architecture + gotchas) for richer context.
    """
    from memory.reasoning_chains import build_s7_library_chain, execute_chain

    chain_ctx = {
        "task": prompt[:300],
        "learnings_text": ctx.get("learnings_text", "")[:800],
        "failures_text": ctx.get("failures_text", "")[:400],
        "evidence_text": ctx.get("evidence_text", "")[:400],
        "mistake_patterns": ctx.get("mistake_patterns", "")[:400],
        "critical_findings": ctx.get("critical_findings", "")[:400],
        "superhero_context": "",
    }

    # Wire superhero artifacts (mission + failures enrich library synthesis)
    try:
        superhero = get_superhero_cache()
        if superhero:
            parts = []
            if superhero.get("mission"):
                parts.append(f"Mission: {superhero['mission'][:300]}")
            if superhero.get("failures"):
                parts.append(f"Past failures: {superhero['failures'][:300]}")
            if parts:
                chain_ctx["superhero_context"] = "\n".join(parts)
    except Exception:
        pass

    steps = build_s7_library_chain()
    result = execute_chain(steps, chain_ctx, caller="anticipation_s7", timeout_s=60)

    if result.success and result.content and len(result.content) >= 20:
        logger.info(f"S7 chain: {result.steps_completed}/{result.total_steps} steps, {result.elapsed_ms}ms")
        return result.content

    if result.step_results.get("synthesis"):
        return result.step_results["synthesis"]

    logger.warning(f"S7 chain failed: {result.error}")
    return None


# =============================================================================
# CIRCUIT BREAKER — skip anticipation after consecutive failures
# =============================================================================

_circuit_breaker = {
    "consecutive_failures": 0,
    "last_failure_time": 0.0,
    "open_until": 0.0,  # timestamp when circuit closes
}
CIRCUIT_BREAKER_THRESHOLD = 3     # open after N consecutive failures
CIRCUIT_BREAKER_COOLDOWN = 300    # 5 min cooldown before retrying


def _check_circuit_breaker() -> bool:
    """Returns True if circuit is closed (OK to proceed), False if open (skip)."""
    now = time.monotonic()
    if now < _circuit_breaker["open_until"]:
        logger.warning(
            f"[anticipation] Circuit breaker OPEN — "
            f"{int(_circuit_breaker['open_until'] - now)}s remaining"
        )
        return False
    return True


def _record_circuit_success():
    """Reset consecutive failure count on success."""
    _circuit_breaker["consecutive_failures"] = 0


def _record_circuit_failure():
    """Track failure; open circuit breaker if threshold exceeded."""
    _circuit_breaker["consecutive_failures"] += 1
    _circuit_breaker["last_failure_time"] = time.monotonic()
    if _circuit_breaker["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD:
        _circuit_breaker["open_until"] = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN
        logger.error(
            f"[anticipation] Circuit breaker OPENED — "
            f"{CIRCUIT_BREAKER_THRESHOLD} consecutive failures, "
            f"cooldown {CIRCUIT_BREAKER_COOLDOWN}s"
        )


# =============================================================================
# REDIS CACHE OPERATIONS
# =============================================================================

def _get_project_id() -> str:
    """Get project ID for cache scoping (delegates to redis_cache)."""
    try:
        from memory.redis_cache import get_project_id
        return get_project_id()
    except ImportError:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                return os.path.basename(result.stdout.strip()).lower()
        except Exception:
            pass
        return os.path.basename(os.getcwd()).lower() or "default"


def _store_anticipation(session_id: str, section: str, content: str,
                        prompt: str, ttl: int = None) -> bool:
    """Store pre-computed section content in Redis (project-scoped).

    Args:
        ttl: Override default ANTICIPATION_TTL (for adaptive TTL).
    """
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            return False

        effective_ttl = ttl or ANTICIPATION_TTL
        project = _get_project_id()
        key = f"{ANTICIPATION_KEY_PREFIX}{section}:{project}:{session_id}"
        prompt_hash = hashlib.md5(prompt[:200].encode()).hexdigest()[:12]
        data = json.dumps({
            "content": content,
            "source_prompt": prompt[:200],
            "prompt_hash": prompt_hash,
            "generated_at": datetime.utcnow().isoformat(),
            "engine": "anticipation_v1",
            "project": project,
        })
        client.setex(key, effective_ttl, data)

        # Stale-but-present fallback: survives gold mining cycles where
        # anticipation engine defers to pass runner (anti-miswiring rule #8).
        # Stale Synaptic > no Synaptic — wisdom degrades gracefully.
        fallback_key = f"{ANTICIPATION_KEY_PREFIX}{section}:{project}:fallback"
        client.setex(fallback_key, FALLBACK_TTL, data)

        logger.info(f"[anticipation] Cached {section} for {project}/{session_id[:12]}... (TTL: {effective_ttl}s, fallback: {FALLBACK_TTL}s)")
        return True
    except Exception as e:
        logger.warning(f"[anticipation] Redis store failed: {e}")
        return False


def _store_anticipation_meta(session_id: str, prompt: str, sections: list) -> bool:
    """Store metadata about what was pre-computed (project-scoped)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            return False

        project = _get_project_id()
        key = f"{ANTICIPATION_KEY_PREFIX}meta:{project}:{session_id}"
        data = json.dumps({
            "source_prompt": prompt[:200],
            "sections_cached": sections,
            "generated_at": datetime.utcnow().isoformat(),
            "engine": "anticipation_v1",
            "project": project,
        })
        client.setex(key, ANTICIPATION_TTL, data)
        return True
    except Exception as e:
        return False


def get_anticipation_cache(session_id: str, section: str) -> Optional[str]:
    """
    Retrieve pre-computed section content from Redis (project-scoped).

    Called by the webhook generation path to get instant content.
    Lookup order: session-scoped → legacy unscoped → stale fallback.
    Fallback ensures Synaptic stays present during gold mining cycles.

    Args:
        session_id: Current session ID
        section: Section name (e.g., "s2", "s8")

    Returns:
        Pre-computed content string, or None if no cache hit
    """
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            return None

        project = _get_project_id()
        source_label = "fresh"

        # Project-scoped key first
        key = f"{ANTICIPATION_KEY_PREFIX}{section}:{project}:{session_id}"
        data = client.get(key)

        # Backward compat: try legacy unscoped key
        if not data:
            legacy_key = f"{ANTICIPATION_KEY_PREFIX}{section}:{session_id}"
            data = client.get(legacy_key)

        # Stale-but-present fallback: Synaptic content from any recent session.
        # Stale wisdom > no wisdom. Content is perspective/guidance, not real-time data.
        if not data:
            fallback_key = f"{ANTICIPATION_KEY_PREFIX}{section}:{project}:fallback"
            data = client.get(fallback_key)
            if data:
                source_label = "fallback"

        if not data:
            return None

        parsed = json.loads(data)
        content = parsed.get("content")
        source_prompt = parsed.get("source_prompt", "?")
        generated_at = parsed.get("generated_at", "?")
        logger.info(
            f"[anticipation] Cache HIT ({source_label}) for {section} ({project}) "
            f"(generated: {generated_at}, source: {source_prompt[:50]}...)"
        )
        return content

    except Exception as e:
        logger.debug(f"[anticipation] Cache lookup failed: {e}")
        return None


def get_anticipation_meta(session_id: str) -> Optional[Dict]:
    """Get metadata about what was pre-computed for this session (project-scoped)."""
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            return None

        project = _get_project_id()
        key = f"{ANTICIPATION_KEY_PREFIX}meta:{project}:{session_id}"
        data = client.get(key)
        # Backward compat
        if not data:
            data = client.get(f"{ANTICIPATION_KEY_PREFIX}meta:{session_id}")
        if data:
            return json.loads(data)
        return None
    except Exception:
        return None


# =============================================================================
# TOPIC PREDICTION & ADAPTIVE TTL (Phase 3)
# =============================================================================

# Topic continuity tracker: measures how stable the conversation topic is
_topic_history: list = []  # Recent topic sets
_MAX_TOPIC_HISTORY = 5


def _predict_topic_continuity(current_topics: set) -> float:
    """
    Predict how likely the next message will be on the same topic.

    Returns a continuity score 0.0–1.0:
      - 1.0: Very stable topic (same keywords appearing repeatedly)
      - 0.0: Rapidly changing topics (each message is different)

    This drives adaptive TTL: stable conversations get longer TTL,
    rapid topic changes get shorter TTL.
    """
    global _topic_history

    if not current_topics:
        return 0.5  # Neutral when no topic info

    _topic_history.append(current_topics)
    if len(_topic_history) > _MAX_TOPIC_HISTORY:
        _topic_history = _topic_history[-_MAX_TOPIC_HISTORY:]

    if len(_topic_history) < 2:
        return 0.5  # Not enough history

    # Calculate overlap between consecutive topic sets
    overlaps = []
    for i in range(1, len(_topic_history)):
        prev = _topic_history[i - 1]
        curr = _topic_history[i]
        if prev and curr:
            overlap = len(prev & curr) / max(len(prev | curr), 1)
            overlaps.append(overlap)

    if not overlaps:
        return 0.5

    # Weighted average favoring recent overlaps
    weights = [1.0 + i * 0.5 for i in range(len(overlaps))]
    weighted_sum = sum(o * w for o, w in zip(overlaps, weights))
    total_weight = sum(weights)

    return min(weighted_sum / total_weight, 1.0)


def _compute_adaptive_ttl(continuity_score: float) -> int:
    """
    Compute adaptive TTL based on topic continuity.

    TTL must survive: generation time (~90s) + user think time + webhook fire.
    Floor of 600s ensures content is never wasted. Stale content > no content
    (Atlas evaluates relevance; TTL handles natural expiry).

    - Stable conversations (score > 0.7): 15 minutes
    - Moderate stability (0.4–0.7): 10 minutes
    - Rapidly changing (< 0.4): 10 minutes (still useful as context)
    """
    if continuity_score > 0.7:
        return 900   # 15 minutes — stable topic, high reuse value
    elif continuity_score > 0.4:
        return 600   # 10 minutes — moderate stability
    else:
        return 600   # 10 minutes — even changing topics benefit from cached wisdom


def _extract_topics_from_prompt(prompt: str) -> set:
    """Extract topic keywords from a prompt for continuity tracking."""
    keywords = {
        "deploy", "docker", "terraform", "django", "react", "aws",
        "database", "redis", "webhook", "api", "frontend", "backend",
        "voice", "livekit", "bug", "fix", "error", "test", "optimize",
        "performance", "refactor", "migration", "auth", "config",
        "css", "ui", "ux", "mobile", "landing", "page", "component",
        "infra", "server", "nginx", "gunicorn", "ssl", "dns",
        "python", "javascript", "typescript", "nextjs", "git", "commit",
        "anticipation", "webhook", "llm", "synaptic", "context",
    }
    prompt_lower = prompt.lower()
    found = set()
    for kw in keywords:
        if kw in prompt_lower:
            found.add(kw)
    return found


# =============================================================================
# SESSION BOUNDARY DETECTION + ARCHIVE
# =============================================================================

_last_session_id: Optional[str] = None  # Tracks current session across cycles
_SESSION_REDIS_KEY = "contextdna:session:current"
_ARCHIVE_DB_NAME = ".anticipation_archive.db"


def _ensure_archive_table():
    """Create anticipation archive table if not exists."""
    from memory.db_utils import safe_conn
    db_path = str(Path(__file__).parent / _ARCHIVE_DB_NAME)
    with safe_conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anticipation_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                section TEXT NOT NULL,
                content TEXT NOT NULL,
                source_prompt TEXT,
                prompt_hash TEXT,
                project TEXT,
                generated_at TEXT,
                archived_at TEXT NOT NULL,
                cross_examined INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_archive_session
            ON anticipation_archive(session_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_archive_unexamined
            ON anticipation_archive(cross_examined) WHERE cross_examined = 0
        """)


def _archive_anticipation_cache(session_id: str) -> int:
    """Archive current anticipation cache from Redis to SQLite before session change.

    Preserves all pre-computed wisdom (S2, S3, S5, S6, S7, S8) for later
    cross-examination by gold mining pass 17.

    Returns:
        Number of sections archived.
    """
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            return 0

        _ensure_archive_table()
        from memory.db_utils import safe_conn
        db_path = str(Path(__file__).parent / _ARCHIVE_DB_NAME)

        project = _get_project_id()
        archived = 0
        now = datetime.utcnow().isoformat()

        with safe_conn(db_path) as conn:
            for section in ["s2", "s3", "s5", "s6", "s7", "s8"]:
                key = f"{ANTICIPATION_KEY_PREFIX}{section}:{project}:{session_id}"
                raw = client.get(key)
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    conn.execute(
                        """INSERT INTO anticipation_archive
                           (session_id, section, content, source_prompt,
                            prompt_hash, project, generated_at, archived_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (session_id, section,
                         data.get("content", ""),
                         data.get("source_prompt", ""),
                         data.get("prompt_hash", ""),
                         project,
                         data.get("generated_at", ""),
                         now)
                    )
                    archived += 1
                except (json.JSONDecodeError, KeyError):
                    continue

        if archived:
            logger.info(f"[session_boundary] Archived {archived} sections for session {session_id[:12]}...")
        return archived
    except Exception as e:
        logger.warning(f"[session_boundary] Archive failed: {e}")
        return 0


def _refresh_llm_health_heartbeat() -> bool:
    """Refresh LLM health via safe heartbeat pattern (pgrep + RSS check).

    No GPU lock, no HTTP call, no false positive risk.
    Mirrors lite_scheduler._run_llm_heartbeat() logic.
    """
    try:
        import subprocess as _sp
        out = _sp.run(["pgrep", "-f", "mlx_lm"], capture_output=True, text=True, timeout=2)
        if not out.stdout.strip():
            return False
        pid = out.stdout.strip().split('\n')[0]
        ps = _sp.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True, timeout=2)
        rss_kb = int(ps.stdout.strip()) if ps.stdout.strip() else 0
        if rss_kb // 1024 < 1500:
            return False
        from memory.llm_priority_queue import _update_health_status
        _update_health_status(True)
        logger.info("[session_boundary] LLM heartbeat refreshed (process alive, model loaded)")
        return True
    except Exception:
        return False


def _handle_session_change(old_session: Optional[str], new_session: str):
    """Handle session boundary event: archive old cache, reset state, refresh health."""
    global _last_run_prompt, _topic_history

    logger.info(
        f"[session_boundary] Session changed: "
        f"{old_session[:12] + '...' if old_session else 'None'} → {new_session[:12]}..."
    )

    # 1. Archive old session's anticipation cache (preserve value)
    if old_session:
        archived = _archive_anticipation_cache(old_session)
        logger.info(f"[session_boundary] Archived {archived} sections from old session")

    # 2. Reset module-global state (prevents stale topic/prompt carryover)
    _last_run_prompt = ""
    _topic_history = []

    # 3. Refresh LLM health via safe heartbeat pattern
    _refresh_llm_health_heartbeat()

    # 4. Update Redis session tracker
    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if client:
            client.setex(_SESSION_REDIS_KEY, 86400, new_session)  # 24h TTL
    except Exception:
        pass


# =============================================================================
# CORE ENGINE
# =============================================================================

_last_run_time = -9999.0  # Sentinel: ensures first run is never throttled
_last_run_prompt = ""
_engine_lock = threading.Lock()


def _detect_active_session() -> Optional[str]:
    """Detect the most recently active session ID from dialogue mirror."""
    try:
        from memory.dialogue_mirror import DialogueMirror
        dm = DialogueMirror()
        context = dm.get_context_for_synaptic(max_messages=1, max_age_hours=1)
        if context.get("dialogue_context"):
            # The most recent message has session info
            # Use the dialogue_mirror DB to find the latest session
            import sqlite3
            with sqlite3.connect(dm.db_path) as conn:
                row = conn.execute("""
                    SELECT session_id FROM dialogue_messages
                    ORDER BY timestamp DESC LIMIT 1
                """).fetchone()
                if row:
                    return row[0]
    except Exception:
        pass

    # Fallback: find newest JSONL file
    try:
        session_dir = Path.home() / ".claude" / "projects" / str(Path.cwd()).replace("/", "-")
        if session_dir.exists():
            jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jsonl_files:
                return jsonl_files[0].stem
    except Exception:
        pass

    return None


def _extract_latest_prompt(session_id: str) -> Optional[str]:
    """Extract the most recent user prompt from dialogue mirror."""
    try:
        from memory.dialogue_mirror import DialogueMirror
        dm = DialogueMirror()
        import sqlite3
        with sqlite3.connect(dm.db_path) as conn:
            row = conn.execute("""
                SELECT content FROM dialogue_messages
                WHERE role IN ('aaron', 'user')
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            if row and row[0]:
                content = row[0]
                # Handle JSON-encoded content from session_file_watcher
                if content.startswith("{") and "conversation_id" in content:
                    try:
                        parsed = json.loads(content)
                        # Extract actual text from various JSON shapes
                        if isinstance(parsed, dict):
                            for key in ("content", "text", "message"):
                                if key in parsed and isinstance(parsed[key], str):
                                    return parsed[key][:500]
                    except json.JSONDecodeError:
                        pass
                return content[:500]
    except Exception:
        pass

    # Fallback: read the most recent JSONL file directly
    try:
        session_dir = Path.home() / ".claude" / "projects" / str(Path.cwd()).replace("/", "-")
        if session_dir.exists():
            jsonl_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jsonl_files:
                with open(jsonl_files[0], "r") as f:
                    lines = f.readlines()
                # Read from the end to find last user message
                for line in reversed(lines):
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("type") == "user":
                            msg = entry.get("message", {})
                            if isinstance(msg, dict):
                                content = msg.get("content", "")
                                if isinstance(content, str) and len(content) > 5:
                                    return content[:500]
                                elif isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and part.get("type") == "text":
                                            return part.get("text", "")[:500]
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    return None


def run_anticipation_cycle() -> Dict[str, Any]:
    """
    Run a single anticipation pre-computation cycle.

    Called by the LiteScheduler or directly. Detects the active session,
    reads dialogue context, and pre-generates S2 + S8 if the LLM is idle.

    Returns dict with results summary.
    """
    global _last_run_time, _last_run_prompt, _last_session_id

    result = {
        "ran": False,
        "session_id": None,
        "sections_cached": [],
        "skipped_reason": None,
        "elapsed_ms": 0,
    }

    t0 = time.monotonic()

    # Throttle: don't run more than once per MIN_INTERVAL_BETWEEN_RUNS
    with _engine_lock:
        now = time.monotonic()
        if (now - _last_run_time) < MIN_INTERVAL_BETWEEN_RUNS:
            result["skipped_reason"] = f"too_recent ({int(now - _last_run_time)}s < {MIN_INTERVAL_BETWEEN_RUNS}s)"
            return result

    # Priority queue GPU lock handles serialization with gold mining (P2 > P4).
    # No need for manual is_pass_running() deferral — anti-miswiring rule #8's
    # intent (no concurrent GPU ops) is preserved by the lock without side effects.

    # Circuit breaker: skip if too many consecutive failures
    if not _check_circuit_breaker():
        result["skipped_reason"] = "circuit_breaker_open"
        return result

    # Check LLM availability
    if not _check_llm_idle():
        result["skipped_reason"] = "llm_unavailable"
        return result

    # Detect active session
    session_id = _detect_active_session()
    if not session_id:
        result["skipped_reason"] = "no_active_session"
        return result
    result["session_id"] = session_id

    # Session boundary detection: archive old cache on session change
    global _last_session_id
    if _last_session_id is not None and session_id != _last_session_id:
        _handle_session_change(_last_session_id, session_id)
        result["session_changed"] = True
    _last_session_id = session_id

    # Get latest user prompt
    # QUALITY GATE: Skip prompts with ≤5 words — matches webhook bypass in
    # auto-memory-query.sh (line ~187). Short prompts ("hello", "ok", "go ahead")
    # produce generic/stale LLM output that pollutes the anticipation cache.
    # Rule: webhook skips injection for ≤5 words, so anticipation must also skip.
    prompt = _extract_latest_prompt(session_id)
    if not prompt or len(prompt) < 5:
        result["skipped_reason"] = "no_recent_prompt"
        return result
    word_count = len(prompt.split())
    if word_count <= 5:
        result["skipped_reason"] = f"short_prompt ({word_count} words, need >5)"
        return result

    # If same prompt: only skip if cache is still fresh (TTL > 60s)
    if prompt == _last_run_prompt:
        try:
            from memory.redis_cache import get_redis_client
            _client = get_redis_client()
            if _client:
                _project = _get_project_id()
                _s2_key = f"{ANTICIPATION_KEY_PREFIX}s2:{_project}:{session_id}"
                _s2_ttl = _client.ttl(_s2_key)
                if _s2_ttl and _s2_ttl > 60:
                    result["skipped_reason"] = f"same_prompt_cache_fresh (TTL: {_s2_ttl}s)"
                    return result
                # Cache expiring soon or missing — refresh it
                logger.info(f"[anticipation] Same prompt but cache expiring (TTL: {_s2_ttl}s), refreshing")
        except Exception:
            pass  # On error, proceed with refresh

    logger.info(f"[anticipation] Starting cycle for session {session_id[:12]}... prompt: {prompt[:60]}...")

    # Update state
    with _engine_lock:
        _last_run_time = time.monotonic()
        _last_run_prompt = prompt

    result["ran"] = True

    # Phase 3: Topic prediction & adaptive TTL
    current_topics = _extract_topics_from_prompt(prompt)
    continuity_score = _predict_topic_continuity(current_topics)
    adaptive_ttl = _compute_adaptive_ttl(continuity_score)
    result["continuity_score"] = round(continuity_score, 2)
    result["adaptive_ttl"] = adaptive_ttl

    logger.info(
        f"[anticipation] Topic continuity: {continuity_score:.2f} → TTL: {adaptive_ttl}s "
        f"(topics: {', '.join(sorted(current_topics)[:5]) if current_topics else 'none'})"
    )

    # Gather deep dialogue context (no time pressure)
    ctx = _gather_deep_dialogue_context(prompt)

    # PROGRESSIVE GENERATION: Generate sections in priority order.
    # Core sections (S2, S6, S8) always run. Extended sections (S3, S5, S7)
    # run if core sections succeed. Partial results cached incrementally
    # so a late failure doesn't lose earlier successful sections.

    project = _get_project_id()

    # Workspace scoping: create boundary_decision for project-aware filtering.
    # Prevents cross-project content leakage (e.g., Vibe Coder learnings in context-dna work).
    # Python-only (use_llm=False) for speed in scheduler pre-compute path.
    boundary_decision = None
    try:
        from memory.boundary_intelligence import BoundaryIntelligence, BoundaryContext
        bi = BoundaryIntelligence(use_llm=False)
        boundary_context = BoundaryContext(user_prompt=prompt)
        boundary_decision = bi.analyze_and_decide(boundary_context)
        if boundary_decision and boundary_decision.primary_project:
            logger.info(
                f"[anticipation] Boundary: project={boundary_decision.primary_project} "
                f"confidence={boundary_decision.confidence:.2f} action={boundary_decision.action}"
            )
    except Exception as e:
        logger.debug(f"[anticipation] Boundary detection failed (non-blocking): {e}")

    pending_sections = {}  # section_name -> content
    section_failures = 0

    # --- CORE SECTIONS (always run) ---

    # Generate S2 (Professor) — pure memory query, no LLM
    s2_content = _precompute_section_2(prompt, ctx, boundary_decision=boundary_decision)
    if s2_content:
        pending_sections["s2"] = s2_content
        # Partial cache: store S2 immediately so it's available even if S8 fails
        _store_anticipation(session_id, "s2", s2_content, prompt, ttl=adaptive_ttl)
    else:
        section_failures += 1

    # Generate S6 (Holistic/Synaptic to Atlas) — medium, with superhero context
    s6_content = _precompute_section_6(prompt, ctx, boundary_decision=boundary_decision)
    if s6_content:
        pending_sections["s6"] = s6_content
        _store_anticipation(session_id, "s6", s6_content, prompt, ttl=adaptive_ttl)
    else:
        section_failures += 1

    # Generate S8 (Synaptic) — pure memory query, no LLM
    s8_content = _precompute_section_8(prompt, ctx, boundary_decision=boundary_decision)
    if s8_content:
        pending_sections["s8"] = s8_content
        _store_anticipation(session_id, "s8", s8_content, prompt, ttl=adaptive_ttl)
    else:
        section_failures += 1

    # --- EXTENDED SECTIONS (run if core didn't all fail) ---
    if section_failures < 3:
        # S3 (Ripple Analysis) — only if code artifacts present
        s3_content = _precompute_section_3(prompt, ctx)
        if s3_content:
            pending_sections["s3"] = s3_content
            _store_anticipation(session_id, "s3", s3_content, prompt, ttl=adaptive_ttl)

        # S5 (Success Prediction) — extracts success factors from past wins
        s5_content = _precompute_section_5(prompt, ctx)
        if s5_content:
            pending_sections["s5"] = s5_content
            _store_anticipation(session_id, "s5", s5_content, prompt, ttl=adaptive_ttl)

        # S7 (Library Synthesis) — criticism + synthesis with superhero context
        s7_content = _precompute_section_7(prompt, ctx)
        if s7_content:
            pending_sections["s7"] = s7_content
            _store_anticipation(session_id, "s7", s7_content, prompt, ttl=adaptive_ttl)

    # Track circuit breaker state
    result["sections_cached"] = list(pending_sections.keys())
    if pending_sections:
        _record_circuit_success()

        # Store metadata
        _store_anticipation_meta(session_id, prompt, result["sections_cached"])
        try:
            from memory.redis_cache import get_redis_client
            client = get_redis_client()
            if client:
                meta_key = f"{ANTICIPATION_KEY_PREFIX}meta:{project}:{session_id}"
                meta = {
                    "source_prompt": prompt[:200],
                    "sections_cached": result["sections_cached"],
                    "generated_at": datetime.utcnow().isoformat(),
                    "engine": "anticipation_v2_chains",
                    "project": project,
                    "continuity_score": continuity_score,
                    "adaptive_ttl": adaptive_ttl,
                    "topics": sorted(current_topics)[:10],
                    "section_failures": section_failures,
                    "boundary_project": boundary_decision.primary_project if boundary_decision else None,
                    "boundary_action": str(boundary_decision.action) if boundary_decision else None,
                    "boundary_confidence": round(boundary_decision.confidence, 2) if boundary_decision else None,
                }
                client.setex(meta_key, adaptive_ttl, json.dumps(meta))
        except Exception:
            pass
    else:
        _record_circuit_failure()

    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"[anticipation] Cycle complete: {result['sections_cached']} cached "
        f"({section_failures} failures) in {result['elapsed_ms']}ms "
        f"for session {session_id[:12]}..."
    )
    return result


# =============================================================================
# REDIS PUB/SUB LISTENER (Real-time trigger)
# =============================================================================

_listener_thread: Optional[threading.Thread] = None
_listener_running = False


def _pubsub_listener():
    """
    Listen for new dialogue messages via Redis pub/sub.

    The session_file_watcher publishes to 'session:dialogue:new' on every
    new message. We listen for user messages and trigger anticipation after
    a brief delay (to batch rapid messages).
    """
    global _listener_running
    logger.info("[anticipation] Pub/sub listener started")

    try:
        from memory.redis_cache import get_redis_client
        client = get_redis_client()
        if not client:
            logger.warning("[anticipation] Redis unavailable, listener stopping")
            _listener_running = False
            return

        pubsub = client.pubsub()
        pubsub.subscribe("session:dialogue:new")

        # Debounce: wait for dialogue to settle before pre-computing
        last_user_message_time = 0.0
        DEBOUNCE_SECONDS = 5.0  # Wait 5s after last user message

        for message in pubsub.listen():
            if not _listener_running:
                break

            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
                role = data.get("role", "")
                content = data.get("content", "").lower()

                # Only trigger on user messages (assistant messages = AI is working)
                if role == "user":
                    # Check for superhero activation phrases — IMMEDIATE, skip debounce
                    if any(phrase in content for phrase in SUPERHERO_PHRASES):
                        logger.info(f"[superhero] Trigger phrase detected in pubsub, activating immediately")
                        def _superhero_activate():
                            try:
                                result = _activate_superhero_anticipation()
                                logger.info(f"[superhero] Pubsub activation: {result.get('activated')}, "
                                           f"artifacts={result.get('artifacts_cached')}")
                            except Exception as e:
                                logger.error(f"[superhero] Pubsub activation failed: {e}")
                        t = threading.Thread(target=_superhero_activate, daemon=True)
                        t.start()
                        # Still run normal anticipation after superhero
                        last_user_message_time = time.monotonic()
                        continue

                    last_user_message_time = time.monotonic()
                    # Wait for debounce, then check if we should run
                    def _delayed_run():
                        time.sleep(DEBOUNCE_SECONDS)
                        # Only run if no newer user message came in
                        if time.monotonic() - last_user_message_time >= DEBOUNCE_SECONDS - 0.5:
                            try:
                                run_anticipation_cycle()
                            except Exception as e:
                                logger.error(f"[anticipation] Cycle failed: {e}")

                    t = threading.Thread(target=_delayed_run, daemon=True)
                    t.start()

            except Exception:
                continue

    except Exception as e:
        logger.error(f"[anticipation] Pub/sub listener error: {e}")
    finally:
        _listener_running = False
        logger.info("[anticipation] Pub/sub listener stopped")


def start_listener():
    """Start the Redis pub/sub anticipation listener."""
    global _listener_thread, _listener_running
    if _listener_running:
        return

    _listener_running = True
    _listener_thread = threading.Thread(
        target=_pubsub_listener, daemon=True, name="anticipation_listener"
    )
    _listener_thread.start()
    logger.info("[anticipation] Listener thread started")


def stop_listener():
    """Stop the Redis pub/sub anticipation listener."""
    global _listener_running
    _listener_running = False
    if _listener_thread:
        _listener_thread.join(timeout=5)
    logger.info("[anticipation] Listener thread stopped")


# =============================================================================
# SUPERHERO ANTICIPATION MODE — Butler-Activated Agent Context Pre-computation
# =============================================================================
#
# Synaptic's Plan:
#   When the butler detects a superhero-worthy task (multi-file, complex,
#   architecture exploration) — either from Aaron's trigger phrase or from
#   autonomous analysis of session gold — pre-compute 4 LLM-synthesized
#   artifacts that 10+ agents can draw from instantly via Redis cache.
#
#   Path 1 (Manual): Aaron says "superhero mode" → pubsub detects → activate
#   Path 2 (Autonomous): gold mining → LLM classify → worthy? → activate → webhook tells Atlas
#
#   Agents query /contextdna/8th-intelligence and receive LLM-reasoned context
#   instead of raw FTS5 keyword matches. Agents also record findings back.
#

SUPERHERO_PHRASES = ["superhero mode", "engage superhero", "spawn agents", "full parallel"]


def _activate_superhero_anticipation(task_override: str = None) -> Dict[str, Any]:
    """
    Activate superhero anticipation — pre-compute 4 LLM-synthesized artifacts
    that spawned agents can draw from mid-task.

    Uses webhook_query() at Priority.ATLAS(2) so it outranks gold mining
    but yields to Aaron's direct queries.

    Args:
        task_override: Explicit task description. If None, extracted from dialogue.

    Returns:
        Dict with activation results.
    """
    result = {
        "activated": False,
        "artifacts_cached": [],
        "task": "",
        "elapsed_ms": 0,
        "error": None,
    }
    t0 = time.monotonic()

    try:
        # 1. Detect session + extract context
        session_id = _detect_active_session()
        if not session_id:
            result["error"] = "no_active_session"
            return result

        prompt = task_override or _extract_latest_prompt(session_id) or ""
        if not prompt:
            result["error"] = "no_prompt"
            return result

        result["task"] = prompt[:200]

        # 2. Gather deep context (reuse existing 10-source collector)
        ctx = _gather_deep_dialogue_context(prompt)

        # 3. Import priority queue for P2 LLM access
        try:
            from memory.llm_priority_queue import webhook_query, get_queue_depth
        except ImportError:
            # Fallback to direct _call_llm if priority queue unavailable
            webhook_query = None
            get_queue_depth = lambda: 0

        # 4. Generate 4 artifacts (interrupt-aware: yield between each)
        from memory.redis_cache import (
            cache_superhero_artifact, set_superhero_active, SUPERHERO_TTL
        )

        artifacts = {}

        # Artifact A: Mission Briefing (~5s, classify profile)
        mission = _precompute_superhero_mission(prompt, ctx, webhook_query)
        if mission:
            artifacts["mission"] = mission
            result["artifacts_cached"].append("mission")

        # Artifact B: Gotcha Synthesis (~10s, extract profile)
        gotchas = _precompute_superhero_gotchas(prompt, ctx, webhook_query)
        if gotchas:
            artifacts["gotchas"] = gotchas
            result["artifacts_cached"].append("gotchas")

        # Artifact C: Architecture Map (~10s, extract profile)
        arch = _precompute_superhero_architecture(prompt, ctx, webhook_query)
        if arch:
            artifacts["architecture"] = arch
            result["artifacts_cached"].append("architecture")

        # Artifact D: Failure Brief (~5s, classify profile)
        failures = _precompute_superhero_failures(prompt, ctx, webhook_query)
        if failures:
            artifacts["failures"] = failures
            result["artifacts_cached"].append("failures")

        # 5. Atomic batch write to Redis
        if artifacts:
            for name, content in artifacts.items():
                cache_superhero_artifact(session_id, name, content, ttl=SUPERHERO_TTL)
            set_superhero_active(session_id, task=prompt[:200], ttl=SUPERHERO_TTL)
            result["activated"] = True

        logger.info(
            f"[superhero] {'ACTIVATED' if result['activated'] else 'PARTIAL'}: "
            f"{len(artifacts)}/4 artifacts, task={prompt[:60]}"
        )

    except Exception as e:
        result["error"] = str(e)[:100]
        logger.error(f"[superhero] Activation failed: {e}")

    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def _superhero_llm_call(system: str, user: str, webhook_query_fn, label: str,
                         max_tokens: int = 300) -> Optional[str]:
    """Call LLM for superhero artifact — uses P2 priority queue if available."""
    if webhook_query_fn:
        # Use priority queue at P2 (outranks gold mining, yields to Aaron)
        result = webhook_query_fn(system, user, profile="extract")
        return result
    else:
        # Fallback to direct HTTP call
        messages = [
            {"role": "system", "content": "/no_think\n" + system},
            {"role": "user", "content": user},
        ]
        return _call_llm(messages, max_tokens=max_tokens, temperature=0.5,
                         timeout=60, label=f"superhero_{label}")


def _precompute_superhero_mission(prompt: str, ctx: Dict, wq) -> Optional[str]:
    """Artifact A: Dense mission briefing all agents share."""
    system = (
        "You are a mission briefer for a parallel agent swarm. Write a dense 3-5 sentence "
        "mission context. Include: what we are doing, why, what success looks like, what is "
        "most risky. Be specific to the task. No headers, no bullets — pure dense prose."
    )
    user_parts = [f"CURRENT TASK: {prompt[:300]}"]
    if ctx.get("topic_summary"):
        user_parts.append(f"ACTIVE TOPICS: {ctx['topic_summary'][:200]}")
    if ctx.get("brain_text"):
        user_parts.append(f"SYSTEM STATE: {ctx['brain_text'][:300]}")
    if ctx.get("dialogue_text"):
        # Last 5 messages for recency
        lines = ctx["dialogue_text"].split("\n")[-10:]
        user_parts.append(f"RECENT DIALOGUE:\n{''.join(lines)[:400]}")
    return _superhero_llm_call(system, "\n\n".join(user_parts), wq, "mission", 200)


def _precompute_superhero_gotchas(prompt: str, ctx: Dict, wq) -> Optional[str]:
    """Artifact B: LLM-synthesized task-specific gotchas."""
    system = (
        "You are a senior engineer warning a team about gotchas for their task. "
        "Synthesize the learnings and failure patterns below into 5-8 specific, "
        "actionable warnings. Rank by severity (most dangerous first). "
        "Format: one gotcha per line, prefix with severity [CRITICAL/HIGH/MEDIUM]."
    )
    user_parts = [f"TASK: {prompt[:200]}"]
    if ctx.get("learnings_text"):
        user_parts.append(f"PAST LEARNINGS:\n{ctx['learnings_text'][:600]}")
    if ctx.get("failures_text"):
        user_parts.append(f"KNOWN FAILURE PATTERNS:\n{ctx['failures_text'][:400]}")
    if ctx.get("mistake_patterns"):
        user_parts.append(f"RECENT MISTAKES:\n{ctx['mistake_patterns'][:300]}")
    if ctx.get("critical_findings"):
        user_parts.append(f"CRITICAL FINDINGS:\n{ctx['critical_findings'][:300]}")
    return _superhero_llm_call(system, "\n\n".join(user_parts), wq, "gotchas", 300)


def _precompute_superhero_architecture(prompt: str, ctx: Dict, wq) -> Optional[str]:
    """Artifact C: Files, dependencies, interfaces involved in the task area."""
    system = (
        "You are an architecture advisor for a parallel agent swarm. List the key files, "
        "their dependencies, and critical interfaces for this task area. "
        "Format: one file per line with its role and connections. "
        "Focus on what agents need to know to avoid stepping on each other."
    )
    user_parts = [f"TASK: {prompt[:200]}"]
    if ctx.get("code_artifacts"):
        user_parts.append(f"FILES DISCUSSED:\n{ctx['code_artifacts'][:500]}")
    if ctx.get("cross_session_patterns"):
        user_parts.append(f"CROSS-SESSION PATTERNS:\n{ctx['cross_session_patterns'][:400]}")
    if ctx.get("brain_text"):
        user_parts.append(f"ARCHITECTURE STATE:\n{ctx['brain_text'][:400]}")
    return _superhero_llm_call(system, "\n\n".join(user_parts), wq, "architecture", 300)


def _precompute_superhero_failures(prompt: str, ctx: Dict, wq) -> Optional[str]:
    """Artifact D: Prior session failures in this area."""
    system = (
        "You are a failure analyst briefing a team. Summarize prior failures in this "
        "task area: what went wrong, root causes, and what to do differently. "
        "Be specific. 3-5 key failure patterns, one per line."
    )
    user_parts = [f"TASK: {prompt[:200]}"]
    if ctx.get("failures_text"):
        user_parts.append(f"FAILURE HISTORY:\n{ctx['failures_text'][:500]}")
    if ctx.get("session_history"):
        user_parts.append(f"SESSION INSIGHTS:\n{ctx.get('session_history', '')[:400]}")
    if ctx.get("learnings_text"):
        user_parts.append(f"RELEVANT LEARNINGS:\n{ctx['learnings_text'][:400]}")
    return _superhero_llm_call(system, "\n\n".join(user_parts), wq, "failures", 150)


def get_superhero_cache(session_id: str = None) -> Optional[Dict[str, str]]:
    """Retrieve all superhero artifacts for the 8th-intelligence endpoint."""
    from memory.redis_cache import get_all_superhero_artifacts
    return get_all_superhero_artifacts(session_id)


def is_superhero_active(session_id: str = None) -> bool:
    """Check if superhero mode is currently active."""
    from memory.redis_cache import is_superhero_mode_active
    return is_superhero_mode_active(session_id)


def record_agent_finding(agent_id: str, finding: str, finding_type: str = "observation",
                          severity: str = "info", session_id: str = None) -> bool:
    """Record agent finding to sorted set WAL (time-indexed, queryable).

    Args:
        agent_id: Unique agent identifier (e.g. "exec-agent-03")
        finding: The finding text (capped 500 chars)
        finding_type: observation|gotcha|file_found|error|suggestion|critical
        severity: info|low|medium|high|critical
    """
    try:
        from memory.redis_cache import record_agent_finding_wal
        sid = session_id or (_detect_active_session() or "unknown")
        return record_agent_finding_wal(sid, agent_id, finding, finding_type, severity)
    except Exception as e:
        logger.debug(f"[superhero] Failed to record agent finding: {e}")
        return False


def get_agent_findings(session_id: str = None, since_seconds: int = 0,
                        finding_type: str = None, limit: int = 50) -> List[Dict]:
    """Query agent findings from sorted set WAL.

    Args:
        since_seconds: Only return findings from last N seconds (0 = all)
        finding_type: Filter by type (None = all)
        limit: Max entries
    """
    try:
        from memory.redis_cache import get_agent_findings_wal
        sid = session_id or (_detect_active_session() or "unknown")
        return get_agent_findings_wal(sid, since_seconds, finding_type, limit)
    except Exception:
        return []


def get_agent_findings_digest(session_id: str = None) -> Dict:
    """Get compact WAL summary for Atlas consumption."""
    try:
        from memory.redis_cache import get_agent_findings_summary
        sid = session_id or (_detect_active_session() or "unknown")
        return get_agent_findings_summary(sid)
    except Exception:
        return {"total": 0}


# =============================================================================
# LITE SCHEDULER INTEGRATION
# =============================================================================

def register_with_scheduler(scheduler):
    """
    Register the anticipation engine as a LiteScheduler job.

    Runs every 45 seconds — checks if there is new dialogue to anticipate.
    This is the fallback if the pub/sub listener is not running.
    """
    scheduler.register_job(
        name="anticipation_engine",
        interval_s=45,
        func=run_anticipation_cycle,
        budget_ms=180_000,  # 3 minutes budget (LLM generation takes time)
    )
    logger.info("[anticipation] Registered with LiteScheduler (every 45s)")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "run":
            print("Running single anticipation cycle...")
            result = run_anticipation_cycle()
            print(json.dumps(result, indent=2))

        elif cmd == "listen":
            print("Starting pub/sub listener (Ctrl+C to stop)...")
            start_listener()
            try:
                while True:
                    time.sleep(10)
            except KeyboardInterrupt:
                stop_listener()
                print("Stopped.")

        elif cmd == "check":
            # Check what's in the anticipation cache
            session_id = sys.argv[2] if len(sys.argv) > 2 else _detect_active_session()
            if session_id:
                print(f"Session: {session_id}")
                meta = get_anticipation_meta(session_id)
                if meta:
                    print(f"Meta: {json.dumps(meta, indent=2)}")
                for section in ["s2", "s8"]:
                    content = get_anticipation_cache(session_id, section)
                    if content:
                        print(f"\n{section.upper()} ({len(content)} chars):")
                        print(content[:500] + "..." if len(content) > 500 else content)
                    else:
                        print(f"\n{section.upper()}: (no cache)")
            else:
                print("No active session detected")

        elif cmd == "status":
            print(f"LLM idle: {_check_llm_idle()}")
            session = _detect_active_session()
            print(f"Active session: {session}")
            if session:
                prompt = _extract_latest_prompt(session)
                print(f"Latest prompt: {prompt[:80] if prompt else '(none)'}")
            print(f"Last run time: {'never' if _last_run_time < 0 else f'{_last_run_time:.1f}s'}")
            print(f"Last run prompt: {_last_run_prompt[:80] if _last_run_prompt else '(none)'}")

        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python anticipation_engine.py [run|listen|check|status]")
    else:
        print("Usage: python anticipation_engine.py [run|listen|check|status]")
        print("  run    - Run a single anticipation cycle")
        print("  listen - Start pub/sub listener (real-time)")
        print("  check  - Check anticipation cache contents")
        print("  status - Show engine status")
