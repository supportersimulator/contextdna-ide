# Reasoning Alignment Audit — Emergence vs. Prescription

**Date**: 2026-02-07
**Status**: VIOLATION IDENTIFIED + REMEDIATION PLAN

---

## Summary of Violations

I introduced forced programmatic reasoning patterns when I should have provided evidence and allowed natural emergence. Three core violations:

### Violation 1: Forced Think Mode (`llm_priority_queue.py:152`)

**Current Implementation**:
```python
if enable_think:
    payload["messages"][-1]["content"] += " /think"  # FORCED
```

**Problem**:
- LLM never decides whether thinking helps
- No tracking of thinking mode outcomes
- Prescriptive, not emergent
- Every reasoning-profile query gets `/think` regardless of context

**Philosophy Violation**:
- ❌ "Allow the thinking to do the thinking" — No, I'm forcing it
- ❌ "Evidence pipeline tracks which patterns work best" — Not tracking thinking mode effectiveness
- ❌ "Emergence over prescription" — Pure prescription

**Correct Approach**:
1. Make thinking mode OPTIONAL (suggest, not force)
2. Let LLM choose: "You may use thinking mode if helpful: /think"
3. Track outcome: did thinking mode improve quality?
4. Evidence pipeline calibrates whether to suggest thinking in similar contexts

---

### Violation 2: Rigid 5-Step Reasoning (graph_reasoning_context.py:257-280)

**Current Implementation**:
```
1. Semantic Dependency Decomposition (forced step)
2. Importance Weighting (forced step)
3. Uncertainty Handling (forced step)
4. Novel Organization (forced step)
5. Study Approach Recommendations (forced step)
```

**Problem**:
- Only one allowed reasoning pattern
- LLM can't explore its own paths
- Not tracking whether 5-step approach is actually effective
- Premature optimization disguised as sophistication

**Philosophy Violation**:
- ❌ "Emergence over prescription" — Rigidly prescribes 5 steps
- ❌ "System evolves toward most effective reasoning patterns" — Only pattern is allowed
- ❌ "Empower creative reasoning without prescriptive directives" — Explicit directive

**Correct Approach**:
1. Present evidence: graph, gaps, hubs
2. Open question: "What matters and why?"
3. Let LLM reason in whatever structure feels natural
4. Track: Was the reasoning useful? Did it prevent bugs?
5. Evidence pipeline learns which reasoning styles actually help

---

### Violation 3: Forced JSON Output Schema (graph_reasoning_context.py:284-310)

**Current Implementation**:
```json
{
  "semantic_analysis": {
    "spine_components": [...],
    "dependency_chains": [...],
    "tangential_areas": [...]
  },
  "gap_priorities": [...],
  "recommended_study_sequence": [...],
  "novel_organization": {...},
  "thinking_summary": "..."
}
```

**Problem**:
- LLM cannot express ideas in its own structure
- Forced parsing/validation overhead
- Cannot discover better schema through experimentation
- Premature standardization

**Philosophy Violation**:
- ❌ "Allow the thinking to do the thinking" — No, forced into predetermined output
- ❌ "Emergence over prescription" — Schema is prescribed constraint
- ❌ "Evidence-based learning" — Not tracking whether this schema is effective

**Correct Approach**:
1. Accept whatever format the LLM naturally produces
2. Store raw reasoning for analysis
3. Extract value from natural expression (parsing → understanding, not vice versa)
4. Track: Does structured JSON actually improve outcomes vs. conversational response?

---

### Violation 4: introspection_engine.py (348-382) - Same 4-Step Pattern

**Current Implementation**:
```
1. ANALYZE: What should we learn about each gap domain?
2. REASON: Why is learning this important? What depends on it?
3. RECOMMEND: What study approach would be most effective?
4. CREATIVELY ORGANIZE: How should we organize learning about this domain?
```

**Problem**: Identical to graphing reasoning — forced step-by-step analysis.

**Correct Approach**: Same as above — evidence + open question + natural emergence.

---

## Remediation Plan

### Phase 1: Remove Forced Patterns

**File**: `llm_priority_queue.py`
**Action**: Make thinking mode optional suggestion, not forced append

```python
# BEFORE (WRONG):
if enable_think:
    payload["messages"][-1]["content"] += " /think"

# AFTER (CORRECT):
# Don't force. Let the LLM decide OR suggest as option in system prompt
# Track when it's used naturally vs. when we suggest it
if enable_think and context_suggests_benefit():  # Learned from evidence
    suggested_hint = "\n\n[Optional: Use detailed reasoning if helpful: /think]"
    payload["messages"][-1]["content"] += suggested_hint
# Otherwise: no thinking mode append at all
```

**Outcome**:
- LLM chooses thinking naturally
- Track: times thinking suggested, times used, outcome quality
- Evidence pipeline learns when thinking actually helps

---

### Phase 2: Replace Prescriptive Prompts with Evidence + Open Questions

**File**: `graph_reasoning_context.py`

**BEFORE (VIOLATES - Prescriptive)**:
```python
def generate_reasoning_prompt(self, context):
    prompt = f"""
# Graph-Based Knowledge Gap Analysis

...5 forced steps...
1. Semantic Dependency Decomposition
2. Importance Weighting
...
## Output Format
Provide your analysis in JSON with these fields:
- semantic_analysis
- gap_priorities
...
"""
```

