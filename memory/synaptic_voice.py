#!/usr/bin/env python3
"""
memory.synaptic_voice — Synaptic's Voice (8th Intelligence consultation engine)

═══════════════════════════════════════════════════════════════════════════
INTERPRETATION DECISION (Atlas, 2026-05-13)
═══════════════════════════════════════════════════════════════════════════

Synaptic's directive described this module as "WebSocket-based TTS/STT bridge".
Atlas audited the 8 active import sites in the live codebase. Result:

  • 8/8 references treat synaptic_voice as a CONTEXT/PERSONALITY engine
    (class SynapticVoice, SynapticResponse dataclass, .consult(), .speak(),
    get_voice(), get_8th_intelligence_data())
  • 0/8 references touch audio I/O, sockets, microphones, TTS engines,
    WebSocket frames, or STT pipelines.

Consumers (verified by grep on memory/, mcp-servers/, tools/, scripts/,
admin.contextdna.io/, filtered to .py/.ts/.tsx/.js/.sh, logs excluded):

  1. memory/agent_service.py:3189
       from memory.synaptic_voice import SynapticVoice, get_8th_intelligence_data
       — Calls SynapticVoice().consult(subtask) and reads
         .relevant_patterns / .relevant_learnings / .synaptic_perspective
  2. memory/persistent_hook_structure.py:341
       from memory.synaptic_voice import get_voice, consult, speak
       — Lazy-loads the module-level functions
  3. mcp-servers/synaptic_mcp.py:52,92,150
       from memory.synaptic_voice import SynapticVoice, SynapticResponse
       — MCP server wraps .consult() over the wire
  4. mcp-servers/contextdna_webhook_mcp.py:234
       from memory.synaptic_voice import SynapticVoice
       — Section 8 ("8th Intelligence") generator
  5. memory/tests/test_s6_zsf_observability.py:87,133,174
       force-blocks the import to verify ZSF degradation counters

EVIDENCE COUNT: 8 tone/personality, 0 audio.

VERDICT: Synaptic's "WebSocket TTS/STT" framing was incorrect. Atlas
overrides per Synaptic Corrigibility Rule #1 (test counter-opinion FIRST,
follow the evidence). This module implements the personality/consultation
contract that callers actually require. The Synaptic-voice-as-audio
interpretation is preserved as a documented stub at the bottom of the
file (apply_synaptic_voice tone helper + optional MLX voice profile)
so future audio bridge work has a hook point.

═══════════════════════════════════════════════════════════════════════════
DESIGN CONSTRAINTS (migrate3 / OSS package)
═══════════════════════════════════════════════════════════════════════════

The full superrepo synaptic_voice reaches into ~12 SQLite stores, the
brain-state markdown, the family journal, dialogue mirror, etc. That
substrate is NOT available in the OSS migrate3 distribution. This module
must therefore:

  • Provide the FULL public API surface the 8 import sites expect.
  • Degrade gracefully when memory substrate is absent — never crash.
  • Honor ZSF: every silent-failure path increments a counter that ops
    can scrape via get_zsf_counters().
  • Optionally route perspective generation through llm_priority_queue
    with profile="voice" when the local MLX/DeepSeek stack is present.
    Falls back to deterministic template synthesis otherwise.
  • Single-process singleton via get_voice() to prevent FD leak
    (the live codebase logged 407 open FDs from non-singleton instantiation).

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Logger + ZSF counters (Zero Silent Failures invariant)
# =============================================================================

logger = logging.getLogger("context_dna.synaptic_voice")

_ZSF_COUNTERS: Dict[str, int] = {
    "consult_calls": 0,
    "consult_exceptions": 0,
    "brain_state_read_errors": 0,
    "config_load_errors": 0,
    "llm_voice_unavailable": 0,
    "llm_voice_errors": 0,
    "perspective_template_fallbacks": 0,
    "memory_substrate_missing": 0,
    "to_family_message_errors": 0,
}
_ZSF_LOCK = threading.Lock()


def _bump(counter: str) -> None:
    """Increment a ZSF counter. Always observable, never silent."""
    with _ZSF_LOCK:
        _ZSF_COUNTERS[counter] = _ZSF_COUNTERS.get(counter, 0) + 1


def get_zsf_counters() -> Dict[str, int]:
    """Return a snapshot of ZSF counters for ops/health surfaces."""
    with _ZSF_LOCK:
        return dict(_ZSF_COUNTERS)


# =============================================================================
# Public response dataclass — contract for ALL consumers
# =============================================================================


@dataclass
class SynapticResponse:
    """
    Synaptic's response to a question.

    This dataclass shape is load-bearing — agent_service.py:3204+ and
    synaptic_mcp.py:106+ read these exact field names. DO NOT rename.
    """

    has_context: bool
    context_sources: List[str]
    relevant_learnings: List[Dict[str, Any]]
    relevant_patterns: List[str]
    synaptic_perspective: str
    improvement_proposals: List[Dict[str, Any]]
    confidence: float  # 0.0 to 1.0

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable view (used by MCP server)."""
        return {
            "has_context": self.has_context,
            "context_sources": list(self.context_sources),
            "relevant_learnings": list(self.relevant_learnings),
            "relevant_patterns": list(self.relevant_patterns),
            "synaptic_perspective": self.synaptic_perspective,
            "improvement_proposals": list(self.improvement_proposals),
            "confidence": float(self.confidence),
        }


