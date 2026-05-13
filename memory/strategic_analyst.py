"""
S10 Strategic Analyst — GPT-4.1 as Persistent Strategic Mind.

Pre-computes Section 10 (Strategic Big Picture) every 15 minutes using GPT-4.1.
Gathers context from 7 sources: session historian, markdown memory layer,
evidence store, git log, dialogue mirror, focus state, + strategic plans.
Caches result in Redis for instant webhook delivery. Zero blocking at injection time.

Every 4th cycle (~1 hour), GPT-4.1 rewrites TWO project-scoped strategic plans files:
- memory/strategic_plans.md — ContextDNA infrastructure (webhook, scheduler, LLM, etc.)
- memory/strategic_plans_ersim.md — ER Simulator (Apps Script, voice stack, web app, etc.)

Uses ===SPLIT=== delimiter to separate project outputs in a single GPT-4.1 call.
All 3 surgeons can read/write the plans:
- Atlas (Claude): direct file read/edit
- GPT-4.1: via this module's run_strategic_analysis_cycle()
- Qwen3-4B: via read_strategic_plans(project=) / update_plan_entry(project=)

Surgery Team of 3 architecture: GPT-4.1 = dedicated strategic analyst role.
Runs independently of local LLM (no GPU lock contention).
"""

import json
import logging
import os
import subprocess
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Budget gate: S10 sub-budget (within overall $5/day)
S10_DAILY_BUDGET_USD = float(os.environ.get("S10_DAILY_BUDGET_USD", "2.0"))

# Redis key for S10 cost tracking
S10_COST_KEY_PREFIX = "llm:costs"

# Persistent strategic plans files — project-scoped, shared across all 3 surgeons
PLANS_FILE = os.path.join(os.path.dirname(__file__), "strategic_plans.md")  # ContextDNA/Infrastructure
PLANS_FILE_ERSIM = os.path.join(os.path.dirname(__file__), "strategic_plans_ersim.md")  # ER Simulator

# Project → file mapping
PLANS_FILES = {
    "contextdna": PLANS_FILE,
    "ersim": PLANS_FILE_ERSIM,
}

# Plan update frequency: every N cycles (4 × 15min = 1 hour)
PLAN_UPDATE_EVERY_N_CYCLES = 4

# Context character limits per source (total ~5K chars → ~1.2K tokens input)
LIMITS = {
    "session_historian": 800,
    "markdown_memory": 800,
    "evidence": 500,
    "git_log": 400,
    "dialogue": 500,
    "focus": 200,
    "strategic_plans": 1000,
}

SYSTEM_PROMPT = """You are the Strategic Analyst for the Atlas ecosystem. Your role: synthesize cross-session patterns, spot forgotten infrastructure, identify abandoned code, and surface blind spots that Atlas (Claude) loses each session due to context limits.

RULES:
- EXTREME CONCISION. Every word earns its place.
- Focus on ACTIONABLE strategic threads, not summaries.
- Flag BLIND SPOTS: things the team planned but forgot, code written but never wired, infrastructure started but never finished.
- Flag CONNECTIONS: recurring themes across sessions that suggest a deeper pattern.
- Suggest NEXT priorities based on trajectory and momentum.

OUTPUT FORMAT (strict):
THREADS: [2-3 active strategic threads with status — what's moving forward]
BLIND SPOTS: [forgotten infra, abandoned code, unfinished plans — be specific with file paths if known]
CONNECTIONS: [cross-session patterns, recurring themes worth noting]
NEXT: [1-2 suggested priorities based on current trajectory]"""

PLANS_UPDATE_PROMPT = """You maintain TWO project-scoped Strategic Plans files for the Surgery Team of 3 (Atlas/Claude, GPT-4.1, Qwen3-4B).

PROJECT SEPARATION (STRICT):
- **ContextDNA** = memory infrastructure, webhook pipeline, scheduler, LLM team, IDE panels, Redis cache, session historian, brain, professor, evidence, anticipation engine. Key dirs: memory/, context-dna/, scripts/, admin.contextdna.io/, mcp-servers/
- **ER Simulator** = Apps Script monolith, scenario pipeline, voice AI stack (LiveKit/Kyutai), web-app (Next.js/Django/Supabase), backend, categories/pathways, ATSR, batch tools. Key dirs: simulator-core/, ersim-voice-stack/, web-app/, backend/, google-drive-code/

NEVER mix ER Simulator concerns into ContextDNA plans or vice versa.

OUTPUT FORMAT (strict):
Produce TWO documents separated by the line: ===SPLIT===
First document: ContextDNA plans starting with '# Strategic Plans — ContextDNA Infrastructure'
Second document: ER Simulator plans starting with '# Strategic Plans — ER Simulator'

RULES (both documents):
- REWRITE the entire file, don't just append
- MERGE new analysis findings into existing structure
- PROMOTE recurring themes to Active Threads (with status: ACTIVE|STALLED|BLOCKED|NEAR-COMPLETE)
- ARCHIVE completed items (move to Archived section with [DONE yyyy-mm-dd])
- Mark threads STALLED if no git/session evidence of progress in 3+ days
- EXTREME CONCISION. Every line earns its place
- Keep each section to 5-8 items max (archive excess)
- Include REAL file paths, specific function names, concrete details — not vague descriptions
- Only reference files that ACTUALLY EXIST. Do not invent paths.
- Preserve the header block (lines 1-5) exactly as given for each document"""


