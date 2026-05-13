# REMEDIATION IMPLEMENTATION — Emergence-Aligned Reasoning

## File 1: llm_priority_queue.py — Remove Forced Thinking Mode

### Change 1: Make thinking mode optional suggestion, not forced append

**Location**: Lines 128-152

**BEFORE** (VIOLATES):
```python
# For reasoning profile, enable Qwen3 thinking mode
enable_think = req.enable_thinking or req.profile == "reasoning"

try:
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": req.system_prompt},
            {"role": "user", "content": req.user_prompt}
        ],
        ...
    }

    # Qwen3 thinking mode: append /think to user prompt to enable
    if enable_think:
        payload["messages"][-1]["content"] += " /think"  # ❌ FORCED
```

**AFTER** (ALIGNED):
```python
# Thinking mode is optional — let LLM decide naturally
# Don't append /think. Instead, mention in system prompt that it's available.
enable_think = req.enable_thinking  # Only if explicitly requested, not from profile

try:
    # If system prompt should mention thinking mode availability:
    system_with_thinking_option = req.system_prompt
    if enable_think:
        system_with_thinking_option += """

---
Note: You may use detailed reasoning when helpful by starting with /think.
You may respond directly when the answer is straightforward.
Choose what's appropriate for this query.
"""

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_with_thinking_option},
            {"role": "user", "content": req.user_prompt}
        ],
        ...
    }

    # DON'T force /think append. Let LLM choose naturally.
    # if enable_think:
    #     payload["messages"][-1]["content"] += " /think"  # ❌ REMOVED
```

**Why This Works**:
- ✅ LLM can choose thinking naturally
- ✅ System suggests option, doesn't force
- ✅ Outcomes will show if thinking actually helps
- ✅ Evidence pipeline can calibrate when to suggest

---

## File 2: graph_reasoning_context.py — Replace Prescriptive Structure

### Change: Replace 5-step forced reasoning with evidence + open questions

**Location**: Lines 229-312 (`generate_reasoning_prompt` method)

**BEFORE** (VIOLATES):
```python
def generate_reasoning_prompt(self, context: Dict[str, Any]) -> str:
    """Generate prompt for LLM thinking mode analysis.

    Empowers creative reasoning without prescriptive directives.  ← IRONIC: actually prescribes 5 steps
    """
    prompt = f"""
# Graph-Based Knowledge Gap Analysis

You are presented with a software codebase represented as a dependency graph.
Your task is to reason creatively about knowledge gaps and learning priorities.

## Graph Structure
- Total Nodes: {context['graph_structure'].get('total_nodes', 'unknown')}
...

## Your Task (Creative Reasoning with Thinking Mode)

Using your reasoning capabilities, analyze:

1. **Semantic Dependency Decomposition**  ← ❌ FORCED STEP 1
   - How do these domains relate semantically?
   ...

2. **Importance Weighting (Creative)**     ← ❌ FORCED STEP 2
   - Which gaps, if filled, would have the most systemic impact?
   ...

3. **Uncertainty Handling**                 ← ❌ FORCED STEP 3
   ...

4. **Novel Organization**                  ← ❌ FORCED STEP 4
   ...

5. **Study Approach Recommendations**      ← ❌ FORCED STEP 5
   ...

## Output Format                            ← ❌ LOCKED TO JSON

Provide your analysis in JSON:
{
  "semantic_analysis": {...},
  "gap_priorities": [...],
  "recommended_study_sequence": [...],
  "novel_organization": {...},
  "thinking_summary": "..."
}
"""
    return prompt
```

**AFTER** (ALIGNED):
```python
def generate_reasoning_prompt(self, context: Dict[str, Any]) -> str:
    """Generate reasoning context for LLM.

    Presents evidence. Asks open question. Lets LLM reason naturally.
    """
    prompt = f"""
# Codebase Knowledge Map

Here's what exists in the system:

## Graph Structure
- **Total Components**: {context['graph_structure'].get('total_nodes', 'unknown')} nodes
- **Total Connections**: {context['graph_structure'].get('total_edges', 'unknown')} edges

## Critical Infrastructure (high connectivity)
{self._format_hubs_for_evidence(context['graph_structure'].get('critical_hubs', []))}

## Identified Knowledge Gaps
{self._format_gaps_for_evidence(context.get('knowledge_gaps', []))}

---

## What we're trying to understand:
Given this map of the system, what stands out to you?
- Which gaps seem most important?
- Which infrastructure areas deserve deeper study?
- What patterns or clusters do you notice?
- If you were learning this system, what sequence would make sense?

(Share your thinking in whatever way is most natural. No specific format required.)
"""
    return prompt

def _format_hubs_for_evidence(self, hubs: List[Dict[str, Any]]) -> str:
    """Format hub nodes as evidence, not as directive."""
    if not hubs:
        return "No critical infrastructure hubs identified."

    lines = ["These components have high connectivity (many things depend on them):"]
    for hub in hubs[:10]:
        lines.append(
            f"  • **{hub['file_path']}** — {hub['outgoing_edges']} outgoing connections "
            f"({hub['criticality']})"
        )
    return "\n".join(lines)

def _format_gaps_for_evidence(self, gaps: List[Dict[str, Any]]) -> str:
    """Format gaps as evidence, not as task list."""
    if not gaps:
        return "No significant knowledge gaps identified."

    lines = ["Domains with limited understanding:"]
    for gap in gaps[:10]:
        lines.append(
            f"  • **{gap['domain']}** — {gap['gap_score']:.0%} gap "
            f"({gap['files']} files, {gap['learnings']} learnings, {gap['priority']})"
        )
    return "\n".join(lines)
```

