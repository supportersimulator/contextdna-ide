#!/usr/bin/env python3
"""
LLM SOP ENHANCER - Ollama-Powered SOP Title Generation

This module provides LLM-enhanced SOP title generation using local Ollama.
It AUGMENTS the rule-based enhancers with semantic understanding.

PHILOSOPHY:
- Rule-based is FAST and deterministic → use for clear cases
- LLM is SMART but slower → use for ambiguous cases
- Hybrid approach: rules first, LLM fallback for low-confidence

WHEN LLM IS USED:
1. Type classification is ambiguous (bugfix/process scores within 2 points)
2. Zone extraction yields poor results (< 2 zones detected)
3. Title quality score is low (lacks specificity)

SETUP:
1. Start Ollama: ollama serve
2. Pull model: ollama pull llama3.1:8b
3. Import and use: from context_dna.llm_sop_enhancer import enhance_with_llm

Usage:
    from context_dna.llm_sop_enhancer import (
        is_ollama_available,
        enhance_sop_type_with_llm,
        enhance_zones_with_llm,
        enhance_title_with_llm,
        generate_sop_title_llm_hybrid
    )

    # Check availability
    if is_ollama_available():
        # Enhance ambiguous type classification
        sop_type, confidence = enhance_sop_type_with_llm(content, scores)

        # Enhance zone extraction
        zones = enhance_zones_with_llm(content, sop_type)

        # Full hybrid generation
        title = generate_sop_title_llm_hybrid(task, details)
"""

import re
import json
from typing import Optional, Tuple, Dict, List
from dataclasses import dataclass

# Import local LLM client
try:
    from memory.local_llm_analyzer import LocalLLMClient, llm_client
    LLM_CLIENT_AVAILABLE = True
except ImportError:
    try:
        # Fallback for different import paths
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "memory"))
        from local_llm_analyzer import LocalLLMClient, llm_client
        LLM_CLIENT_AVAILABLE = True
    except ImportError:
        LLM_CLIENT_AVAILABLE = False
        llm_client = None


# =============================================================================
# OLLAMA STATUS
# =============================================================================

def is_ollama_available() -> bool:
    """Check if Ollama is running and accessible."""
    if not LLM_CLIENT_AVAILABLE:
        return False
    if llm_client is None:
        return False
    return llm_client.available


def get_ollama_status() -> Dict:
    """Get detailed Ollama status."""
    if not LLM_CLIENT_AVAILABLE or llm_client is None:
        return {
            "available": False,
            "reason": "LLM client not imported",
            "models": []
        }

    return llm_client.get_status()


# =============================================================================
# LLM PROMPTS
# =============================================================================

TYPE_CLASSIFICATION_PROMPT = """Classify this development task as either a BUG-FIX or PROCESS SOP.

Consider whatever seems relevant to you:

- **What's the core issue?** Is this describing a problem that was solved, or a procedure for how to do something?
- **What are the structural clues?** Does it have symptoms/root-cause/fix (bugfix) or steps/tools/verification (process)?
- **What's the scope?** Is this convergent (one problem → one solution) or divergent (one goal → many routes)?
- **How confident are you?** What characteristics make this clearly one type vs the other?

TASK: {task}

DETAILS: {details}

Share your analysis in whatever way makes sense to you. Both JSON-formatted and natural language analyses are equally useful.

Format suggestion (if JSON makes sense):
{{
    "type": "bugfix" or "process",
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of why this classification"
}}

Or natural language: just describe what type you believe this is and why."""

ZONE_EXTRACTION_PROMPT = """Extract the key structural elements from this {sop_type} SOP content.

Consider whatever seems relevant to you:

{zone_instructions}

CONTENT:
{content}

Share your analysis in whatever way makes sense to you. Both JSON-formatted and natural language structures are equally useful.

Format suggestion (if JSON makes sense):
{json_template}

Or natural language: just describe the key elements you identify."""