def _check_s10_budget() -> bool:
    """Check if S10 sub-budget is exhausted for today."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        today = date.today().isoformat()
        cost_str = r.get(f"{S10_COST_KEY_PREFIX}:{today}:s10")
        if cost_str:
            current = float(cost_str)
            if current >= S10_DAILY_BUDGET_USD:
                logger.info(f"S10 budget exhausted: ${current:.4f} >= ${S10_DAILY_BUDGET_USD}")
                return False
        return True
    except Exception as e:
        logger.debug(f"S10 budget check failed (allowing): {e}")
        return True  # Allow on Redis failure


def _track_s10_cost(cost_usd: float, model: str):
    """Track S10-specific cost in Redis."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        today = date.today().isoformat()

        # S10-specific daily cost
        r.incrbyfloat(f"{S10_COST_KEY_PREFIX}:{today}:s10", cost_usd)
        r.expire(f"{S10_COST_KEY_PREFIX}:{today}:s10", 86400 * 2)

        # Also increment overall daily total
        r.incrbyfloat(f"{S10_COST_KEY_PREFIX}:{today}", cost_usd)
        r.expire(f"{S10_COST_KEY_PREFIX}:{today}", 86400 * 2)

        # Per-provider breakdown
        r.incrbyfloat(f"{S10_COST_KEY_PREFIX}:{today}:openai", cost_usd)
        r.expire(f"{S10_COST_KEY_PREFIX}:{today}:openai", 86400 * 2)

        logger.debug(f"S10 cost tracked: ${cost_usd:.6f} ({model})")
    except Exception as e:
        logger.debug(f"S10 cost tracking failed: {e}")


def _gather_session_historian() -> str:
    """Get recent insights and cross-session patterns from session historian."""
    try:
        from memory.session_historian import SessionHistorian
        historian = SessionHistorian()
        insights = historian.get_recent_insights(limit=10)
        if not insights:
            return "[No recent session insights]"

        parts = []
        for i in insights:
            parts.append(f"[{i.get('date', '?')}] {i.get('type', '?')}: {i.get('content', '')[:100]}")
        result = "\n".join(parts)
        return result[:LIMITS["session_historian"]]
    except Exception as e:
        logger.debug(f"Session historian gather failed: {e}")
        return "[Session historian unavailable]"


def _gather_markdown_memory() -> str:
    """Get document summaries from Markdown Memory Layer."""
    try:
        from memory.markdown_memory_layer import query_markdown_layer
        results = query_markdown_layer(
            "plans infrastructure architecture roadmap unfinished",
            top_k=5,
            focus_filter=False,  # Get across all projects
        )
        if not results:
            return "[No markdown memory results]"

        parts = []
        for r in results:
            path = r.get("path", r.get("file", "?"))
            summary = r.get("summary", r.get("content", ""))[:120]
            parts.append(f"[{path}] {summary}")
        result = "\n".join(parts)
        return result[:LIMITS["markdown_memory"]]
    except Exception as e:
        logger.debug(f"Markdown memory gather failed: {e}")
        return "[Markdown memory layer unavailable]"


def _gather_evidence() -> str:
    """Get top evidence items (correlation+ grade)."""
    try:
        from memory.query import query_learnings
        learnings = query_learnings("architecture infrastructure plans", limit=5)
        if not learnings:
            return "[No evidence items]"

        result = "\n".join(str(l)[:100] for l in learnings)
        return result[:LIMITS["evidence"]]
    except Exception as e:
        logger.debug(f"Evidence gather failed: {e}")
        return "[Evidence store unavailable]"


def _gather_git_log() -> str:
    """Get recent git commits for development trajectory."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(__file__))
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()[:LIMITS["git_log"]]
        return "[Git log unavailable]"
    except Exception as e:
        logger.debug(f"Git log gather failed: {e}")
        return "[Git log unavailable]"


def _gather_dialogue() -> str:
    """Get recent Aaron messages from dialogue mirror."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        data = r.get("dialogue:recent")
        if data:
            return data[:LIMITS["dialogue"]]

        # Fallback: check dialogue mirror state
        mirror_data = r.get("dialogue:mirror_state")
        if mirror_data:
            parsed = json.loads(mirror_data)
            messages = parsed.get("recent_messages", [])
            if messages:
                parts = [f"[{m.get('role', '?')}] {m.get('content', '')[:80]}" for m in messages[-5:]]
                return "\n".join(parts)[:LIMITS["dialogue"]]
        return "[No recent dialogue]"
    except Exception as e:
        logger.debug(f"Dialogue gather failed: {e}")
        return "[Dialogue mirror unavailable]"


def _gather_focus_state() -> str:
    """Get current focus mode state."""
    try:
        focus_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".claude", "focus.json")
        if os.path.exists(focus_path):
            with open(focus_path) as f:
                data = json.load(f)
            active = data.get("active", [])
            return f"Focus: {', '.join(active) if active else 'none'}"[:LIMITS["focus"]]
        return "Focus: not configured"
    except Exception as e:
        logger.debug(f"Focus state gather failed: {e}")
        return "Focus: unknown"


def _gather_strategic_plans() -> str:
    """Get current strategic plans (7th context source — continuity across cycles). Reads both project files."""
    try:
        parts = []
        per_project_limit = LIMITS["strategic_plans"] // max(len(PLANS_FILES), 1)
        for project in PLANS_FILES:
            content = read_strategic_plans(project=project)
            if content and content.strip():
                parts.append(f"[{project}]\n{content[:per_project_limit]}")
        if not parts:
            return "[No strategic plans yet]"
        return "\n\n".join(parts)[:LIMITS["strategic_plans"]]
    except Exception as e:
        logger.debug(f"Strategic plans gather failed: {e}")
        return "[Strategic plans unavailable]"


def _gather_all_context() -> str:
    """Gather context from all 7 sources into a single prompt."""
    sections = []

    sections.append("=== SESSION HISTORIAN (recent insights) ===")
    sections.append(_gather_session_historian())

    sections.append("\n=== DOCUMENT MEMORY (key docs) ===")
    sections.append(_gather_markdown_memory())

    sections.append("\n=== EVIDENCE STORE (proven patterns) ===")
    sections.append(_gather_evidence())

    sections.append("\n=== GIT TRAJECTORY (recent commits) ===")
    sections.append(_gather_git_log())

    sections.append("\n=== DIALOGUE (recent Aaron messages) ===")
    sections.append(_gather_dialogue())

    sections.append("\n=== FOCUS STATE ===")
    sections.append(_gather_focus_state())

    sections.append("\n=== STRATEGIC PLANS (persistent big picture) ===")
    sections.append(_gather_strategic_plans())

    return "\n".join(sections)