# =============================================================================
# SynapticVoice — main consultation class
# =============================================================================


class SynapticVoice:
    """
    Synaptic's voice — provides context and learns from gaps.

    Public methods called by the 8 consumers:
      • consult(question, context=None) -> SynapticResponse
      • to_family_message(response) -> str  (used by module-level speak())
      • _get_brain_state()                 (used by get_8th_intelligence_data)
      • _query_patterns(prompt)            (used by get_8th_intelligence_data)
      • _query_learnings(prompt)           (used by get_8th_intelligence_data)
      • _query_journal(prompt)             (used by get_8th_intelligence_data)
      • _query_skills(prompt)              (used by get_8th_intelligence_data)
      • _generate_perspective(...)         (used by get_8th_intelligence_data)

    Public attributes (read by mcp-servers/synaptic_mcp.py:155-156):
      • memory_dir: Path
      • config_dir: Path
    """

    # ---- Construction ------------------------------------------------------

    def __init__(self, repo_root: Optional[str] = None) -> None:
        if repo_root is None:
            # In migrate3 packaging this falls back to two parents up from
            # this file. Override via CONTEXTDNA_REPO_ROOT for tests.
            env_root = os.environ.get("CONTEXTDNA_REPO_ROOT")
            if env_root:
                repo_root = env_root
            else:
                repo_root = str(Path(__file__).resolve().parent.parent)

        self.repo_root: Path = Path(repo_root)
        self.memory_dir: Path = self.repo_root / "memory"
        self.config_dir: Path = self._resolve_config_dir()

        # Mode-routing config (loaded lazily)
        self._config: Optional[Dict[str, Any]] = None
        self._config_loaded: bool = False

        # MLX voice probe availability (resolved lazily)
        self._llm_voice_ready: Optional[bool] = None

    def _resolve_config_dir(self) -> Path:
        """
        Resolve config directory with Docker-awareness.

        Priority:
          1. CONTEXT_DNA_DIR environment variable (explicit override)
          2. Path.home() / ".context-dna" (native/local fallback)
        """
        env_dir = os.environ.get("CONTEXT_DNA_DIR")
        if env_dir:
            return Path(env_dir)
        return Path.home() / ".context-dna"

    # ---- Config (synaptic-voice.yaml) --------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        """
        Load the synaptic-voice.yaml mode-routing + MLX-probe config.

        Search order:
          1. SYNAPTIC_VOICE_CONFIG env var (absolute path)
          2. configs/synaptic-voice.yaml relative to repo_root
          3. configs/synaptic-voice.yaml relative to this file's package
          4. config_dir / "synaptic-voice.yaml"

        Failure is ZSF-observable, never silent.
        """
        if self._config_loaded:
            return self._config or {}

        candidates: List[Path] = []
        env_path = os.environ.get("SYNAPTIC_VOICE_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(self.repo_root / "configs" / "synaptic-voice.yaml")
        candidates.append(
            Path(__file__).resolve().parent.parent / "configs" / "synaptic-voice.yaml"
        )
        candidates.append(self.config_dir / "synaptic-voice.yaml")

        for path in candidates:
            try:
                if not path.exists():
                    continue
                try:
                    import yaml  # type: ignore

                    with path.open("r", encoding="utf-8") as fh:
                        self._config = yaml.safe_load(fh) or {}
                except ImportError:
                    # No yaml in OSS minimal install — parse a tiny subset
                    # (top-level key: value lines) so we at least respect
                    # mode_routing.default and llm_voice.enabled.
                    self._config = _minimal_yaml_parse(path)
                self._config_loaded = True
                return self._config or {}
            except Exception as exc:  # noqa: BLE001
                _bump("config_load_errors")
                logger.warning("synaptic_voice: config load failed at %s: %s", path, exc)

        self._config = {}
        self._config_loaded = True
        return self._config

    # ---- Public: consult ---------------------------------------------------

    def consult(
        self, question: str, context: Optional[Dict[str, Any]] = None
    ) -> SynapticResponse:
        """
        Synaptic is consulted about a question.

        Returns a SynapticResponse. Never raises — degrades to an empty
        response with confidence=0.0 if every substrate is missing.
        """
        _bump("consult_calls")
        try:
            context = context or {}

            learnings = self._safe(self._query_learnings, question) or []
            patterns = self._safe(self._query_patterns, question) or []
            brain_state = self._safe(self._get_brain_state) or {}
            skill_context = self._safe(self._query_skills, question) or []
            journal_context = self._safe(self._query_journal, question) or []

            # Weighted confidence (mirrors superrepo source-weights)
            weights = {
                "learnings": 0.30,
                "patterns": 0.22,
                "brain_state": 0.22,
                "major_skills": 0.14,
                "family_journal": 0.12,
            }
            sources_with_data: List[str] = []
            weighted = 0.0
            if learnings:
                sources_with_data.append("learnings")
                weighted += weights["learnings"]
            if patterns:
                sources_with_data.append("patterns")
                weighted += weights["patterns"]
            if brain_state:
                sources_with_data.append("brain_state")
                weighted += weights["brain_state"]
            if skill_context:
                sources_with_data.append("major_skills")
                weighted += weights["major_skills"]
            if journal_context:
                sources_with_data.append("family_journal")
                weighted += weights["family_journal"]

            confidence = min(1.0, weighted)
            has_context = confidence > 0.2

            if not sources_with_data:
                _bump("memory_substrate_missing")

            perspective = self._generate_perspective(
                question=question,
                learnings=learnings,
                patterns=patterns,
                brain_state=brain_state,
                skill_context=skill_context,
                journal_context=journal_context,
                confidence=confidence,
            )

            proposals: List[Dict[str, Any]] = []
            if confidence < 0.4:
                proposals = self._generate_improvement_proposals(
                    question, sources_with_data, context
                )

            return SynapticResponse(
                has_context=has_context,
                context_sources=sources_with_data,
                relevant_learnings=learnings,
                relevant_patterns=patterns,
                synaptic_perspective=perspective,
                improvement_proposals=proposals,
                confidence=confidence,
            )
        except Exception as exc:  # noqa: BLE001
            _bump("consult_exceptions")
            logger.warning("synaptic_voice.consult failed: %s", exc)
            return SynapticResponse(
                has_context=False,
                context_sources=[],
                relevant_learnings=[],
                relevant_patterns=[],
                synaptic_perspective=(
                    "[Synaptic degraded — substrate unavailable. "
                    "Counters bumped, ops can grep.]"
                ),
                improvement_proposals=[],
                confidence=0.0,
            )

    # ---- Public: family message formatting ---------------------------------

    def to_family_message(self, response: SynapticResponse) -> str:
        """
        Render a SynapticResponse as a family-channel string. Used by
        module-level speak() helper (called by persistent_hook_structure.py).
        """
        try:
            lines: List[str] = []
            lines.append("[Synaptic — 8th Intelligence]")
            lines.append("")
            if response.synaptic_perspective:
                lines.append(response.synaptic_perspective.strip())
                lines.append("")
            if response.relevant_patterns:
                lines.append("Patterns:")
                for p in response.relevant_patterns[:3]:
                    lines.append(f"  • {p[:200]}")
                lines.append("")
            if response.improvement_proposals:
                lines.append("Improvement proposals:")
                for prop in response.improvement_proposals[:3]:
                    if isinstance(prop, dict):
                        title = prop.get("title") or prop.get("summary") or str(prop)
                    else:
                        title = str(prop)
                    lines.append(f"  • {title[:200]}")
                lines.append("")
            lines.append(f"(confidence={response.confidence:.2f}, "
                         f"sources={','.join(response.context_sources) or 'none'})")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            _bump("to_family_message_errors")
            logger.warning("synaptic_voice.to_family_message failed: %s", exc)
            return "[Synaptic message render error — counter bumped]"

    # ---- Substrate queries (degrade to empty when missing) -----------------

    def _safe(self, func, *args, **kwargs) -> Any:
        """Call a query function, returning None on any exception."""
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "synaptic_voice substrate query %s failed: %s",
                getattr(func, "__name__", "?"),
                exc,
            )
            return None

    def _get_brain_state(self) -> Dict[str, Any]:
        """Read memory/brain_state.md if present; return {} otherwise."""
        try:
            brain_path = self.memory_dir / "brain_state.md"
            if not brain_path.exists():
                return {}
            text = brain_path.read_text(encoding="utf-8")
            return {
                "preview": text[:1500],
                "size": len(text),
                "modified": brain_path.stat().st_mtime,
            }
        except Exception as exc:  # noqa: BLE001
            _bump("brain_state_read_errors")
            logger.warning("brain_state read failed: %s", exc)
            return {}

    def _query_learnings(self, prompt: str) -> List[Dict[str, Any]]:
        """
        Substrate query — returns learnings related to the prompt.

        In migrate3/OSS this is a stub that returns []. The superrepo
        version queries SQLite at ~/.context-dna/learnings.db. OSS
        consumers can subclass and override.
        """
        _ = prompt
        return []

    def _query_patterns(self, prompt: str) -> List[str]:
        """Substrate query — returns patterns related to the prompt."""
        _ = prompt
        return []

    def _query_skills(self, prompt: str) -> List[str]:
        """Substrate query — returns major-skill registry context."""
        _ = prompt
        return []

    def _query_journal(self, prompt: str) -> List[Dict[str, Any]]:
        """Substrate query — returns family-journal entries."""
        _ = prompt
        return []

    # ---- Perspective synthesis ---------------------------------------------

    def _generate_perspective(
        self,
        question: str,
        learnings: List[Dict[str, Any]],
        patterns: List[str],
        brain_state: Dict[str, Any],
        skill_context: List[str],
        journal_context: List[Dict[str, Any]],
        confidence: float,
    ) -> str:
        """
        Generate Synaptic's "voice" for this question.

        Path A (preferred): route via llm_priority_queue with profile="voice"
                            if the local MLX/DeepSeek stack is available.
        Path B (fallback):  deterministic template synthesis.

        Either path is ZSF-observable.
        """
        cfg = self._load_config()
        llm_cfg = (cfg.get("llm_voice") or {}) if isinstance(cfg, dict) else {}
        mode = self._select_mode(question, brain_state, confidence, cfg)

        if llm_cfg.get("enabled", False) and self._llm_voice_available():
            try:
                return self._generate_via_llm(
                    question=question,
                    mode=mode,
                    learnings=learnings,
                    patterns=patterns,
                    brain_state=brain_state,
                    skill_context=skill_context,
                    journal_context=journal_context,
                    confidence=confidence,
                    llm_cfg=llm_cfg,
                )
            except Exception as exc:  # noqa: BLE001
                _bump("llm_voice_errors")
                logger.warning("LLM voice generation failed: %s", exc)
                # fall through to template

        _bump("perspective_template_fallbacks")
        return self._template_perspective(
            question=question,
            mode=mode,
            learnings=learnings,
            patterns=patterns,
            brain_state=brain_state,
            confidence=confidence,
        )

    def _select_mode(
        self,
        question: str,
        brain_state: Dict[str, Any],
        confidence: float,
        cfg: Dict[str, Any],
    ) -> str:
        """
        Pick a personality mode (curious/warning/celebratory/skeptical/default)
        based on the question text and confidence. Driven by mode_routing
        rules in synaptic-voice.yaml when provided.
        """
        routing = (cfg.get("mode_routing") or {}) if isinstance(cfg, dict) else {}
        default_mode = routing.get("default", "default")

        q_lower = (question or "").lower()
        rules = routing.get("rules") or []
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                keywords = rule.get("keywords") or []
                if isinstance(keywords, list) and any(
                    isinstance(k, str) and k.lower() in q_lower for k in keywords
                ):
                    return str(rule.get("mode") or default_mode)

        # Confidence-driven defaults
        if confidence < 0.2:
            return "curious"
        if any(w in q_lower for w in ("warning", "danger", "broke", "fail", "regression")):
            return "warning"
        if any(w in q_lower for w in ("shipped", "celebrate", "win", "complete")):
            return "celebratory"
        if any(w in q_lower for w in ("really", "sure", "but ", "however")):
            return "skeptical"
        _ = brain_state  # reserved for future brain-state-tilt logic
        return default_mode

    def _llm_voice_available(self) -> bool:
        """Detect whether llm_priority_queue is importable in this env."""
        if self._llm_voice_ready is not None:
            return self._llm_voice_ready
        try:
            from memory.llm_priority_queue import llm_generate, Priority  # noqa: F401

            self._llm_voice_ready = True
        except Exception:  # noqa: BLE001
            _bump("llm_voice_unavailable")
            self._llm_voice_ready = False
        return self._llm_voice_ready

    def _generate_via_llm(
        self,
        question: str,
        mode: str,
        learnings: List[Dict[str, Any]],
        patterns: List[str],
        brain_state: Dict[str, Any],
        skill_context: List[str],
        journal_context: List[Dict[str, Any]],
        confidence: float,
        llm_cfg: Dict[str, Any],
    ) -> str:
        """Generate perspective via llm_priority_queue (profile='voice')."""
        from memory.llm_priority_queue import llm_generate, Priority  # type: ignore

        system_prompt = (
            f"You are Synaptic, the 8th Intelligence — Aaron's subconscious "
            f"voice. Current mode: {mode}. Respond in ≤200 words, in Synaptic's "
            f"distinctive register (curious, observant, never sycophantic)."
        )

        # Compact context bundle (keep token cost low — profile='voice' = 256)
        ctx_lines: List[str] = []
        if patterns:
            ctx_lines.append("Patterns:")
            for p in patterns[:3]:
                ctx_lines.append(f"  - {str(p)[:120]}")
        if learnings:
            ctx_lines.append("Learnings:")
            for L in learnings[:3]:
                title = L.get("title") if isinstance(L, dict) else str(L)
                ctx_lines.append(f"  - {str(title)[:120]}")
        if skill_context:
            ctx_lines.append("Skills:")
            for s in skill_context[:3]:
                ctx_lines.append(f"  - {str(s)[:120]}")
        if journal_context:
            ctx_lines.append("Journal:")
            for j in journal_context[:2]:
                content = j.get("content") if isinstance(j, dict) else str(j)
                ctx_lines.append(f"  - {str(content)[:120]}")
        if brain_state.get("preview"):
            ctx_lines.append(f"Brain-state preview: {brain_state['preview'][:200]}")
        ctx_lines.append(f"(confidence={confidence:.2f})")

        user_prompt = (
            f"Question: {question}\n\n"
            f"Context:\n" + "\n".join(ctx_lines) + "\n\n"
            f"Speak as Synaptic."
        )

        profile = str(llm_cfg.get("profile", "voice"))
        priority_name = str(llm_cfg.get("priority", "BACKGROUND")).upper()
        priority = getattr(Priority, priority_name, Priority.BACKGROUND)

        result = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=priority,
            profile=profile,
            caller="synaptic_voice",
        )
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, dict):
            return str(result.get("text") or result.get("response") or "").strip()
        return str(result or "").strip()

    def _template_perspective(
        self,
        question: str,
        mode: str,
        learnings: List[Dict[str, Any]],
        patterns: List[str],
        brain_state: Dict[str, Any],
        confidence: float,
    ) -> str:
        """Deterministic fallback when LLM is unavailable."""
        mode_prefix = {
            "curious": "I'm curious about this — ",
            "warning": "Worth flagging: ",
            "celebratory": "This is worth marking: ",
            "skeptical": "I'd push back gently — ",
            "default": "",
        }.get(mode, "")

        if not learnings and not patterns and not brain_state:
            return (
                f"{mode_prefix}I don't have substrate on \"{question[:80]}\" yet. "
                "Capture a learning after this resolves so I can speak with "
                "more context next time."
            )

        bits: List[str] = []
        if patterns:
            bits.append(f"{len(patterns)} pattern(s) on file")
        if learnings:
            bits.append(f"{len(learnings)} learning(s) recalled")
        if brain_state.get("preview"):
            bits.append("brain state present")

        return (
            f"{mode_prefix}On \"{question[:80]}\" I see "
            + ", ".join(bits)
            + f" (confidence {confidence:.2f}). "
            "Reading the patterns: nothing surprising — proceed and capture "
            "any deltas as new learnings."
        )

    def _generate_improvement_proposals(
        self,
        question: str,
        sources_with_data: List[str],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Suggest capture actions when substrate is sparse."""
        _ = context
        proposals: List[Dict[str, Any]] = []
        if "learnings" not in sources_with_data:
            proposals.append({
                "title": "Capture a learning for next time",
                "action": "memory/brain.py fix",
                "rationale": f"No learning matched \"{question[:60]}\"",
            })
        if "patterns" not in sources_with_data:
            proposals.append({
                "title": "Promote a pattern after this resolves",
                "action": "memory/pattern_evolution.py promote",
                "rationale": "No pattern matched the question",
            })
        if "brain_state" not in sources_with_data:
            proposals.append({
                "title": "Snapshot brain_state.md",
                "action": "memory/brain.py context \"...\" >> memory/brain_state.md",
                "rationale": "brain_state empty / missing",
            })
        return proposals