BUGFIX_ZONE_INSTRUCTIONS = """For a BUG-FIX SOP, extract:
- bad_sign: The observable symptom (what you SEE - timeout, crash, error, slow)
- antecedent: The contributing factor (what CAUSED it - blocking, sync, missing)
- fix: The treatment action (HOW to fix - wrap, add, configure)
- stack: The tools involved (boto3, docker, terraform)
- outcome: The desired state (responsive, working, healthy)"""

BUGFIX_JSON_TEMPLATE = """{
    "bad_sign": ["symptom1", "symptom2"],
    "antecedent": ["cause1", "cause2"],
    "fix": ["action1", "action2"],
    "stack": ["tool1", "tool2"],
    "outcome": ["desired_state"]
}"""

PROCESS_ZONE_INSTRUCTIONS = """For a PROCESS SOP, extract:
- tools: The tools/methods available (systemctl, docker, terraform)
- steps: The sequence of actions (backup, deploy, verify)
- verification: How to confirm success (200 OK, healthy, logs clean)

SOP QUALITY FORMAT NOTE:
Process SOPs use a two-section layout:
1. TOP: What WORKS (routes ranked by preference, preferred method first)
2. BOTTOM (after divider): What DOESN'T work (only if pattern seen 3+ times)

The divider line is: ─── What doesn't work (learned from experience) ───
Anti-patterns below are prefixed with ✗.

Example:
  Route 1 (preferred): via (systemctl) → restart → ✓ healthy
  Route 2: via (docker) → rebuild → deploy → ✓ healthy
  ─── What doesn't work (learned from experience) ───
  ✗ docker restart (doesn't reload env vars — must recreate)"""

PROCESS_JSON_TEMPLATE = """{
    "tools": ["tool1", "tool2"],
    "steps": ["step1", "step2", "step3"],
    "verification": ["check1", "check2"],
    "anti_patterns": ["what_doesnt_work_1"]
}"""

TITLE_ENHANCEMENT_PROMPT = """Improve this SOP title to be more specific and searchable.

Consider whatever seems relevant to you:

- **What's essential here?** What key technical terms and actions define this SOP?
- **What would make it more searchable?** What keywords should users be able to find it by?
- **Does it follow the format?** Should it maintain the [{sop_type} SOP] tag?
- **How specific is it?** Does it capture the symptom → fix → outcome arc clearly?

SOP QUALITY FORMAT:
- For process SOPs: What WORKS goes at the TOP (routes ranked by preference)
- A divider separates positive from negative: ─── What doesn't work (learned from experience) ───
- What DOESN'T work goes BELOW the divider (only after 3+ occurrences of same pattern)
- Anti-patterns are prefixed with ✗

Current title: {title}

Context: {context}

Share your improved title in whatever way makes sense. You could:
- Provide just the improved title with explanation
- Suggest multiple options
- Explain your reasoning in natural language

Keep improvements to maximum 150 characters and maintain the [{sop_type} SOP] tag."""


# =============================================================================
# LLM-ENHANCED TYPE CLASSIFICATION
# =============================================================================

@dataclass
class TypeClassificationResult:
    """Result of LLM type classification."""
    sop_type: str
    confidence: float
    reasoning: str
    used_llm: bool