def _call_gpt4(context: str) -> Optional[dict]:
    """Call GPT-4.1 via LLMGateway. Returns {content, cost_usd, model, latency_ms} or None."""
    try:
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        # Load context-dna/.env for API keys
        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        from local_llm.llm_gateway import LLMGateway, GatewayConfig

        # Custom config: use gpt-4.1 (not mini) for strategic quality
        config = GatewayConfig(
            default_models={"openai": "gpt-4.1", "local": "qwen3-4b"},
        )
        gateway = LLMGateway(config)
        response = gateway.infer(
            prompt=f"Analyze the following ecosystem state and provide strategic analysis:\n\n{context}",
            system=SYSTEM_PROMPT,
            provider="openai",
            max_tokens=3072,
            temperature=0.4,
            session_id="strategic_analyst_s10",
        )

        if response.error:
            logger.warning(f"S10 GPT-4.1 error: {response.error}")
            return None

        if not response.content:
            logger.warning("S10 GPT-4.1: empty response")
            return None

        return {
            "content": response.content,
            "cost_usd": response.cost_usd or 0.0,
            "model": response.model_id or "gpt-4.1",
            "latency_ms": response.latency_ms or 0,
        }
    except Exception as e:
        logger.error(f"S10 GPT-4.1 call failed: {type(e).__name__}: {str(e)[:200]}")
        return None


def _format_s10_content(raw_content: str) -> str:
    """Format raw GPT-4.1 output into Section 10 webhook format."""
    return (
        "=== S10: STRATEGIC ANALYST (GPT-4.1) ===\n"
        f"{raw_content.strip()}\n"
        f"[Generated: {datetime.now(timezone.utc).strftime('%H:%M UTC')} | Model: gpt-4.1]"
    )


def _cache_s10(content: str, session_id: Optional[str] = None) -> bool:
    """Cache S10 content in Redis for webhook retrieval."""
    try:
        from memory.redis_cache import cache_strategic_section
        sid = session_id or _detect_session()
        if not sid:
            sid = "default"
        return cache_strategic_section(sid, content, ttl=1800)
    except Exception as e:
        logger.error(f"S10 cache failed: {e}")
        return False


def _detect_session() -> Optional[str]:
    """Detect active IDE session (same pattern as anticipation engine)."""
    try:
        from memory.anticipation_engine import _detect_active_session
        return _detect_active_session()
    except Exception:
        return None


# ─── PERSISTENT STRATEGIC PLANS ────────────────────────────────