**Why This Works**:
- ✅ No forced 5-step analysis
- ✅ No JSON requirement
- ✅ Evidence presented clearly
- ✅ Open question lets LLM explore naturally
- ✅ LLM can discover its own reasoning structure
- ✅ Outcomes will show what approach was effective

---

## File 3: introspection_engine.py — Same Pattern Removal

### Change: Replace 4-step forced reasoning

**Location**: Lines 348-383

**BEFORE** (VIOLATES):
```python
reasoning_prompt = f"""
You are the infrastructure butler's introspection engine. The system has identified knowledge gaps.

IDENTIFIED GAPS (by severity):
{json.dumps(gaps[:3], indent=2)}

AVAILABLE CONTEXT:
{json.dumps(context, indent=2)}

Your task: Use your reasoning capabilities to analyze these gaps deeply.

1. ANALYZE: What should we learn about each gap domain?    ← ❌ FORCED STEP 1
2. REASON: Why is learning this important? ...             ← ❌ FORCED STEP 2
3. RECOMMEND: What study approach would be most ...        ← ❌ FORCED STEP 3
4. CREATIVELY ORGANIZE: How should we organize ...         ← ❌ FORCED STEP 4

Use your thinking mode to explore:
- Dependencies and ripple effects
...

Return JSON:  ← ❌ LOCKED FORMAT
{
  "gap_analyses": [...],
  "reasoning_summary": "..."
}
"""
```

**AFTER** (ALIGNED):
```python
reasoning_prompt = f"""
# Knowledge Gaps in the Codebase

The system identified these domains needing deeper study:

{json.dumps(gaps[:3], indent=2)}

## Current Context
{json.dumps(context, indent=2)}

---

## What we're wondering:
Looking at these gaps, what seems important?
- Which would have the most impact if understood?
- Which are foundational to understanding others?
- How would you organize learning about these areas?
- What dependencies or patterns do you notice?

(Share your perspective in whatever way makes sense.)
"""
```

**Why This Works**:
- ✅ Evidence presented (gaps + context)
- ✅ Open wondering, not directive
- ✅ LLM chooses reasoning approach
- ✅ No forced step structure
- ✅ No JSON schema lock

---

## Supporting Change: Track Reasoning Pattern Outcomes

### New Table: reasoning_pattern_outcomes

Add to `observability_store.py`:

```python
def create_reasoning_pattern_table(self):
    """Track reasoning patterns and their effectiveness."""
    with self.get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reasoning_pattern_outcomes (
                id TEXT PRIMARY KEY,
                query_id TEXT,

                -- What pattern was used
                pattern_type TEXT,  -- 'narrative', '5_step', 'json_structured', 'exploratory', 'direct', etc.
                thinking_mode_used BOOLEAN,
                reasoning_length_tokens INTEGER,
                output_format TEXT,

                -- Outcome tracking
                was_useful BOOLEAN,
                prevented_bug BOOLEAN,
                helped_similar_task BOOLEAN,
                user_feedback TEXT,

                -- Confidence for future similar queries
                accuracy_score FLOAT,  -- 0.0-1.0
                confidence FLOAT,      -- 0.0-1.0

                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.commit()

def record_reasoning_outcome(self, query_id: str, pattern_type: str,
                            thinking_used: bool, useful: bool,
                            prevented_bug: bool, accuracy: float) -> str:
    """Record how a reasoning approach worked out."""
    outcome_id = f"reasoning_{uuid.uuid4().hex[:8]}"

    with self.get_connection() as conn:
        conn.execute("""
            INSERT INTO reasoning_pattern_outcomes
            (id, query_id, pattern_type, thinking_mode_used, was_useful,
             prevented_bug, accuracy_score, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            outcome_id, query_id, pattern_type, thinking_used, useful,
            prevented_bug, accuracy, 0.7, datetime.now().isoformat()
        ))
        conn.commit()

    return outcome_id
```

---

## Checking for Other Violations

Also audit these for forced patterns:

### Check: butler_deep_query.py

```bash
grep -n "Your task\|Steps\|Provide.*format\|Return JSON" memory/butler_deep_query.py
```

If found, same remediation applies.

### Check: Section 6 injection (butler_context_summary.py)

```bash
grep -n "reasoning\|thinking.*mode" memory/butler_context_summary.py
```

If injecting thinking mode requirement, remove it.

---

## Implementation Order

1. ✅ **Audit** — Identify all forced patterns (done: REASONING_ALIGNMENT_AUDIT.md)
2. 🔄 **Review** — User approves remediation direction (YOU HERE)
3. ⏳ **Implement** — Apply code changes above if approved
4. ⏳ **Test** — Verify LLM can reason naturally without forced structure
5. ⏳ **Monitor** — Track reasoning pattern outcomes in evidence pipeline
6. ⏳ **Calibrate** — Use evidence to decide when to suggest thinking mode

---

## Success Signals After Remediation

✅ LLM produces reasoning in natural structure (not forced 5-step)
✅ LLM chooses thinking mode when appropriate (not appended to every query)
✅ Output varies by query (narrative, JSON, bullet points, exploratory)
✅ Evidence pipeline tracks which patterns work best
✅ System learns vs. prescribes

---

## Ready for Approval

This audit identifies the violations + provides concrete remediation code.

**Decision needed**:
- Approve remediation direction?
- Which files to update first?
- Should I implement changes now?