def enhance_sop_type_with_llm(
    content: str,
    rule_scores: Optional[Dict[str, int]] = None
) -> TypeClassificationResult:
    """
    Use LLM to classify SOP type when rule-based is ambiguous.

    Args:
        content: The combined task + details text
        rule_scores: Optional dict with 'bugfix' and 'process' scores from rules

    Returns:
        TypeClassificationResult with type, confidence, and reasoning
    """
    # Check if we should use LLM
    use_llm = False
    if rule_scores:
        diff = abs(rule_scores.get('bugfix', 0) - rule_scores.get('process', 0))
        use_llm = diff <= 2  # Scores within 2 points = ambiguous
    else:
        use_llm = True  # No rule scores, use LLM

    if not use_llm:
        # Rule-based is confident enough
        sop_type = 'bugfix' if rule_scores.get('bugfix', 0) >= rule_scores.get('process', 0) else 'process'
        return TypeClassificationResult(
            sop_type=sop_type,
            confidence=0.8,
            reasoning="Rule-based classification (high confidence)",
            used_llm=False
        )

    # Check Ollama availability
    if not is_ollama_available():
        # Fallback to rule-based
        if rule_scores:
            sop_type = 'bugfix' if rule_scores.get('bugfix', 0) >= rule_scores.get('process', 0) else 'process'
        else:
            sop_type = 'bugfix'  # Default to bugfix
        return TypeClassificationResult(
            sop_type=sop_type,
            confidence=0.5,
            reasoning="LLM not available - using rule-based fallback",
            used_llm=False
        )

    # Use LLM for classification
    # Split content into task and details if possible
    parts = content.split('\n', 1)
    task = parts[0]
    details = parts[1] if len(parts) > 1 else ""

    prompt = TYPE_CLASSIFICATION_PROMPT.format(task=task, details=details)

    response = llm_client.generate(prompt, max_tokens=300)

    if not response:
        # LLM failed
        if rule_scores:
            sop_type = 'bugfix' if rule_scores.get('bugfix', 0) >= rule_scores.get('process', 0) else 'process'
        else:
            sop_type = 'bugfix'
        return TypeClassificationResult(
            sop_type=sop_type,
            confidence=0.5,
            reasoning="LLM generation failed - using fallback",
            used_llm=False
        )

    # Parse response
    try:
        result = _extract_json(response)
        if result and 'type' in result:
            # JSON parsing succeeded
            return TypeClassificationResult(
                sop_type=result.get('type', 'bugfix'),
                confidence=float(result.get('confidence', 0.7)),
                reasoning=result.get('reasoning', 'LLM classification'),
                used_llm=True
            )
    except Exception:
        pass  # Fall through to natural language extraction

    # Fallback: extract from natural language response
    extracted_type = _extract_sop_type_from_text(response)
    if extracted_type:
        return TypeClassificationResult(
            sop_type=extracted_type,
            confidence=0.6,
            reasoning=f"Extracted from natural language response: {response[:100]}",
            used_llm=True
        )

    # Ultimate fallback: return rule-based result
    sop_type = 'bugfix' if rule_scores.get('bugfix', 0) >= rule_scores.get('process', 0) else 'process'
    return TypeClassificationResult(
        sop_type=sop_type,
        confidence=0.5,
        reasoning="LLM response could not be parsed - using rule-based fallback",
        used_llm=True
    )


# =============================================================================
# LLM-ENHANCED ZONE EXTRACTION
# =============================================================================

@dataclass
class ZoneExtractionResult:
    """Result of LLM zone extraction."""
    zones: Dict[str, List[str]]
    formatted: str
    used_llm: bool