def _get_cycle_count() -> int:
    """Get current cycle count from Redis. Increments each call."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
        count = r.incr("strategic_analyst:cycle_count")
        # Reset daily to prevent unbounded growth
        r.expire("strategic_analyst:cycle_count", 86400)
        return count
    except Exception:
        return 1


def _is_plan_update_cycle(cycle_count: int) -> bool:
    """Check if this cycle should update the plans file (every Nth cycle)."""
    return cycle_count % PLAN_UPDATE_EVERY_N_CYCLES == 0


def _strip_code_fences(content: str) -> str:
    """Strip markdown code fences if GPT-4.1 wrapped output."""
    content = content.strip()
    if content.startswith("```"):
        lines_raw = content.split("\n")
        if lines_raw[-1].strip() == "```":
            lines_raw = lines_raw[1:-1]
        else:
            lines_raw = lines_raw[1:]
        content = "\n".join(lines_raw).strip()
    return content


def _parse_split_output(content: str) -> tuple[str, str]:
    """
    Parse GPT-4.1 output with ===SPLIT=== delimiter into (contextdna, ersim).
    Falls back to treating entire content as ContextDNA if no delimiter found.
    """
    content = _strip_code_fences(content)

    if "===SPLIT===" in content:
        parts = content.split("===SPLIT===", 1)
        contextdna = _strip_code_fences(parts[0])
        ersim = _strip_code_fences(parts[1]) if len(parts) > 1 else ""
    else:
        # No split marker — treat as ContextDNA only (backwards compat)
        contextdna = content
        ersim = ""

    return contextdna, ersim


def _update_plans_via_gpt4(latest_analysis: str) -> bool:
    """
    GPT-4.1 rewrites BOTH project-scoped strategic plans files.
    Called every 4th cycle (~1 hour). Merges new analysis into existing plans.
    Uses ===SPLIT=== delimiter to separate ContextDNA and ER Simulator plans.
    """
    current_contextdna = read_strategic_plans(project="contextdna")
    current_ersim = read_strategic_plans(project="ersim")
    if not current_contextdna:
        current_contextdna = "(empty — first update)"
    if not current_ersim:
        current_ersim = "(empty — first update)"

    prompt = (
        f"Current ContextDNA/Infrastructure plans:\n```\n{current_contextdna}\n```\n\n"
        f"Current ER Simulator plans:\n```\n{current_ersim}\n```\n\n"
        f"Latest analysis from this cycle:\n```\n{latest_analysis}\n```\n\n"
        f"Today's date: {date.today().isoformat()}\n\n"
        "Rewrite BOTH strategic plans files, merging the new analysis findings.\n"
        "Separate the two documents with a line containing only: ===SPLIT==="
    )

    try:
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        from local_llm.llm_gateway import LLMGateway, GatewayConfig

        config = GatewayConfig(
            default_models={"openai": "gpt-4.1", "local": "qwen3-4b"},
        )
        gateway = LLMGateway(config)
        response = gateway.infer(
            prompt=prompt,
            system=PLANS_UPDATE_PROMPT,
            provider="openai",
            max_tokens=6000,  # Increased for two documents
            temperature=0.3,
            session_id="strategic_analyst_plans",
        )

        if response.error or not response.content:
            logger.warning(f"Plans update GPT-4.1 error: {response.error}")
            return False

        # Track cost
        cost = response.cost_usd or 0.0
        _track_s10_cost(cost, response.model_id or "gpt-4.1")

        # Parse split output into two documents
        contextdna_content, ersim_content = _parse_split_output(response.content)

        # Write ContextDNA plans
        if contextdna_content:
            if not contextdna_content.startswith("# Strategic Plans"):
                contextdna_content = "# Strategic Plans — ContextDNA Infrastructure\n\n" + contextdna_content
            write_strategic_plans(contextdna_content, author="gpt-4.1", project="contextdna")
            logger.info(f"ContextDNA plans updated: {len(contextdna_content)} chars")

        # Write ER Simulator plans
        if ersim_content:
            if not ersim_content.startswith("# Strategic Plans"):
                ersim_content = "# Strategic Plans — ER Simulator\n\n" + ersim_content
            write_strategic_plans(ersim_content, author="gpt-4.1", project="ersim")
            logger.info(f"ER Simulator plans updated: {len(ersim_content)} chars")

        logger.info(f"Plans updated: contextdna={len(contextdna_content)} ersim={len(ersim_content)} chars (${cost:.6f})")
        return True

    except Exception as e:
        logger.error(f"Plans update failed: {type(e).__name__}: {str(e)[:200]}")
        return False


# ─── PUBLIC API (All 3 Surgeons) ───────────────────────────────

def read_strategic_plans(project: str = "contextdna") -> str:
    """
    Read a project-scoped strategic plans file.
    Args:
        project: "contextdna" or "ersim". Default: "contextdna".
    Accessible by: Atlas (direct), GPT-4.1 (via this function), Qwen3-4B (via this function).
    """
    try:
        fpath = PLANS_FILES.get(project, PLANS_FILE)
        if os.path.exists(fpath):
            with open(fpath, "r") as f:
                return f.read()
        return ""
    except Exception as e:
        logger.error(f"Failed to read strategic plans ({project}): {e}")
        return ""


def read_all_strategic_plans() -> str:
    """Read both plan files concatenated (for context gathering or display)."""
    parts = []
    for project in PLANS_FILES:
        content = read_strategic_plans(project=project)
        if content and content.strip():
            parts.append(content)
    return "\n\n---\n\n".join(parts) if parts else ""


def write_strategic_plans(content: str, author: str = "unknown", project: str = "contextdna") -> bool:
    """
    Overwrite a project-scoped strategic plans file (full rewrite by GPT-4.1).
    Keeps a timestamp of last update in the header.
    """
    try:
        fpath = PLANS_FILES.get(project, PLANS_FILE)

        # Update the "Last updated" line in header
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("> **Last updated"):
                lines[i] = f"> **Last updated**: {now} by {author}"
                break
        else:
            # No "Last updated" line found — inject after header
            if len(lines) > 1:
                lines.insert(1, f"\n> **Last updated**: {now} by {author}")

        with open(fpath, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"Strategic plans ({project}) written by {author}: {len(content)} chars")
        return True
    except Exception as e:
        logger.error(f"Failed to write strategic plans ({project}): {e}")
        return False


def update_plan_entry(section: str, content: str, author: str = "atlas",
                      project: str = "contextdna") -> bool:
    """
    Add or update an entry in a specific section of a project's plans file.
    Sections: 'threads', 'blind_spots', 'connections', 'priorities', 'archived'

    Used by Atlas or Qwen3-4B to inject individual findings without a full rewrite.
    """
    section_map = {
        "threads": "## Active Threads",
        "blind_spots": "## Blind Spots",
        "connections": "## Connections",
        "priorities": "## Next Priorities",
        "archived": "## Archived",
    }

    header = section_map.get(section)
    if not header:
        logger.error(f"Unknown section: {section}")
        return False

    try:
        current = read_strategic_plans(project=project)
        if not current:
            logger.error(f"Cannot update: plans file empty or missing ({project})")
            return False

        # Find the section and its end (next ## or EOF)
        lines = current.split("\n")
        section_start = None
        section_end = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                section_start = i
            elif section_start is not None and line.startswith("## ") and i > section_start:
                section_end = i
                break

        if section_start is None:
            logger.error(f"Section '{header}' not found in plans file")
            return False
        if section_end is None:
            section_end = len(lines)

        # Insert the new entry before the section end (skip comment lines)
        entry_line = f"- [{author.upper()} {date.today().isoformat()}] {content}"
        insert_at = section_end
        # Find first blank line or end of section content
        for j in range(section_start + 1, section_end):
            if lines[j].strip() == "" and j > section_start + 1:
                insert_at = j
                break
        else:
            insert_at = section_end

        lines.insert(insert_at, entry_line)

        write_strategic_plans("\n".join(lines), author=author, project=project)
        return True
    except Exception as e:
        logger.error(f"Failed to update plan entry: {e}")
        return False


def get_plan_summary(project: str = "") -> str:
    """
    Get a concise summary of active threads and next priorities (for webhook/context).
    Args:
        project: "contextdna", "ersim", or "" (both).
    """
    projects = [project] if project else list(PLANS_FILES.keys())
    all_parts = []

    for proj in projects:
        plans = read_strategic_plans(project=proj)
        if not plans:
            continue

        # Extract just Active Threads and Next Priorities sections
        lines = plans.split("\n")
        summary_parts = []
        in_section = False
        for line in lines:
            if "## 1. Active Threads" in line or "## Active Threads" in line or \
               "## 6. Next Priorities" in line or "## Next Priorities" in line or \
               "## 4. Next Priorities" in line:
                in_section = True
                summary_parts.append(line)
            elif line.startswith("## ") and in_section:
                in_section = False
            elif in_section and line.strip() and not line.startswith("<!--"):
                summary_parts.append(line)

        if summary_parts:
            if len(projects) > 1:
                all_parts.append(f"[{proj}]")
            all_parts.extend(summary_parts)

    return "\n".join(all_parts) if all_parts else "[No active threads]"


# ─── MAIN CYCLE ────────────────────────────────────────────────

def run_strategic_analysis_cycle() -> Optional[str]:
    """
    Main entry point. Called by scheduler every 15 minutes.

    1. Check S10 budget gate
    2. Gather context from 7 sources (including current plans)
    3. Call GPT-4.1 for strategic analysis
    4. Cache formatted result in Redis
    5. Every 4th cycle: GPT-4.1 rewrites plans file with merged findings
    6. Return formatted content (or None on failure/budget)
    """
    # Budget gate
    if not _check_s10_budget():
        logger.info("S10 strategic analyst: budget exhausted, using stale cache")
        return None

    # Track cycle
    cycle = _get_cycle_count()

    # Gather context (now includes plans as 7th source)
    context = _gather_all_context()
    logger.info(f"S10 context gathered: {len(context)} chars from 7 sources (cycle {cycle})")

    # Call GPT-4.1 for S10 analysis
    result = _call_gpt4(context)
    if not result:
        logger.warning("S10 strategic analyst: GPT-4.1 call failed")
        return None

    # Track cost
    _track_s10_cost(result["cost_usd"], result["model"])

    # Format and cache S10 for webhook
    formatted = _format_s10_content(result["content"])
    _cache_s10(formatted)

    logger.info(f"S10 strategic analyst: cached {len(formatted)} chars "
                f"(${result['cost_usd']:.6f}, {result['latency_ms']}ms)")

    # Every 4th cycle: update persistent plans file
    if _is_plan_update_cycle(cycle):
        logger.info(f"S10: plan update cycle ({cycle}) — rewriting strategic plans (both projects)")
        _update_plans_via_gpt4(result["content"])
    else:
        logger.debug(f"S10: cycle {cycle}, next plan update at cycle {cycle + (PLAN_UPDATE_EVERY_N_CYCLES - cycle % PLAN_UPDATE_EVERY_N_CYCLES)}")

    return formatted


# ─── DEEP SCAN PIPELINE ──────────────────────────────────────

# Deep scan: one-time comprehensive review of entire codebase
# Seeds strategic_plans.md with full awareness. ~$18 smart tier.

DEEP_SCAN_SYSTEM = """You are performing a DEEP SCAN of an entire software project for the Surgery Team of 3 (Atlas/Claude Opus 4.6, GPT-4.1, Qwen3-4B local LLM).