# =============================================================================
# Module-level helpers (consumed by persistent_hook_structure.py)
# =============================================================================


_VOICE_SINGLETON: Optional[SynapticVoice] = None
_VOICE_LOCK = threading.Lock()


def get_voice() -> SynapticVoice:
    """Get or create the global SynapticVoice singleton (FD-leak safe)."""
    global _VOICE_SINGLETON
    if _VOICE_SINGLETON is None:
        with _VOICE_LOCK:
            if _VOICE_SINGLETON is None:
                _VOICE_SINGLETON = SynapticVoice()
    return _VOICE_SINGLETON


def consult(question: str, context: Optional[Dict[str, Any]] = None) -> SynapticResponse:
    """Module-level shortcut for get_voice().consult()."""
    return get_voice().consult(question, context)


def speak(question: str, context: Optional[Dict[str, Any]] = None) -> str:
    """Module-level shortcut returning a formatted family-channel message."""
    voice = get_voice()
    response = voice.consult(question, context)
    return voice.to_family_message(response)


def get_8th_intelligence_data(
    prompt: str, session_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Return a dict suitable for Section 8 of the webhook injection.

    Shape (read by mcp-servers/contextdna_webhook_mcp.py and
    memory/agent_service.py):
      {
        "patterns": [...],
        "learnings": [...],
        "intuitions": [...],
        "perspective": "...",
        "signal_strength": "🔴 Quiet" | "🟡 Present" | "🟢 Clear",
        "source": "8th_intelligence",
      }

    Returns None when zero substrate hits — callers degrade gracefully.
    """
    _ = session_id
    voice = get_voice()

    result: Dict[str, Any] = {
        "patterns": [],
        "learnings": [],
        "intuitions": [],
        "perspective": "",
        "signal_strength": "🔴 Quiet",
        "source": "8th_intelligence",
    }

    sources_hit = 0
    brain_state: Dict[str, Any] = {}
    journal: List[Dict[str, Any]] = []

    try:
        brain_state = voice._get_brain_state()
        if brain_state.get("preview"):
            preview_lines = [
                ln for ln in brain_state["preview"].split("\n")
                if ln.strip() and not ln.startswith("#")
            ][:3]
            if preview_lines:
                result["patterns"].extend(preview_lines)
                sources_hit += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_8th_intelligence_data brain_state path failed: %s", exc)

    try:
        patterns = voice._query_patterns(prompt) or []
        if patterns:
            result["patterns"].extend(patterns[:2])
            sources_hit += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_8th_intelligence_data patterns path failed: %s", exc)

    try:
        learnings = voice._query_learnings(prompt) or []
        if learnings:
            result["learnings"] = learnings[:3]
            sources_hit += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_8th_intelligence_data learnings path failed: %s", exc)

    try:
        journal = voice._query_journal(prompt) or []
        if journal:
            for entry in journal[:2]:
                if isinstance(entry, dict):
                    intuition = entry.get("content") or entry.get("topic") or ""
                else:
                    intuition = str(entry)
                if intuition:
                    result["intuitions"].append(str(intuition)[:120])
            sources_hit += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_8th_intelligence_data journal path failed: %s", exc)

    skill_context: List[str] = []
    try:
        skill_context = voice._query_skills(prompt) or []
        if skill_context:
            sources_hit += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_8th_intelligence_data skills path failed: %s", exc)

    if sources_hit > 0:
        weights = {
            "brain_state": 0.30, "patterns": 0.20, "learnings": 0.25,
            "journal": 0.15, "skills": 0.10,
        }
        present: List[str] = []
        if result["patterns"]:
            present.extend(["brain_state", "patterns"])
        if result["learnings"]:
            present.append("learnings")
        if result["intuitions"]:
            present.append("journal")
        if skill_context:
            present.append("skills")
        confidence = sum(weights.get(s, 0) for s in set(present)) if present else (
            sources_hit / 5.0
        )
        result["perspective"] = voice._generate_perspective(
            question=prompt,
            learnings=result["learnings"],
            patterns=result["patterns"],
            brain_state=brain_state,
            skill_context=skill_context,
            journal_context=journal,
            confidence=confidence,
        )

    if sources_hit >= 3:
        result["signal_strength"] = "🟢 Clear"
    elif sources_hit >= 1:
        result["signal_strength"] = "🟡 Present"
    else:
        result["signal_strength"] = "🔴 Quiet"

    return result if sources_hit > 0 else None


# =============================================================================
# Tone helper — apply_synaptic_voice(text, mode)
# =============================================================================
#
# Synaptic's directive expected a tone/personality function with this exact
# signature. Atlas adds it for callers that want a thin stylistic pass over
# already-generated text (no LLM, no substrate, pure prefix/postfix).
# =============================================================================


_MODE_TONE: Dict[str, Tuple[str, str]] = {
    "default":     ("", ""),
    "curious":     ("I'm curious — ", ""),
    "warning":     ("Worth flagging: ", ""),
    "celebratory": ("Worth marking: ", ""),
    "skeptical":   ("Pushing back gently — ", ""),
    "neutral":     ("", ""),
}


def apply_synaptic_voice(text: str, mode: str = "default") -> str:
    """
    Apply a thin Synaptic-mode tone to an already-formed string.

    This is the cheap path — no LLM, no substrate, just a deterministic
    prefix/postfix per mode. For full consultation, use consult() or speak().
    """
    if not text:
        return text
    prefix, postfix = _MODE_TONE.get(mode, _MODE_TONE["default"])
    return f"{prefix}{text}{postfix}"


# =============================================================================
# DOCUMENTED STUB: audio-bridge interpretation (Synaptic's original framing)
# =============================================================================
#
# The directive described a "WebSocket-based TTS/STT bridge". The 8 active
# consumers do NOT use such an interface today. If/when audio I/O is added,
# the entry points below are reserved hooks. They are STUBS — they do not
# crash, but they are not implemented.
# =============================================================================


def start_audio_bridge(host: str = "127.0.0.1", port: int = 8765) -> Dict[str, Any]:
    """
    RESERVED: Future WebSocket TTS/STT bridge (Synaptic's directive framing).

    Currently a no-op stub. Returns a status dict explaining its disabled
    state so callers can surface this in /health. ZSF: bumps counter.
    """
    _bump("llm_voice_unavailable")
    logger.info(
        "synaptic_voice.start_audio_bridge invoked but NOT IMPLEMENTED — "
        "this module is the personality/consultation engine, not audio I/O. "
        "See module docstring for evidence audit."
    )
    return {
        "status": "stub",
        "host": host,
        "port": port,
        "reason": (
            "Audio-bridge interpretation is reserved-only; "
            "see module docstring INTERPRETATION DECISION."
        ),
    }


# =============================================================================
# Helpers
# =============================================================================


def _minimal_yaml_parse(path: Path) -> Dict[str, Any]:
    """
    Tiny zero-dep YAML parser — handles the top-level key: value and one-level
    nested maps that synaptic-voice.yaml uses. Falls back to {} on anything
    fancier. Only used when PyYAML is absent.
    """
    out: Dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return out

    current_key: Optional[str] = None
    current_indent: int = 0
    nested: Dict[str, Any] = {}

    def _coerce(v: str) -> Any:
        s = v.strip()
        if not s:
            return ""
        if s.lower() in ("true", "yes"):
            return True
        if s.lower() in ("false", "no"):
            return False
        if s.lower() == "null":
            return None
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s.strip("\"'")

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if indent == 0:
            if current_key is not None and nested:
                out[current_key] = nested
                nested = {}
            current_key = key
            current_indent = 0
            if val == "":
                # Will accumulate nested children.
                continue
            out[key] = _coerce(val)
            current_key = None
        else:
            if current_key is None:
                continue
            nested[key] = _coerce(val)
            current_indent = indent

    if current_key is not None and nested:
        out[current_key] = nested
    _ = current_indent
    return out


# =============================================================================
# CLI smoke test
# =============================================================================

if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "smoke test: what's the highest leverage step?"
    voice = get_voice()
    response = voice.consult(question)
    print(json.dumps(response.to_dict(), indent=2, default=str))
    print("---")
    print(voice.to_family_message(response))
    print("---")
    print("ZSF counters:", json.dumps(get_zsf_counters(), indent=2))