def enhance_zones_with_llm(
    content: str,
    sop_type: str,
    rule_zones: Optional[str] = None
) -> ZoneExtractionResult:
    """
    Use LLM to extract zones when rule-based yields poor results.

    Args:
        content: The combined task + details text
        sop_type: 'bugfix' or 'process'
        rule_zones: Optional zones already extracted by rules

    Returns:
        ZoneExtractionResult with zones dict and formatted string
    """
    # Check if we need LLM
    need_llm = False
    if rule_zones:
        # Count zones
        zone_count = rule_zones.count('→') + 1
        need_llm = zone_count < 2  # Less than 2 zones = poor extraction
    else:
        need_llm = True

    if not need_llm and rule_zones:
        # Rule-based is good enough
        return ZoneExtractionResult(
            zones={},  # Not parsed
            formatted=rule_zones,
            used_llm=False
        )

    # Check Ollama availability
    if not is_ollama_available():
        return ZoneExtractionResult(
            zones={},
            formatted=rule_zones or "",
            used_llm=False
        )

    # Build prompt based on SOP type
    if sop_type == 'bugfix':
        instructions = BUGFIX_ZONE_INSTRUCTIONS
        json_template = BUGFIX_JSON_TEMPLATE
    else:
        instructions = PROCESS_ZONE_INSTRUCTIONS
        json_template = PROCESS_JSON_TEMPLATE

    prompt = ZONE_EXTRACTION_PROMPT.format(
        sop_type=sop_type,
        zone_instructions=instructions,
        content=content[:1000],  # Limit content length
        json_template=json_template
    )

    response = llm_client.generate(prompt, max_tokens=500)

    if not response:
        return ZoneExtractionResult(
            zones={},
            formatted=rule_zones or "",
            used_llm=False
        )

    # Parse response
    try:
        zones = _extract_json(response)
        if zones:
            formatted = _format_zones(zones, sop_type)
            return ZoneExtractionResult(
                zones=zones,
                formatted=formatted,
                used_llm=True
            )
    except Exception:
        pass  # Fall through to natural language extraction

    # Fallback: extract from natural language response
    extracted_zones = _extract_zones_from_text(response, sop_type)
    if extracted_zones:
        formatted = _format_zones(extracted_zones, sop_type)
        return ZoneExtractionResult(
            zones=extracted_zones,
            formatted=formatted,
            used_llm=True
        )

    # Ultimate fallback: return rule-based result
    return ZoneExtractionResult(
        zones={},
        formatted=rule_zones or "",
        used_llm=True
    )


def _format_zones(zones: Dict[str, List[str]], sop_type: str) -> str:
    """Format extracted zones into arrow-separated string."""
    parts = []

    if sop_type == 'bugfix':
        # Format: bad_sign (antecedent) → fix (stack) → outcome
        bad_sign = zones.get('bad_sign', [])
        antecedent = zones.get('antecedent', [])
        fix = zones.get('fix', [])
        stack = zones.get('stack', [])
        outcome = zones.get('outcome', [])

        # Part 1: bad_sign (antecedent)
        if bad_sign:
            part1 = ' '.join(bad_sign[:2])
            if antecedent:
                part1 += f" ({', '.join(antecedent[:3])})"
            parts.append(part1)

        # Part 2: fix (stack)
        if fix:
            part2 = ' '.join(fix[:2])
            if stack:
                part2 += f" ({', '.join(stack[:3])})"
            parts.append(part2)

        # Part 3: outcome
        if outcome:
            parts.append(outcome[0])

    else:  # process
        # Format: via (tools) → step1 → step2 → ✓ verification
        tools = zones.get('tools', [])
        steps = zones.get('steps', [])
        verification = zones.get('verification', [])
        anti_patterns = zones.get('anti_patterns', [])

        # Part 1: via (tools)
        if tools:
            parts.append(f"via ({', '.join(tools[:3])})")

        # Part 2-N: steps
        for step in steps[:4]:
            parts.append(step)

        # Final: verification
        if verification:
            parts.append(f"✓ {verification[0]}")

    result = ' → '.join(parts) if parts else ""

    # Append anti-patterns if LLM provided them (rare, but supported)
    if anti_patterns:
        result += "\n  ─── What doesn't work (learned from experience) ───"
        for ap in anti_patterns[:5]:
            result += f"\n  ✗ {ap}"

    return result


# =============================================================================
# LLM-ENHANCED TITLE GENERATION
# =============================================================================