Your job: read ALL provided files carefully and produce a comprehensive analysis.

For each batch of files, identify:
1. ARCHITECTURE: How these files fit together, key patterns, dependency chains
2. DEAD CODE: Functions/classes/files that appear unused or abandoned
3. UNFINISHED WORK: TODOs, stubs, partially implemented features, commented-out code
4. BLIND SPOTS: Missing error handling, untested paths, hardcoded values, security concerns
5. CONNECTIONS: How these files relate to previously analyzed batches
6. STRENGTHS: What's well-built and should be preserved

RULES:
- Be SPECIFIC: include file paths, function names, line references
- Be CONCISE: this feeds into a synthesis step, not a human reader
- Focus on STRATEGIC value — what would a new team member NEED to know?
- If you see patterns across files, call them out explicitly
- Mark severity: [CRITICAL] [IMPORTANT] [NOTE]"""

DEEP_SCAN_MERGE_SYSTEM = """You are synthesizing batch analyses of a large codebase into TWO project-scoped strategic plans documents for the Surgery Team of 3 (Atlas/Claude Opus 4.6, GPT-4.1, Qwen3-4B).

PROJECT SEPARATION (STRICT):
- **ContextDNA** = memory infrastructure, webhook pipeline, scheduler, LLM team, IDE panels, Redis cache, session historian, brain, professor, evidence, anticipation engine. Key dirs: memory/, context-dna/, scripts/, admin.contextdna.io/, mcp-servers/
- **ER Simulator** = Apps Script monolith, scenario pipeline, voice AI stack (LiveKit/Kyutai), web-app (Next.js/Django/Supabase), backend, categories/pathways, ATSR, batch tools. Key dirs: simulator-core/, ersim-voice-stack/, web-app/, backend/, google-drive-code/

Each document must include:
1. Active Threads — major work streams with status and key files
2. Architecture Map — how the major subsystems connect
3. Dead Code & Debt — abandoned/unused code to clean up
4. Blind Spots — gaps in testing, error handling, monitoring
5. Connections — cross-cutting patterns and dependencies
6. Next Priorities — what should be tackled next based on full awareness
7. Archived — completed items

RULES:
- This is THE definitive strategic document. Be thorough but concise.
- Every claim must reference specific files/functions that ACTUALLY EXIST
- Status markers: ACTIVE|STALLED|BLOCKED|NEAR-COMPLETE|DEBT
- Keep each section to 10-15 items max (this is the deep version)
- NEVER mix ER Simulator concerns into ContextDNA plans or vice versa

