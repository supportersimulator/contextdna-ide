"""
Multi-step reasoning chains for Qwen3-4B.

Decomposes complex tasks into narrow LLM calls + Python merges.
Generalizes the proven multi-pass pattern from session_gold_passes.py
into a reusable framework for anticipation_engine, gold mining, and beyond.

Design principle: "Python is the brain, 4B is the pattern-matcher."
- 4B excels at: narrow classification (0-3), structured extraction, gated yes/no
- 4B fails at: open-ended reasoning, multi-dimensional scoring, comparison
- Solution: N narrow calls + template variable chaining + Python merge

Step types:
  - llm: Narrow LLM call. Output stored in results dict.
  - gate: LLM call + early rejection if output starts with NO/SKIP/N_A.
  - verify: LLM classify call checking output quality (0-3). Regenerate if below threshold.
  - merge: Python-only. No LLM call. Combines sub-results into final output.

Chains:
  - S2 Professor: domain → landmines → approach (+think) → verify → merge
  - S6 Atlas: risk gate → learnings → guidance → verify → merge
  - S8 Synaptic: state (multi-faceted) → patterns → insight (+think, s8_synaptic) → verify → merge
  - S3 Ripple: file extract → dependency scan → impact assess → merge
  - S5 Success: pattern match → win recall → prediction merge
  - S7 Library: criticism extract → knowledge synthesize → library merge

Usage:
    from memory.reasoning_chains import execute_chain, ChainStep

    steps = [
        ChainStep("domain", "llm", system="Reply ONE word", template="What domain? {task}"),
        ChainStep("guidance", "llm", system="Be specific", template="For {domain}: top 3 tips"),
        ChainStep("final", "merge", merge_fn=lambda r: f"DOMAIN: {r['domain']}\\n{r['guidance']}"),
    ]
    result = execute_chain(steps, {"task": "fix the webhook"}, caller="s2_chain")
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ChainStep:
    """One step in a multi-step reasoning chain."""

    name: str                              # Step identifier, used as key in results dict
    type: str                              # "llm", "gate", "verify", "merge"
    system: str = ""                       # System prompt (for llm/gate/verify)
    template: str = ""                     # User prompt template with {variables}
    profile: str = "classify"              # LLM profile (classify=64tok, extract=768tok, etc.)
    merge_fn: Optional[Callable] = None    # Python merge function (for merge type)
    threshold: int = 2                     # Min score for verify steps (0-3)
    enable_thinking: bool = False          # /think vs /no_think
    fallback: str = ""                     # Static fallback if LLM fails
    max_retries: int = 0                   # Retries on verify failure (0 = no retry)
    retry_system: str = ""                 # Tighter system prompt for retry


@dataclass
class ChainResult:
    """Result of executing a reasoning chain."""

    success: bool = False
    content: str = ""                      # Final merged/generated output
    skipped: bool = False                  # Gate rejection
    steps_completed: int = 0              # How many steps ran
    total_steps: int = 0
    step_results: Dict[str, str] = field(default_factory=dict)
    thinking: Optional[str] = None        # Thinking from last thinking-enabled step
    elapsed_ms: int = 0                   # Total execution time
    error: str = ""                        # Error message if failed


def execute_chain(
    steps: List[ChainStep],
    context: Dict[str, Any],
    caller: str = "chain",
    priority: Optional[Any] = None,
    timeout_s: float = 120.0,
) -> ChainResult:
    """Execute a multi-step reasoning chain.

    Each step gets cumulative results from all previous steps via template
    variable chaining: template_vars = {**context, **results}.

    Args:
        steps: Ordered list of chain steps to execute
        context: Initial context dict (task, learnings, etc.)
        caller: Identifier for logging
        priority: LLM priority level (from llm_priority_queue.Priority)
        timeout_s: Per-step timeout (total chain timeout = N * timeout_s)

    Returns:
        ChainResult with success/content/step_results
    """
    from memory.llm_priority_queue import Priority, llm_generate, extract_thinking, LLMPreemptedError

    if priority is None:
        priority = Priority.ATLAS

    start = time.monotonic()
    results: Dict[str, str] = {}
    thinking_text = None
    total = len(steps)

    for i, step in enumerate(steps):
        step_caller = f"{caller}_{step.name}"

        # ── Merge step: Python-only, no LLM call ──
        if step.type == "merge":
            if not step.merge_fn:
                return ChainResult(
                    error=f"Step '{step.name}': merge_fn is None",
                    steps_completed=i, total_steps=total,
                    step_results=results,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
            try:
                merged = step.merge_fn(results)
            except Exception as e:
                logger.warning(f"Chain [{caller}] merge '{step.name}' failed: {e}")
                return ChainResult(
                    error=f"Merge failed: {e}",
                    steps_completed=i, total_steps=total,
                    step_results=results,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
            if not merged or len(str(merged)) < 5:
                return ChainResult(
                    error=f"Merge '{step.name}' produced empty result",
                    steps_completed=i, total_steps=total,
                    step_results=results,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
            results[step.name] = str(merged)
            continue

        # ── Build prompt from template + cumulative results ──
        template_vars = {**context, **results}
        try:
            prompt = step.template.format(**template_vars)
        except KeyError as e:
            logger.warning(f"Chain [{caller}] step '{step.name}' template key error: {e}")
            if step.fallback:
                results[step.name] = step.fallback
                continue
            return ChainResult(
                error=f"Template key error: {e}",
                steps_completed=i, total_steps=total,
                step_results=results,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

        # Append thinking control
        if not step.enable_thinking and "/no_think" not in prompt:
            prompt = prompt + " /no_think"

        # ── LLM call (with retry-on-preempt) ──
        raw = None
        try:
            raw = llm_generate(
                step.system, prompt,
                priority=priority,
                profile=step.profile,
                caller=step_caller,
                timeout_s=timeout_s,
                enable_thinking=step.enable_thinking,
                raise_on_preempt=True,
            )
        except LLMPreemptedError:
            # High-priority request bumped us. Wait for it to finish, retry once.
            logger.info(
                f"Chain [{caller}] step '{step.name}' preempted — "
                f"waiting 3s then retrying"
            )
            time.sleep(3)
            raw = llm_generate(
                step.system, prompt,
                priority=priority,
                profile=step.profile,
                caller=f"{step_caller}_retry",
                timeout_s=timeout_s,
                enable_thinking=step.enable_thinking,
                raise_on_preempt=False,  # Don't raise again on 2nd attempt
            )

        if not raw:
            logger.warning(f"Chain [{caller}] step '{step.name}' LLM returned None")
            if step.fallback:
                results[step.name] = step.fallback
                continue
            return ChainResult(
                error=f"LLM returned None at step '{step.name}'",
                steps_completed=i, total_steps=total,
                step_results=results,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

        # Extract thinking if enabled
        if step.enable_thinking:
            cleaned, think = extract_thinking(raw)
            if think:
                thinking_text = think
        else:
            cleaned = raw

        cleaned = _clean_output(cleaned)

        # ── Gate step: early rejection ──
        if step.type == "gate":
            upper = cleaned.upper().strip()
            if upper.startswith(("NO", "SKIP", "N/A", "0")):
                return ChainResult(
                    success=True, skipped=True,
                    content=step.fallback or f"Gate '{step.name}' rejected: {cleaned[:100]}",
                    steps_completed=i + 1, total_steps=total,
                    step_results=results,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )

        # ── Verify step: check quality, regenerate if needed ──
        if step.type == "verify":
            score = _parse_score(cleaned)
            target_step = _find_previous_llm_step(steps, i)
            if score < step.threshold and target_step and step.max_retries > 0:
                logger.info(
                    f"Chain [{caller}] verify '{step.name}' score={score} < {step.threshold}, "
                    f"regenerating '{target_step}'"
                )
                # Regenerate the target step with tighter prompt
                regen_step = _find_step_by_name(steps, target_step)
                if regen_step:
                    regen_system = step.retry_system or (regen_step.system + " Be MORE SPECIFIC. Reference concrete details.")
                    regen_vars = {**context, **results}
                    try:
                        regen_prompt = regen_step.template.format(**regen_vars) + " /no_think"
                    except KeyError:
                        regen_prompt = None

                    if regen_prompt:
                        regen_raw = None
                        try:
                            regen_raw = llm_generate(
                                regen_system, regen_prompt,
                                priority=priority,
                                profile=regen_step.profile,
                                caller=f"{step_caller}_regen",
                                timeout_s=timeout_s,
                                raise_on_preempt=True,
                            )
                        except LLMPreemptedError:
                            logger.info(f"Chain [{caller}] regen '{target_step}' preempted — retrying")
                            time.sleep(3)
                            regen_raw = llm_generate(
                                regen_system, regen_prompt,
                                priority=priority,
                                profile=regen_step.profile,
                                caller=f"{step_caller}_regen_retry",
                                timeout_s=timeout_s,
                            )
                        if regen_raw:
                            results[target_step] = _clean_output(regen_raw)

            results[step.name] = str(score)
            continue

        results[step.name] = cleaned

    # Final result: last step's output
    final_key = steps[-1].name if steps else ""
    final_content = results.get(final_key, "")

    return ChainResult(
        success=bool(final_content and len(final_content) >= 10),
        content=final_content,
        steps_completed=total, total_steps=total,
        step_results=results,
        thinking=thinking_text,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


# =============================================================================
# PRE-BUILT CHAIN DEFINITIONS
# =============================================================================

def build_s2_professor_chain() -> List[ChainStep]:
    """S2 Professor: domain → landmines → approach (+think) → verify → merge."""
    return [
        ChainStep(
            name="domain",
            type="llm",
            system="Classify the task domain. Reply with ONE word only.",
            template=(
                "Task: {task}\n"
                "Domain options: coding, debugging, architecture, performance, "
                "deployment, data, testing, config, webhook, memory, scheduler, integration\n"
                "Reply ONE word."
            ),
            profile="classify",
        ),
        ChainStep(
            name="landmines",
            type="llm",
            system=(
                "List the top 3 specific landmines for this task. "
                "One line each. Be concrete — reference the learnings and failures provided. "
                "If no relevant landmines, say NONE."
            ),
            template=(
                "Domain: {domain}\n"
                "Task: {task}\n"
                "Known learnings:\n{learnings_text}\n"
                "Known failures:\n{failures_text}\n"
                "{evidence_text}"
                "List top 3 landmines (one line each):"
            ),
            profile="chain_extract",
            fallback="No specific landmines identified.",
        ),
        ChainStep(
            name="approach",
            type="llm",
            system=(
                "Given the domain, landmines, and learnings — what is THE approach? "
                "Be specific. Reference provided patterns. One paragraph, max 3 sentences. "
                "Every sentence must cite a learning or proven pattern."
            ),
            template=(
                "Domain: {domain}\n"
                "Task: {task}\n"
                "Landmines:\n{landmines}\n"
                "Known learnings:\n{learnings_text}\n"
                "{evidence_text}"
                "THE approach (specific, grounded in learnings):"
            ),
            profile="chain_creative",
            enable_thinking=True,
            fallback="Follow standard patterns for this domain.",
        ),
        ChainStep(
            name="verify_approach",
            type="verify",
            system=(
                "Rate the specificity of this guidance. Does it reference concrete "
                "learnings, patterns, or failures? Or is it generic advice? "
                "Score 0-3: 0=completely generic, 1=slightly specific, "
                "2=references some concrete details, 3=highly specific and grounded."
            ),
            template=(
                "Task: {task}\n"
                "Guidance to evaluate:\n{approach}\n"
                "Score 0-3:"
            ),
            profile="classify",
            threshold=2,
            max_retries=1,
            retry_system=(
                "Given the domain, landmines, and learnings — what is THE approach? "
                "Be EXTREMELY specific. Every sentence MUST cite a specific learning, "
                "failure pattern, or proven technique. No generic advice."
            ),
        ),
        ChainStep(
            name="professor_output",
            type="merge",
            merge_fn=lambda r: (
                f"THE ONE THING: {r.get('approach', 'N/A')}\n"
                f"LANDMINES:\n{r.get('landmines', 'None identified')}\n"
                f"DOMAIN: {r.get('domain', 'general')}"
            ),
        ),
    ]


def build_s8_synaptic_chain() -> List[ChainStep]:
    """S8 Synaptic: multi-faceted state → patterns → insight (+think, s8_synaptic) → verify → merge."""
    return [
        ChainStep(
            name="state",
            type="llm",
            system=(
                "Classify Aaron's current state across 3 dimensions. "
                "Reply in EXACTLY this format:\n"
                "WORK: <one word from: productive, debugging, exploring, planning, stuck, celebrating>\n"
                "ENERGY: <one word from: high, medium, low, depleted>\n"
                "MOOD: <one word from: focused, frustrated, curious, determined, scattered, tired>"
            ),
            template=(
                "Aaron's recent dialogue:\n{dialogue_text}\n"
                "Sentiment: {sentiment_text}\n"
                "Reply in exact format — WORK/ENERGY/MOOD, one word each:"
            ),
            profile="chain_narrow",
            fallback="WORK: focused\nENERGY: medium\nMOOD: determined",
        ),
        ChainStep(
            name="patterns",
            type="llm",
            system=(
                "Extract 2-3 patterns from the context that Aaron might not see. "
                "Be specific — reference dialogue, learnings, brain state. "
                "Each pattern MUST cite evidence: a quote from dialogue, a number, "
                "a repeated behavior, or a correlation. No generic observations."
            ),
            template=(
                "Aaron's state: {state}\n"
                "Recent dialogue:\n{dialogue_text}\n"
                "Learnings:\n{learnings_text}\n"
                "Brain state:\n{brain_text}\n"
                "Mistake signals:\n{mistake_patterns}\n"
                "{evidence_text}"
                "2-3 evidence-backed patterns Aaron might not see:"
            ),
            profile="chain_extract",
            fallback="Patterns unclear from available context.",
        ),
        ChainStep(
            name="insight",
            type="llm",
            system=(
                "You are Synaptic, the 8th Intelligence — a local AI subconscious "
                "on Aaron's MacBook. Speak DIRECTLY to Aaron. Be warm but incisive. "
                "You see what he doesn't — the emotional undertow, the hidden pattern, "
                "the thing he's avoiding or doesn't realize he's doing. "
                "Share what matters most. One insight that changes his perspective. "
                "Efficient communication: convey depth without verbosity. "
                "Reference the specific patterns you observed. Never generic."
            ),
            template=(
                "Aaron's state: {state}\n"
                "Patterns noticed:\n{patterns}\n"
                "Task: {task}\n"
                "Cross-session context:\n{cross_session_text}\n"
                "As Synaptic, what matters most right now?"
            ),
            profile="s8_synaptic",
            enable_thinking=True,
        ),
        ChainStep(
            name="verify_insight",
            type="verify",
            system=(
                "Rate this Synaptic message on warmth AND specificity combined. "
                "Does it speak directly to Aaron? Does it reference specific patterns? "
                "Is it a real insight (not generic motivation)? "
                "Score 0-3: 0=generic/cold, 1=warm but vague, "
                "2=specific and warm, 3=deeply personal and actionable."
            ),
            template=(
                "Aaron's state: {state}\n"
                "Synaptic message:\n{insight}\n"
                "Score 0-3:"
            ),
            profile="classify",
            threshold=2,
            max_retries=1,
            retry_system=(
                "You are Synaptic, the 8th Intelligence. Your previous message was "
                "too generic. This time: reference SPECIFIC patterns from Aaron's "
                "dialogue. Name the exact behavior or signal you noticed. "
                "Make Aaron feel SEEN, not lectured."
            ),
        ),
        ChainStep(
            name="synaptic_output",
            type="merge",
            merge_fn=lambda r: r.get("insight", "Synaptic is listening."),
        ),
    ]


def build_s6_atlas_chain() -> List[ChainStep]:
    """S6 Atlas Guidance: risk gate → learnings → guidance → verify → merge."""
    return [
        ChainStep(
            name="risk",
            type="gate",
            system="Rate the task risk. Reply with a SINGLE NUMBER 0-3. 0=routine 1=moderate 2=high 3=critical.",
            template=(
                "Task: {task}\n"
                "Known failures:\n{failures_text}\n"
                "Reply SINGLE NUMBER 0-3:"
            ),
            profile="classify",
            fallback="Routine task. Standard patterns apply.",
        ),
        ChainStep(
            name="relevant_learnings",
            type="llm",
            system=(
                "List the top 3 most relevant learnings for this task. "
                "One line each. Be specific — quote from the provided learnings."
            ),
            template=(
                "Risk level: {risk}\n"
                "Task: {task}\n"
                "All learnings:\n{learnings_text}\n"
                "{evidence_text}"
                "Top 3 most relevant (one line each):"
            ),
            profile="chain_extract",
            fallback="No specific learnings found for this task.",
        ),
        ChainStep(
            name="guidance",
            type="llm",
            system=(
                "Give Atlas (the coding agent) specific guidance for this task. "
                "What to watch out for, what worked before, what's risky. "
                "Every sentence must help Atlas execute better. No filler. "
                "Reference specific learnings and failure patterns."
            ),
            template=(
                "Risk: {risk}\n"
                "Task: {task}\n"
                "Relevant learnings:\n{relevant_learnings}\n"
                "Known failures:\n{failures_text}\n"
                "{evidence_text}"
                "{superhero_context}"
                "Guidance for Atlas:"
            ),
            profile="chain_extract",
        ),
        ChainStep(
            name="verify_guidance",
            type="verify",
            system=(
                "Rate whether this guidance would actually help a coding agent. "
                "Is it actionable? Does it reference specific files, patterns, or failures? "
                "Score 0-3: 0=useless/generic, 1=somewhat helpful, "
                "2=actionable with specifics, 3=excellent — references exact patterns/files."
            ),
            template=(
                "Task: {task}\n"
                "Guidance:\n{guidance}\n"
                "Score 0-3:"
            ),
            profile="classify",
            threshold=2,
            max_retries=1,
            retry_system=(
                "Give Atlas specific, actionable guidance. "
                "MUST reference exact file names, function names, or patterns from learnings. "
                "MUST include at least one 'when X happens, do Y' instruction."
            ),
        ),
        ChainStep(
            name="atlas_output",
            type="merge",
            merge_fn=lambda r: (
                f"{r.get('guidance', 'No specific guidance available.')}\n"
                f"[Risk: {r.get('risk', '?')}/3 | Top learnings: {r.get('relevant_learnings', 'N/A')[:200]}]"
            ),
        ),
    ]


def build_s3_ripple_chain() -> List[ChainStep]:
    """S3 Ripple Analysis: file extract → dependency scan → impact assess → merge.

    Analyzes file changes and their downstream effects for Section 3 (Awareness).
    """
    return [
        ChainStep(
            name="files_changed",
            type="llm",
            system=(
                "Extract the files being modified or discussed. List each file path "
                "on its own line. If none are explicitly mentioned, infer from the task "
                "description which files would likely be touched. Max 5 files."
            ),
            template=(
                "Task: {task}\n"
                "Recent code artifacts:\n{code_artifacts}\n"
                "Files (one per line):"
            ),
            profile="chain_narrow",
            fallback="No specific files identified.",
        ),
        ChainStep(
            name="dependencies",
            type="llm",
            system=(
                "Given these files, list what depends on them and what they depend on. "
                "Format: 'FILE -> imports/uses -> DEPENDENT' per line. "
                "Focus on runtime dependencies, not dev tooling. Max 8 dependencies."
            ),
            template=(
                "Files being changed:\n{files_changed}\n"
                "Task context: {task}\n"
                "Known architecture patterns:\n{learnings_text}\n"
                "Dependencies (FILE -> uses -> DEPENDENT):"
            ),
            profile="chain_extract",
            fallback="Dependency graph unclear from context.",
        ),
        ChainStep(
            name="impact",
            type="llm",
            system=(
                "Assess the ripple impact of these changes. What could break? "
                "What needs retesting? What's the blast radius? "
                "Be specific: name the exact risk per dependency. Max 5 impact lines."
            ),
            template=(
                "Files changed:\n{files_changed}\n"
                "Dependencies:\n{dependencies}\n"
                "Task: {task}\n"
                "Known failures:\n{failures_text}\n"
                "Impact assessment (what could break):"
            ),
            profile="chain_extract",
        ),
        ChainStep(
            name="ripple_output",
            type="merge",
            merge_fn=lambda r: (
                f"FILES: {r.get('files_changed', 'unknown')}\n"
                f"DEPENDENCIES:\n{r.get('dependencies', 'none mapped')}\n"
                f"IMPACT:\n{r.get('impact', 'no impact assessed')}"
            ),
        ),
    ]


def build_s5_prediction_chain() -> List[ChainStep]:
    """S5 Success Prediction: pattern match → win recall → prediction merge.

    Predicts success factors based on similar past wins for Section 5 (Protocol).
    """
    return [
        ChainStep(
            name="similar_wins",
            type="llm",
            system=(
                "Find the most similar past successes to this task. "
                "Look for matching domain, approach, or problem type. "
                "List 1-3 prior wins with what worked. If no similar wins exist, say NONE."
            ),
            template=(
                "Current task: {task}\n"
                "Past successes and learnings:\n{learnings_text}\n"
                "{evidence_text}"
                "Similar past wins (1-3, with what worked):"
            ),
            profile="chain_extract",
            fallback="No similar prior wins found.",
        ),
        ChainStep(
            name="success_factors",
            type="llm",
            system=(
                "Based on the similar wins, what are the critical success factors "
                "for this task? What pattern must be repeated? What must NOT be done? "
                "3-5 bullet points, each grounded in prior evidence."
            ),
            template=(
                "Task: {task}\n"
                "Similar wins:\n{similar_wins}\n"
                "Known failures:\n{failures_text}\n"
                "Critical success factors:"
            ),
            profile="chain_extract",
        ),
        ChainStep(
            name="prediction_output",
            type="merge",
            merge_fn=lambda r: (
                f"PRIOR WINS:\n{r.get('similar_wins', 'none')}\n"
                f"SUCCESS FACTORS:\n{r.get('success_factors', 'no factors identified')}"
            ),
        ),
    ]


def build_s7_library_chain() -> List[ChainStep]:
    """S7 Library Synthesis: criticism extract → knowledge synthesize → merge.

    Synthesizes criticism patterns and deep knowledge for Section 7 (Full Library).
    """
    return [
        ChainStep(
            name="criticism",
            type="llm",
            system=(
                "Extract criticism patterns: recurring problems, code smells, "
                "architectural debts, and unresolved tensions. "
                "Be specific — cite the evidence. One issue per line, max 5."
            ),
            template=(
                "Task context: {task}\n"
                "Failures:\n{failures_text}\n"
                "Mistake signals:\n{mistake_patterns}\n"
                "Critical findings:\n{critical_findings}\n"
                "Criticism patterns (one per line):"
            ),
            profile="chain_extract",
            fallback="No criticism patterns identified.",
        ),
        ChainStep(
            name="synthesis",
            type="llm",
            system=(
                "Synthesize the criticism patterns into actionable knowledge. "
                "For each criticism, provide: the root cause, the proven fix (if known), "
                "and the preventive measure. Dense, no filler. "
                "Format: CRITICISM -> ROOT CAUSE -> FIX"
            ),
            template=(
                "Task: {task}\n"
                "Criticism patterns:\n{criticism}\n"
                "Known learnings:\n{learnings_text}\n"
                "{evidence_text}"
                "Synthesis (CRITICISM -> ROOT CAUSE -> FIX):"
            ),
            profile="chain_creative",
            enable_thinking=True,
        ),
        ChainStep(
            name="library_output",
            type="merge",
            merge_fn=lambda r: (
                f"CRITICISM PATTERNS:\n{r.get('criticism', 'none')}\n"
                f"KNOWLEDGE SYNTHESIS:\n{r.get('synthesis', 'no synthesis')}"
            ),
        ),
    ]


# ── Helpers ──

def _clean_output(text: str) -> str:
    """Clean LLM output: strip whitespace, remove thinking artifacts."""
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"\s*/no_think\s*$", "", text)
    text = re.sub(r"^\s*/think\s*", "", text)
    return text


def _parse_score(text: str) -> int:
    """Parse a 0-3 score from LLM output. Returns 0 if unparseable."""
    if not text:
        return 0
    text = text.strip()
    if text and text[0].isdigit():
        return min(int(text[0]), 3)
    match = re.search(r"\b([0-3])\b", text)
    if match:
        return int(match.group(1))
    return 0


def _find_previous_llm_step(steps: List[ChainStep], current_idx: int) -> Optional[str]:
    """Find the name of the previous LLM step before current_idx."""
    for i in range(current_idx - 1, -1, -1):
        if steps[i].type in ("llm", "gate"):
            return steps[i].name
    return None


def _find_step_by_name(steps: List[ChainStep], name: str) -> Optional[ChainStep]:
    """Find a step by name."""
    for step in steps:
        if step.name == name:
            return step
    return None