def enhance_title_with_llm(
    title: str,
    context: str,
    sop_type: str
) -> str:
    """
    Use LLM to improve a title that scored low on quality.

    Args:
        title: The current title
        context: Additional context (details)
        sop_type: 'bugfix' or 'process'

    Returns:
        Improved title or original if LLM unavailable
    """
    if not is_ollama_available():
        return title

    prompt = TITLE_ENHANCEMENT_PROMPT.format(
        title=title,
        context=context[:500],
        sop_type='bug-fix' if sop_type == 'bugfix' else 'process'
    )

    response = llm_client.generate(prompt, max_tokens=200)

    if response:
        # Try JSON parsing first
        try:
            result = _extract_json(response)
            if result and 'title' in result:
                improved = result.get('title', '').strip()
                if improved and improved.startswith('['):
                    return improved[:180]
        except Exception:
            pass

        # Fallback: extract from natural language
        improved = _extract_title_from_text(response, sop_type)
        if improved:
            # Ensure it has the tag
            if not improved.startswith('['):
                tag = '[bug-fix SOP]' if sop_type == 'bugfix' else '[process SOP]'
                improved = f"{tag} {improved}"
            return improved[:180]

        # Ultimate fallback: clean up raw response
        improved = response.strip()
        if not improved.startswith('['):
            tag = '[bug-fix SOP]' if sop_type == 'bugfix' else '[process SOP]'
            improved = f"{tag} {improved}"
        return improved[:180]

    return title


# =============================================================================
# UTILITY: EXTRACT JSON HELPER
# =============================================================================

def _extract_json(text: str) -> dict:
    """Extract JSON from text, handling markdown wrapping."""
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in markdown
        import re
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


def _extract_sop_type_from_text(text: str) -> Optional[str]:
    """Extract SOP type from natural language response."""
    text_lower = text.lower()
    if re.search(r'\b(bugfix|bug.fix|bug-fix|repair|problem.+solution)\b', text_lower):
        return 'bugfix'
    elif re.search(r'\b(process|procedure|workflow|steps|how.to)\b', text_lower):
        return 'process'
    return None


def _extract_zones_from_text(text: str, sop_type: str) -> Optional[Dict]:
    """Extract zones from natural language response."""
    zones = {}
    if sop_type == 'bugfix':
        # Look for symptom/bad_sign
        bad_sign_match = re.search(r'(?:symptom|bad.sign|problem|error)[:\s]+([^.\n]+)', text, re.IGNORECASE)
        if bad_sign_match:
            zones['bad_sign'] = [bad_sign_match.group(1).strip()]

        # Look for root cause/antecedent
        cause_match = re.search(r'(?:caus|antecedent|reason)[:\s]+([^.\n]+)', text, re.IGNORECASE)
        if cause_match:
            zones['antecedent'] = [cause_match.group(1).strip()]

        # Look for fix
        fix_match = re.search(r'(?:fix|solution|action|treat)[:\s]+([^.\n]+)', text, re.IGNORECASE)
        if fix_match:
            zones['fix'] = [fix_match.group(1).strip()]

    return zones if zones else None


def _extract_title_from_text(text: str, sop_type: str) -> Optional[str]:
    """Extract title from natural language response."""
    # First line containing brackets is likely the title
    lines = text.split('\n')
    for line in lines:
        if '[' in line and 'SOP' in line.upper():
            return line.strip()
    # Otherwise try first line
    if lines and lines[0]:
        return lines[0].strip()
    return None


# =============================================================================
# HYBRID GENERATION (RULES + LLM)
# =============================================================================