OUTPUT: TWO documents separated by: ===SPLIT===
First: '# Strategic Plans — ContextDNA Infrastructure'
Second: '# Strategic Plans — ER Simulator'"""

# Directories to always skip during deep scan
SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", ".venv-mlx", "dist", "build",
    ".next", ".git", ".DS_Store", "venv", "env", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "eggs", "*.egg-info", ".tox", "htmlcov", "agents",  # shelved dead code
}

# File extensions to scan
SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".sh", ".yaml", ".yml", ".toml", ".json"}

# Max individual file size (skip huge data files)
MAX_FILE_SIZE = 500_000  # 500KB

# Tokens per batch (~800K tokens = ~3.2MB chars, leave room for prompt)
BATCH_CHAR_LIMIT = 3_000_000  # ~750K tokens


def _build_file_queue(base_dir: str) -> list[dict]:
    """
    Build prioritized file queue for deep scan.
    Returns list of {path, size, priority} sorted by priority then path.
    """
    queue = []

    # Priority tiers
    priority_rules = [
        (1, ["memory"], {".py"}),                          # Core engine
        (1, ["context-dna"], {".py"}),                     # ContextDNA
        (2, ["docs"], {".md"}),                            # Documentation
        (2, ["scripts"], {".sh", ".py"}),                  # Scripts
        (3, ["mcp-servers"], SCAN_EXTENSIONS),             # MCP
        (3, ["google-drive-code"], SCAN_EXTENSIONS),       # Google Drive
        (4, ["backend"], SCAN_EXTENSIONS),                 # Backend
        (4, ["simulator-core"], SCAN_EXTENSIONS),          # Simulator
        (4, ["admin.contextdna.io"], SCAN_EXTENSIONS),     # Admin
        (4, ["web-app"], SCAN_EXTENSIONS),                 # Web app
        (4, ["landing-page"], SCAN_EXTENSIONS),            # Landing page
        (5, ["."], {".md", ".json", ".yaml", ".yml", ".toml"}),  # Root config
    ]

    seen = set()

    for priority, dirs, extensions in priority_rules:
        for d in dirs:
            dir_path = os.path.join(base_dir, d) if d != "." else base_dir
            if not os.path.isdir(dir_path):
                continue

            if d == ".":
                # Root level only (no recurse)
                entries = [os.path.join(dir_path, f) for f in os.listdir(dir_path)
                           if os.path.isfile(os.path.join(dir_path, f))]
            else:
                entries = []
                for root, subdirs, files in os.walk(dir_path):
                    # Skip excluded directories
                    subdirs[:] = [s for s in subdirs if s not in SKIP_DIRS
                                  and not s.startswith(".")]
                    for f in files:
                        entries.append(os.path.join(root, f))

            for fpath in entries:
                ext = os.path.splitext(fpath)[1]
                if ext not in extensions:
                    continue

                # Skip already seen
                abs_path = os.path.abspath(fpath)
                if abs_path in seen:
                    continue
                seen.add(abs_path)

                # Skip oversized files
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                if size > MAX_FILE_SIZE or size == 0:
                    continue

                # Get relative path for readability
                rel_path = os.path.relpath(fpath, base_dir)
                queue.append({"path": fpath, "rel_path": rel_path, "size": size, "priority": priority})

    # Sort by priority, then by path for determinism
    queue.sort(key=lambda x: (x["priority"], x["rel_path"]))
    return queue


def _batch_files(queue: list[dict]) -> list[list[dict]]:
    """Group files into batches that fit within token limits."""
    batches = []
    current_batch = []
    current_size = 0

    for item in queue:
        if current_size + item["size"] > BATCH_CHAR_LIMIT and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(item)
        current_size += item["size"]

    if current_batch:
        batches.append(current_batch)
    return batches


def _read_batch_files(batch: list[dict]) -> str:
    """Read all files in a batch into a single prompt string."""
    parts = []
    for item in batch:
        try:
            with open(item["path"], "r", errors="replace") as f:
                content = f.read()
            parts.append(f"=== FILE: {item['rel_path']} ({item['size']} bytes) ===\n{content}\n")
        except Exception as e:
            parts.append(f"=== FILE: {item['rel_path']} === [READ ERROR: {e}]\n")
    return "\n".join(parts)


def _scan_batch(batch_content: str, batch_num: int, total_batches: int,
                prev_synthesis: str = "") -> Optional[dict]:
    """Send one batch to GPT-4.1 for analysis. Returns {content, cost_usd} or None."""
    prompt_parts = [
        f"BATCH {batch_num}/{total_batches} — Deep Scan\n\n",
        batch_content,
    ]
    if prev_synthesis:
        prompt_parts.append(f"\n\n=== PREVIOUS BATCH SYNTHESIS (for context) ===\n{prev_synthesis[:3000]}")

    prompt = "".join(prompt_parts)

    try:
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        from local_llm.llm_gateway import LLMGateway, GatewayConfig

        config = GatewayConfig(
            default_models={"openai": "gpt-4.1", "local": "qwen3-4b"},
        )
        gateway = LLMGateway(config)
        response = gateway.infer(
            prompt=prompt,
            system=DEEP_SCAN_SYSTEM,
            provider="openai",
            max_tokens=4096,
            temperature=0.3,
            session_id=f"deep_scan_batch_{batch_num}",
        )

        if response.error or not response.content:
            logger.warning(f"Deep scan batch {batch_num} failed: {response.error}")
            return None

        return {
            "content": response.content,
            "cost_usd": response.cost_usd or 0.0,
        }
    except Exception as e:
        logger.error(f"Deep scan batch {batch_num} error: {type(e).__name__}: {str(e)[:200]}")
        return None


def _merge_syntheses(batch_syntheses: list[str], current_plans: str) -> Optional[dict]:
    """Final GPT-4.1 call: merge all batch syntheses into comprehensive plans."""
    combined = "\n\n---\n\n".join(
        f"=== BATCH {i+1} ANALYSIS ===\n{s}" for i, s in enumerate(batch_syntheses)
    )
    prompt = (
        f"You have analyzed the ENTIRE codebase in {len(batch_syntheses)} batches.\n\n"
        f"Current strategic plans:\n```\n{current_plans[:2000]}\n```\n\n"
        f"Batch analyses:\n{combined}\n\n"
        f"Today's date: {date.today().isoformat()}\n\n"
        "Synthesize ALL batch analyses into one comprehensive Strategic Plans document."
    )

    try:
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        from local_llm.llm_gateway import LLMGateway, GatewayConfig

        config = GatewayConfig(
            default_models={"openai": "gpt-4.1", "local": "qwen3-4b"},
        )
        gateway = LLMGateway(config)
        response = gateway.infer(
            prompt=prompt,
            system=DEEP_SCAN_MERGE_SYSTEM,
            provider="openai",
            max_tokens=8192,
            temperature=0.3,
            session_id="deep_scan_merge",
        )

        if response.error or not response.content:
            logger.warning(f"Deep scan merge failed: {response.error}")
            return None

        return {
            "content": response.content,
            "cost_usd": response.cost_usd or 0.0,
        }
    except Exception as e:
        logger.error(f"Deep scan merge error: {type(e).__name__}: {str(e)[:200]}")
        return None


def run_deep_scan(max_batches: int = 0, dry_run: bool = False) -> Optional[str]:
    """
    One-time deep scan of entire codebase via GPT-4.1.
    Seeds strategic_plans.md with comprehensive awareness.

    Args:
        max_batches: Limit number of batches (0 = all). Useful for testing.
        dry_run: If True, show what would be scanned without calling GPT-4.1.

    Returns:
        Summary string or None on failure.
    """
    import time as _time

    base_dir = os.path.dirname(os.path.dirname(__file__))
    print(f"Building file queue from {base_dir}...")

    # Build prioritized queue
    queue = _build_file_queue(base_dir)
    total_size = sum(f["size"] for f in queue)
    print(f"Found {len(queue)} files, {total_size / 1_000_000:.1f} MB total")

    # Show priority breakdown
    from collections import Counter
    pri_counts = Counter(f["priority"] for f in queue)
    pri_sizes = {}
    for f in queue:
        pri_sizes[f["priority"]] = pri_sizes.get(f["priority"], 0) + f["size"]
    for p in sorted(pri_counts.keys()):
        print(f"  P{p}: {pri_counts[p]} files, {pri_sizes[p] / 1_000_000:.1f} MB")

    # Batch
    batches = _batch_files(queue)
    if max_batches > 0:
        batches = batches[:max_batches]
    print(f"Organized into {len(batches)} batches")

    # Estimate cost
    est_input_tokens = total_size / 4  # ~4 chars per token
    est_cost = (est_input_tokens * 2 / 1_000_000) + (len(batches) * 4096 * 8 / 1_000_000)
    print(f"Estimated cost: ${est_cost:.2f} (input) + ${len(batches) * 0.033:.2f} (output) = ${est_cost + len(batches) * 0.033:.2f}")

    if dry_run:
        print("\n=== DRY RUN — would scan these batches: ===")
        for i, batch in enumerate(batches):
            batch_size = sum(f["size"] for f in batch)
            print(f"  Batch {i+1}: {len(batch)} files, {batch_size / 1_000_000:.1f} MB")
            for f in batch[:5]:
                print(f"    {f['rel_path']} ({f['size']} bytes)")
            if len(batch) > 5:
                print(f"    ... and {len(batch) - 5} more")
        return "DRY RUN complete"

    # Execute batches
    total_cost = 0.0
    batch_syntheses = []
    start = _time.time()

    for i, batch in enumerate(batches):
        batch_size = sum(f["size"] for f in batch)
        print(f"\n--- Batch {i+1}/{len(batches)}: {len(batch)} files, {batch_size / 1_000_000:.1f} MB ---")

        content = _read_batch_files(batch)
        prev = batch_syntheses[-1] if batch_syntheses else ""
        result = _scan_batch(content, i + 1, len(batches), prev)

        if result:
            batch_syntheses.append(result["content"])
            total_cost += result["cost_usd"]
            _track_s10_cost(result["cost_usd"], "gpt-4.1")
            print(f"  OK: {len(result['content'])} chars (${result['cost_usd']:.4f})")
        else:
            batch_syntheses.append(f"[Batch {i+1} failed]")
            print(f"  FAILED — continuing...")

    elapsed = _time.time() - start
    print(f"\n=== Batch analysis complete: {len(batch_syntheses)} syntheses, ${total_cost:.4f}, {elapsed:.0f}s ===")

    # Final merge
    print("\nMerging all syntheses into comprehensive plans...")
    current_plans = read_all_strategic_plans()
    merge_result = _merge_syntheses(batch_syntheses, current_plans)

    if merge_result:
        total_cost += merge_result["cost_usd"]
        _track_s10_cost(merge_result["cost_usd"], "gpt-4.1")

        # Parse split output into two project-scoped documents
        contextdna_content, ersim_content = _parse_split_output(merge_result["content"])

        total_chars = 0
        if contextdna_content:
            if not contextdna_content.startswith("# Strategic Plans"):
                contextdna_content = "# Strategic Plans — ContextDNA Infrastructure\n\n" + contextdna_content
            write_strategic_plans(contextdna_content, author="gpt-4.1-deep-scan", project="contextdna")
            total_chars += len(contextdna_content)
            print(f"  ContextDNA plans: {len(contextdna_content)} chars")

        if ersim_content:
            if not ersim_content.startswith("# Strategic Plans"):
                ersim_content = "# Strategic Plans — ER Simulator\n\n" + ersim_content
            write_strategic_plans(ersim_content, author="gpt-4.1-deep-scan", project="ersim")
            total_chars += len(ersim_content)
            print(f"  ER Simulator plans: {len(ersim_content)} chars")

        total_elapsed = _time.time() - start

        summary = (
            f"Deep scan complete: {len(queue)} files, {len(batches)} batches, "
            f"${total_cost:.4f} total, {total_elapsed:.0f}s elapsed. "
            f"Plans updated: {total_chars} chars across {len(PLANS_FILES)} project files."
        )
        print(f"\n{summary}")
        return summary
    else:
        # Merge failed — save batch syntheses as fallback
        fallback = "\n\n---\n\n".join(batch_syntheses)
        fallback_path = os.path.join(os.path.dirname(__file__), "deep_scan_raw.md")
        with open(fallback_path, "w") as f:
            f.write(f"# Deep Scan Raw Results\n\n{fallback}")
        print(f"Merge failed — raw syntheses saved to {fallback_path}")
        return None


# ─── BOLUS SCAN (On-Demand Deep Dive) ─────────────────────────

BOLUS_SYSTEM = """You are performing a FOCUSED DEEP DIVE into specific files/subjects for the Surgery Team of 3.