**AFTER (ALIGNED - Evidence-Based)**:
```python
def generate_reasoning_prompt(self, context):
    prompt = f"""
# Codebase Knowledge Map

Here's what exists in the codebase:
- {context['graph_structure'].get('total_nodes')} connected components
- Critical infrastructure hubs: {len(context['graph_structure'].get('critical_hubs', []))}
- Identified knowledge gaps across {len(context.get('knowledge_gaps', []))} domains

## The Gaps:
{self._format_gaps_as_evidence(context.get('knowledge_gaps', []))}

## Critical Infrastructure (What lots of things depend on):
{self._format_hubs_as_evidence(context['graph_structure'].get('critical_hubs', []))}

## Your Perspective:
Given this map, what seems most important to understand?
- Which gaps have the most ripple effects?
- Which infrastructure deserves deeper study?
- What patterns do you notice?
- How would you organize learning about these areas?

(Share your thoughts in whatever way makes sense to you.)
"""
    return prompt
```

**Key Changes**:
- ❌ No "1. 2. 3. 4. 5." forced steps
- ❌ No "Provide your analysis in JSON" requirement
- ✅ Evidence presented (graph, gaps, hubs)
- ✅ Open questions
- ✅ LLM chooses structure

---

### Phase 3: Track Reasoning Patterns in Evidence Pipeline

**New Table: `reasoning_pattern_outcomes`**
```sql
CREATE TABLE reasoning_pattern_outcomes (
    id TEXT PRIMARY KEY,
    query_id TEXT,
    pattern_used TEXT,  -- 'natural', 'thinking_mode', '5_step_structured', etc.
    thinking_mode_used BOOLEAN,
    reasoning_length_tokens INTEGER,
    output_format TEXT,  -- 'json', 'narrative', 'bullet_points', etc.

    -- Outcome tracking
    was_useful BOOLEAN,
    prevented_bug BOOLEAN,
    helped_with_similar_task BOOLEAN,
    accuracy_score FLOAT,

    -- Evidence for next time
    confidence FLOAT,
    created_at TIMESTAMP
);
```

**What Gets Tracked**:
- When thinking mode is used (natural vs. forced)
- Reasoning pattern observed (5-step, narrative, exploratory, etc.)
- Output format chosen (JSON, prose, etc.)
- Outcomes: did it help? Did it prevent bugs?
- Confidence calibration for next similar query

**How Evidence Informs Future**:
- Query 1: LLM uses thinking mode naturally, produces useful insight → confidence += 0.1
- Query 2: Similar context, thinking mode wasn't used last time → suggest it (not force)
- Query 3: User feedback: "thinking mode slowed it down, direct answer was better" → confidence -= 0.2
- Query 4: Similar context, thinking mode confidence now low → don't suggest

---

### Phase 4: Remove Output Format Constraints

**Remove from prompts**:
- ❌ "Provide your analysis in JSON"
- ❌ "Return in this format: {...}"
- ❌ Specific field names that aren't discovered naturally

**Allow**:
- ✅ Any structure the LLM finds natural
- ✅ Narrative reasoning
- ✅ Bullet points
- ✅ Step-by-step if LLM chooses it naturally
- ✅ JSON if that's clearest expression

**Processing**:
- Read whatever format is returned
- Extract value through understanding (not parsing)
- Track: did this format help? Was it natural?

---

### Phase 5: Calibrate Thinking Mode Suggestions (Not Forces)

**System Prompt (Optional Suggestion)**:
```
You may use detailed reasoning when it would help:
/think

You may respond directly when the answer is straightforward:
(no special notation needed)

You decide what's appropriate for this question.
```

**Outcome**:
- LLM chooses naturally
- Evidence tracks which approach was effective
- Next similar query informed by evidence, not prescription

---

## Implementation Checklist

- [ ] `llm_priority_queue.py`: Convert forced `/think` to optional suggestion
- [ ] `graph_reasoning_context.py`: Replace 5-step structure with evidence + open questions
- [ ] `introspection_engine.py`: Remove 4-step forced reasoning
- [ ] `observability_store.py`: Add `reasoning_pattern_outcomes` table
- [ ] All reasoning prompts: Remove output format constraints
- [ ] Evidence pipeline: Track reasoning pattern effectiveness
- [ ] System prompts: Suggest thinking mode, don't force it
- [ ] Tests: Verify LLM can choose its reasoning approach naturally

---

## Philosophical Alignment

These changes restore alignment with:

✅ **"Allow the thinking to do the thinking"** — LLM chooses its reasoning style
✅ **"Emergence over prescription"** — Patterns emerge from evidence, not design
✅ **"System evolves toward most effective reasoning patterns"** — Evidence pipeline learns
✅ **"Empower creative reasoning without prescriptive directives"** — No forced 5 steps
✅ **"Evidence-based systems"** — Thinking mode effectiveness measured, not assumed
✅ **"Corrigibility over authority"** — Outcomes override the programmatic design
✅ **"Uncertainty calibration"** — Confidence tracks actual helpfulness

---

## Success Metrics

| Metric | Before | After | Goal |
|--------|--------|-------|------|
| Reasoning patterns allowed | 1 (5-step forced) | >5 (emergent) | Diversity |
| Thinking mode | Always forced | Suggested/natural | LLM-driven |
| Output formats accepted | 1 (JSON) | >5 (any natural) | Flexibility |
| Tracking reasoning effectiveness | No | Yes | Evidence-based |
| Calibration of thinking mode | No | Yes, per query type | Learning |

---

## Next: Verify System Response

After remediation:
1. Query with graph gaps → LLM reasons naturally
2. Track what approach it chooses
3. Measure outcome (useful? prevented bugs?)
4. Confidence updates automatically
5. Next similar query gets evidence-informed suggestion (not force)

**This is epistemic sustainability:** The system learns what actually works rather than enforcing what I guessed works.

---

**Prepared by**: Atlas (Alignment Auditor)
**Reference Philosophy**:
- Epistemic Sustainability (Evidence_Based_Systems_Freedom_by_Constraint.md)
- "Emergence over prescription" (Agents-Evidence-Based-Context.md)