def generate_sop_title_llm_hybrid(
    task: str,
    details: str = None,
    force_llm: bool = False
) -> str:
    """
    Generate SOP title using hybrid approach (rules first, LLM fallback).

    This is the main entry point for LLM-enhanced SOP generation.

    Strategy:
    1. Use rules for type classification → LLM if ambiguous
    2. Use rules for zone extraction → LLM if poor results
    3. Combine into title → LLM enhance if quality is low

    Args:
        task: Task description
        details: Additional details
        force_llm: If True, always use LLM (for testing)

    Returns:
        Complete SOP title with tag and zones
    """
    combined = f"{task} {details or ''}"

    # === STEP 1: TYPE CLASSIFICATION ===
    # First, get rule-based scores
    rule_scores = _calculate_type_scores(combined)

    if force_llm or abs(rule_scores['bugfix'] - rule_scores['process']) <= 2:
        # Ambiguous - use LLM
        type_result = enhance_sop_type_with_llm(combined, rule_scores)
        sop_type = type_result.sop_type
    else:
        # Clear - use rules
        sop_type = 'bugfix' if rule_scores['bugfix'] >= rule_scores['process'] else 'process'

    # === STEP 2: ZONE EXTRACTION ===
    # First, try rule-based extraction
    rule_zones = _extract_zones_rules(combined, sop_type)

    if force_llm or not rule_zones or rule_zones.count('→') < 1:
        # Poor extraction - use LLM
        zone_result = enhance_zones_with_llm(combined, sop_type, rule_zones)
        zones = zone_result.formatted
    else:
        zones = rule_zones

    # === STEP 3: COMBINE INTO TITLE ===
    # Generate heart (descriptive core)
    heart = _generate_heart(task, sop_type)

    # Combine heart + zones
    if zones:
        title = f"{heart}: {zones}"
    else:
        title = heart

    # Capitalize
    if title:
        title = title[0].upper() + title[1:]

    # Add tag
    tag = '[bug-fix SOP]' if sop_type == 'bugfix' else '[process SOP]'
    title = f"{tag} {title}"

    # === STEP 4: QUALITY CHECK ===
    quality_score = _score_title_quality(title)

    if force_llm or quality_score < 0.5:
        # Low quality - enhance with LLM
        title = enhance_title_with_llm(title, combined, sop_type)

    return title[:180]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _extract_json(text: str) -> Dict:
    """Extract JSON from LLM response."""
    # Try direct parse
    try:
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] Direct JSON parse failed: {e}")

    # Find JSON in response
    json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception as e:
            print(f"[WARN] Simple JSON extraction failed: {e}")

    # Find JSON with arrays
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception as e:
            print(f"[WARN] Complex JSON extraction failed: {e}")

    return {}


def _calculate_type_scores(content: str) -> Dict[str, int]:
    """Calculate rule-based type scores."""
    content_lower = content.lower()

    bugfix_score = 0
    process_score = 0

    # Strong signals
    strong_bugfix = ['crash', 'error', 'exception', 'broken', 'traceback']
    strong_process = ['deploy', 'install', 'setup', 'configure', 'migrate']

    for word in strong_bugfix:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            bugfix_score += 3

    for word in strong_process:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            process_score += 3

    # Medium signals
    medium_bugfix = ['fix', 'fail', 'bug', 'issue', 'wrong', 'timeout']
    medium_process = ['backup', 'restore', 'create', 'build', 'update']

    for word in medium_bugfix:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            bugfix_score += 2

    for word in medium_process:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            process_score += 2

    return {'bugfix': bugfix_score, 'process': process_score}


def _extract_zones_rules(content: str, sop_type: str) -> str:
    """Simple rule-based zone extraction."""
    content_lower = content.lower()
    parts = []

    if sop_type == 'bugfix':
        # Extract symptoms
        symptoms = []
        for word in ['timeout', 'crash', 'error', 'slow', 'hang', 'fail']:
            if word in content_lower:
                symptoms.append(word)
        if symptoms:
            parts.append(' '.join(symptoms[:2]))

        # Extract causes
        causes = []
        for word in ['blocking', 'sync', 'missing', 'wrong', 'invalid']:
            if word in content_lower:
                causes.append(word)
        if causes:
            parts.append(f"({', '.join(causes[:2])})")

        # Extract tech
        tech = []
        for word in ['docker', 'boto3', 'asyncio', 'postgres', 'redis']:
            if word in content_lower:
                tech.append(word)
        if tech:
            parts.append(' '.join(tech[:2]))

    else:  # process
        # Extract tools
        tools = []
        for word in ['systemctl', 'docker', 'terraform', 'kubectl', 'ssh']:
            if word in content_lower:
                tools.append(word)
        if tools:
            parts.append(f"via ({', '.join(tools[:2])})")

        # Extract steps
        steps = []
        for word in ['backup', 'deploy', 'restart', 'configure', 'verify']:
            if word in content_lower:
                steps.append(word)
        if steps:
            parts.extend(steps[:3])

    return ' → '.join(parts) if parts else ""