The user has a specific research question. Analyze the provided files thoroughly and answer it.

RULES:
- Be SPECIFIC: file paths, function names, line references
- Answer the ACTUAL QUESTION, don't just summarize files
- If you find issues, grade them: [CRITICAL] [IMPORTANT] [NOTE]
- If the answer requires changes, propose specific code modifications
- If related files should also be reviewed, mention them
- EXTREME CONCISION. Strategic value only."""


def run_bolus_scan(query: str, files: list[str] = None,
                   glob_pattern: str = "", max_tokens: int = 4096) -> Optional[str]:
    """
    On-demand GPT-4.1 deep dive into specific files or subjects.

    Args:
        query: Research question or subject to investigate
        files: Specific file paths to analyze (relative to repo root)
        glob_pattern: Alternative — glob pattern to find files (e.g. "memory/*cache*.py")
        max_tokens: Max output tokens for GPT-4.1

    Returns:
        Analysis string or None on failure.
    """
    import glob as _glob

    base_dir = os.path.dirname(os.path.dirname(__file__))

    # Resolve files
    file_paths = []
    if files:
        for f in files:
            fpath = os.path.join(base_dir, f) if not os.path.isabs(f) else f
            if os.path.isfile(fpath):
                file_paths.append(fpath)
            else:
                logger.warning(f"Bolus: file not found: {f}")

    if glob_pattern:
        pattern = os.path.join(base_dir, glob_pattern)
        file_paths.extend(_glob.glob(pattern, recursive=True))

    # If no files specified, use grep to find relevant files
    if not file_paths:
        try:
            result = subprocess.run(
                ["grep", "-rl", "--include=*.py", "--include=*.md",
                 "--include=*.ts", "--include=*.sh", query.split()[0],
                 base_dir],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout:
                file_paths = [f.strip() for f in result.stdout.strip().split("\n")[:20]]
        except Exception:
            pass

    if not file_paths:
        return f"No files found for query: {query}"

    # Read files
    parts = []
    total_size = 0
    for fpath in file_paths:
        try:
            size = os.path.getsize(fpath)
            if size > MAX_FILE_SIZE:
                continue
            with open(fpath, "r", errors="replace") as f:
                content = f.read()
            rel = os.path.relpath(fpath, base_dir)
            parts.append(f"=== FILE: {rel} ({size} bytes) ===\n{content}\n")
            total_size += size
            if total_size > BATCH_CHAR_LIMIT:
                break
        except Exception:
            continue

    if not parts:
        return "Could not read any matching files"

    file_content = "\n".join(parts)
    prompt = f"RESEARCH QUESTION: {query}\n\nFILES ({len(parts)} files, {total_size} bytes):\n\n{file_content}"

    print(f"Bolus scan: {len(parts)} files, {total_size / 1000:.0f} KB, query: {query}")

    # Call GPT-4.1
    try:
        import sys
        cdna_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "context-dna")
        if cdna_path not in sys.path:
            sys.path.insert(0, cdna_path)

        env_path = os.path.join(cdna_path, ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)

        from local_llm.llm_gateway import LLMGateway, GatewayConfig

        config = GatewayConfig(
            default_models={"openai": "gpt-4.1", "local": "qwen3-4b"},
        )
        gateway = LLMGateway(config)
        response = gateway.infer(
            prompt=prompt,
            system=BOLUS_SYSTEM,
            provider="openai",
            max_tokens=max_tokens,
            temperature=0.3,
            session_id="bolus_scan",
        )

        if response.error or not response.content:
            logger.warning(f"Bolus scan failed: {response.error}")
            return None

        cost = response.cost_usd or 0.0
        _track_s10_cost(cost, response.model_id or "gpt-4.1")
        print(f"Bolus complete: {len(response.content)} chars, ${cost:.4f}")
        return response.content

    except Exception as e:
        logger.error(f"Bolus scan error: {type(e).__name__}: {str(e)[:200]}")
        return None


# CLI for manual testing
if __name__ == "__main__":
    import sys as _sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cmd = _sys.argv[1] if len(_sys.argv) > 1 else ""

    # Parse --project flag from any position
    _project_filter = ""
    for arg in _sys.argv[2:]:
        if arg.startswith("--project="):
            _project_filter = arg.split("=")[1]

    if cmd == "plans":
        # Force plan update (rewrites both project files)
        print("=== Forcing plan update cycle (both projects) ===")
        context = _gather_all_context()
        result = _call_gpt4(context)
        if result:
            _track_s10_cost(result["cost_usd"], result["model"])
            print(result["content"][:300])
            print("\n--- Updating plans ---")
            _update_plans_via_gpt4(result["content"])
            for proj in PLANS_FILES:
                content = read_strategic_plans(project=proj)
                print(f"\n=== {proj} ({len(content)} chars) ===")
                print(content[:300])
        else:
            print("GPT-4.1 call failed")

    elif cmd == "read":
        # Read current plans (optionally filtered by --project=)
        if _project_filter:
            print(read_strategic_plans(project=_project_filter))
        else:
            print(read_all_strategic_plans())

    elif cmd == "deep-scan":
        # One-time deep scan of entire codebase
        dry = "--dry-run" in _sys.argv
        max_b = 0
        for arg in _sys.argv[2:]:
            if arg.startswith("--max-batches="):
                max_b = int(arg.split("=")[1])
        result = run_deep_scan(max_batches=max_b, dry_run=dry)
        if not result:
            print("Deep scan failed")

    elif cmd == "bolus":
        # On-demand deep dive into specific files/subject
        if len(_sys.argv) < 3:
            print("Usage: strategic_analyst.py bolus <query> [--files file1,file2,...]")
            _sys.exit(1)
        query = _sys.argv[2]
        files = []
        for arg in _sys.argv[3:]:
            if arg.startswith("--files="):
                files = arg.split("=")[1].split(",")
        result = run_bolus_scan(query=query, files=files)
        if result:
            print(result)
        else:
            print("Bolus scan failed")

    elif cmd == "help":
        print("Usage: strategic_analyst.py [command] [options]")
        print("  (none)     — Run regular S10 analysis cycle")
        print("  plans      — Force plan update via GPT-4.1 (both projects)")
        print("  read       — Read current strategic plans (both projects)")
        print("    --project=contextdna|ersim   Filter to one project")
        print("  deep-scan  — One-time full codebase scan (~$18, ~10min)")
        print("    --dry-run           Show what would be scanned")
        print("    --max-batches=N     Limit batches (for testing)")
        print("  bolus <query>         On-demand deep dive")
        print("    --files=f1,f2,...   Specific files to analyze")
        print("  help       — Show this help")

    else:
        result = run_strategic_analysis_cycle()
        if result:
            print(result)
        else:
            print("No result (budget exhausted or GPT-4.1 unavailable)")