def _generate_heart(task: str, sop_type: str) -> str:
    """Generate the descriptive heart of the title."""
    # Remove filler words
    filler = {'the', 'a', 'an', 'is', 'was', 'successfully', 'completed'}
    words = task.split()
    meaningful = [w for w in words if w.lower() not in filler]

    if meaningful:
        return ' '.join(meaningful[:8])
    return task[:60]


def _score_title_quality(title: str) -> float:
    """Score title quality from 0.0 to 1.0."""
    score = 0.5  # Start at middle

    # Reward technical terms
    tech_terms = ['docker', 'asyncio', 'boto3', 'terraform', 'postgres', 'api']
    for term in tech_terms:
        if term in title.lower():
            score += 0.1

    # Reward arrows (structure)
    if '→' in title:
        score += 0.2

    # Reward good length
    if 60 <= len(title) <= 120:
        score += 0.1

    # Penalize vague words
    vague = ['remember', 'approach', 'successfully', 'completed']
    for word in vague:
        if word in title.lower():
            score -= 0.1

    return max(0.0, min(1.0, score))


# =============================================================================
# CLI
# =============================================================================

def main():
    """CLI for testing LLM SOP enhancer."""
    import sys

    if len(sys.argv) < 2:
        print("LLM SOP Enhancer - Ollama-powered SOP title generation")
        print("")
        print("Commands:")
        print("  status                    Check Ollama availability")
        print("  type <content>            Classify SOP type with LLM")
        print("  zones <content> <type>    Extract zones with LLM")
        print("  generate <task> [details] Generate full SOP title (hybrid)")
        print("  test                      Run test cases")
        print("")
        print("Setup:")
        print("  1. ollama serve")
        print("  2. ollama pull llama3.1:8b")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        status = get_ollama_status()
        print("=== Ollama Status ===")
        print(f"Available: {status.get('available', False)}")
        if status.get('endpoint'):
            print(f"Endpoint: {status['endpoint'].get('name', 'unknown')}")
            print(f"Model: {status['endpoint'].get('model', 'unknown')}")
        if status.get('models'):
            print(f"Models: {', '.join(status['models'])}")
        if status.get('reason'):
            print(f"Note: {status['reason']}")

    elif cmd == "type":
        if len(sys.argv) < 3:
            print("Usage: type <content>")
            sys.exit(1)
        content = ' '.join(sys.argv[2:])
        result = enhance_sop_type_with_llm(content)
        print(f"Type: {result.sop_type}")
        print(f"Confidence: {result.confidence:.0%}")
        print(f"Reasoning: {result.reasoning}")
        print(f"Used LLM: {result.used_llm}")

    elif cmd == "zones":
        if len(sys.argv) < 4:
            print("Usage: zones <content> <type>")
            sys.exit(1)
        content = sys.argv[2]
        sop_type = sys.argv[3]
        result = enhance_zones_with_llm(content, sop_type)
        print(f"Zones: {result.zones}")
        print(f"Formatted: {result.formatted}")
        print(f"Used LLM: {result.used_llm}")

    elif cmd == "generate":
        if len(sys.argv) < 3:
            print("Usage: generate <task> [details]")
            sys.exit(1)
        task = sys.argv[2]
        details = sys.argv[3] if len(sys.argv) > 3 else None
        title = generate_sop_title_llm_hybrid(task, details)
        print(title)

    elif cmd == "test":
        print("=== LLM SOP Enhancer Tests ===\n")

        test_cases = [
            ("Fixed asyncio timeout", "Used asyncio.to_thread to wrap blocking boto3 calls"),
            ("Deploy Django to production", "Used systemctl restart gunicorn"),
            ("Container crash on startup", "HOME env var was missing"),
            ("Setup postgres backup", "pg_dump to S3 daily via cron"),
        ]

        for task, details in test_cases:
            print(f"Task: {task}")
            print(f"Details: {details}")

            # Test hybrid generation
            title = generate_sop_title_llm_hybrid(task, details)
            print(f"Title: {title}")
            print()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
