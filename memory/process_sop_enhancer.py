"""
PROCESS SOP ENHANCER - Chain Format Title Generation

Generates enhanced titles for PROCESS SOPs using the chain format.
This enhancer ONLY works on process SOPs - bug-fix SOPs should use bugfix_sop_enhancer.py.

PHILOSOPHY: Process SOPs describe HOW to do things. Unlike bug-fix SOPs which diagnose
problems, process SOPs instruct on procedures. The heart is the GOAL, not the problem.

KEY INSIGHT: Process SOPs are LIVING DOCUMENTS that GROW over time.
- Bug-fix SOPs: Convergent (one problem → one fix) → SHORT, point-in-time capture
- Process SOPs: Divergent (one goal → many routes) → LONG, accumulates successful routes

FUTURE VISION: Process SOPs should accumulate ALL successful routes to the goal,
listed in order of preference:
  [process SOP] Deploy Django to production:
    Route 1 (preferred): via (systemctl) → restart → ✓ healthy
    Route 2: via (docker) → rebuild → deploy → ✓ healthy
    Route 3: via (ansible) → playbook → ✓ complete

Each new successful approach adds a route. Routes ranked by reliability/speed.
Current implementation: Multi-route accumulation via route_tracker.py.
Routes tracked per goal with success/fail counts, scoring, and date stamps.
See: generate_process_sop_with_routes(), generate_route_aware_process_sop().

CHAIN FORMAT:
[process SOP] [GOAL HEART]: via (tools) → step1 → step2 → ✓ verification

Zone Structure:
- Zone 1 (GOAL): The task objective - what to accomplish
- Zone 2 (VIA): Tools/methods used - how to do it
- Zones 3-N (CHAIN): Ordered steps - the sequence
- Final (CHECK): Verification - how to confirm success

Example:
    Input: "Deploy Django to production" + "Used systemctl restart gunicorn"
    Output: "[process SOP] Deploy Django to production: via (systemctl) → deploy → restart → ✓ healthy"

What Atlas needs from process SOPs:
1. Clear GOAL - what to accomplish
2. Tools listed - what METHOD to use
3. Ordered steps - the SEQUENCE
4. Success criteria - how to VERIFY

BOUNDARY: Returns None for bug-fix SOPs (use bugfix_sop_enhancer.py)
"""

import re
from typing import Optional, Tuple, List, Set, Dict, Any

# Divider line for negative patterns section
NEGATIVE_DIVIDER = "  ─── What doesn't work (learned from experience) ───"
DEFAULT_NEGATIVE_THRESHOLD = 3

# =============================================================================
# SOP TYPE DETECTION
# =============================================================================

def detect_sop_type(combined: str) -> str:
    """
    Detect whether content describes a bug-fix or process SOP.

    Uses CONTEXTUALLY-AWARE detection including POTENTIAL LENGTH signals:
    - Growth signals → process (various ROUTES to same goal, will accumulate)
    - Finality signals → bugfix (antecedent → ONE fix per cause, converges)

    KEY INSIGHT (from user):
    - Process SOPs have MULTIPLE ROUTES from A to B → grows as each route is documented
    - Bug-fix SOPs have multiple ANTECEDENTS but each has ONE fix → converges to solutions

    Phrase meaning depends on tense and surrounding words:
      "The alternative was to restart" → FINALITY (past tense = solution found)
      "An alternative approach would be" → GROWTH (present/future = route option)

    Returns:
        'bugfix' or 'process'
    """
    combined_lower = combined.lower()

    # === KEYWORD-BASED DETECTION ===
    # Process words indicate procedural SOPs
    process_words = {'deploy', 'rollback', 'setup', 'configure', 'install', 'migrate',
                     'backup', 'restore', 'update', 'upgrade', 'create', 'build',
                     'provision', 'initialize', 'init', 'bootstrap', 'spin up',
                     'scale', 'teardown', 'cleanup', 'generate'}
    is_process = any(pw in combined_lower for pw in process_words)

    # Problem words indicate bug-fix SOPs
    problem_words = {'fix', 'error', 'block', 'blocking', 'fail', 'crash',
                     'broken', 'wrong', 'missing', 'bug', 'issue', 'latency',
                     'timeout', 'hang', 'freeze', 'slow', 'stuck',
                     '500', '502', '503', '504', '400', '401', '403', '404'}
    is_bug_fix = any(pw in combined_lower for pw in problem_words) and not is_process

    # === CONTEXTUALLY-AWARE POTENTIAL LENGTH SIGNALS ===

    # --- GROWTH SIGNALS (process) ---
    # Forward-looking ROUTE language (present/future tense, indefinite article)
    # These signal "multiple ways to achieve the same goal" = living document
    growth_signals = [
        r'\b(an|another)\s+(alternative|route|option|way)\b', # "another route" = more routes
        r'\balternative\s+(approaches?|methods?|ways?|routes?)\s+(is|are|would|could|include)\b',
        r'\b(can|could|might)\s+also\b',                      # additional possibilities
        r'\b(or|alternatively)\s+you\s+(can|could)\b',        # user has route choices
        r'\b(there are|here are)\s+(several|multiple|many)\b',  # explicit multiplicity
        r'\b(best practice|recommended|preferred)\s+(is|to)\b',  # preference among routes
        r'\bdepends\s+on\s+(the|your|which)\b',               # conditional routing
        r'\b(choose|select)\s+(between|from|a)\b',            # route selection
        r'\b(multiple|several|various)\s+(ways|methods|approaches|options|routes)\b',
        r'\broute\s+\d+\b',                                   # explicit route numbering
        r'\balternatives?\s+(include|are)\b',                 # "alternatives include"
    ]
    has_growth = any(re.search(p, combined_lower) for p in growth_signals)

    # --- FINALITY SIGNALS (bugfix) ---
    # Past-tense resolution language (the single answer was found)
    # These signal "this cause has ONE fix" = point-in-time capture
    finality_signals = [
        r'\b(the|this)\s+(fix|solution|answer)\s+(was|is)\b',  # definite article + past
        r'\balternative\s+was\s+to\b',                         # "alternative was" = that WAS the fix
        r'\bthe\s+only\s+(option|way|solution|fix)\b',         # single path, no growth
        r'\bjust\s+(needed|had)\s+to\b',                       # minimal single action
        r'\bonly\s+(needed|required|had)\s+to\b',              # single action sufficed
        r'\b(root\s+cause|culprit|issue|problem)\s+was\b',     # diagnosis complete
        r'\bturned\s+out\s+to\s+be\b',                         # discovery/finality
        r'\bactually\s+(was|needed|required)\b',               # revelation of single cause
        r'\b(finally|eventually)\s+(fixed|solved|resolved)\b', # search is over
        r'\bthis\s+(fixed|solved|resolved)\s+(the|it)\b',      # solution applied
        r'\bafter\s+.{0,20}\s+it\s+(worked|fixed)\b',          # action led to resolution
        r'\b(cause|antecedent)\s+.{0,10}\s+fix\b',             # antecedent→fix pair
    ]
    has_finality = any(re.search(p, combined_lower) for p in finality_signals)

    # --- AMBIGUITY BREAKERS ---
    # Tense-aware modifiers that tip the scale
    if re.search(r'\b(was|were|had been)\s+the\s+(only|single)\b', combined_lower):
        has_finality = True  # Past definite = finality
    if re.search(r'\b(is|are|will be)\s+(another|one)\s+(option|route)\b', combined_lower):
        has_growth = True  # Present/future indefinite = growth

    # === DECIDE with potential length override ===
    # If strong finality signals, override to bugfix (even if has process words)
    if has_finality and not has_growth:
        return 'bugfix'

    # If strong growth signals, confirm/override to process
    if has_growth and not has_finality:
        return 'process'

    # Fall back to keyword detection
    return 'bugfix' if is_bug_fix else 'process'


# =============================================================================
# PROCESS ZONES EXTRACTION
# =============================================================================

def extract_process_zones(combined: str) -> str:
    """
    Extract zones for PROCESS SOPs using the chain format.

    CHAIN FORMAT:
    via (routes) → step1 → step2 → ... → ✓ verification

    Philosophy: Process SOPs describe HOW to do things. They show:
    - Available routes/tools (methods to accomplish)
    - Sequence of steps (the chain/flow)
    - Verification (how to know it worked)

    Args:
        combined: Combined text (task + details) for zone extraction

    Returns:
        Arrow-separated chain parts, or empty string if insufficient content
    """
    combined_lower = combined.lower()

    # === EXTRACT ROUTES/TOOLS MENTIONED ===
    routes = []
    route_vocab = {
        'systemctl': 'systemctl', 'systemd': 'systemctl',
        'docker': 'docker', 'docker-compose': 'docker-compose', 'compose': 'docker-compose',
        'ansible': 'ansible', 'terraform': 'terraform',
        'kubectl': 'kubectl', 'k8s': 'kubectl', 'kubernetes': 'kubectl',
        'ssm': 'SSM', 'aws ssm': 'SSM',
        'ecs': 'ECS', 'lambda': 'Lambda',
        'ec2': 'EC2', 'boto3': 'boto3',
        'ssh': 'SSH', 'api': 'API', 'cli': 'CLI',
        'script': 'script', 'cron': 'cron', 'hook': 'hook',
        'git': 'git', 'npm': 'npm', 'pip': 'pip',
        'make': 'make', 'bash': 'bash', 'python': 'python',
        'gunicorn': 'gunicorn', 'nginx': 'nginx', 'redis': 'redis',
        'postgres': 'postgres', 'rabbitmq': 'rabbitmq',
    }
    for raw, normalized in route_vocab.items():
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower) and normalized not in routes:
            routes.append(normalized)
            if len(routes) >= 3:
                break

    # === EXTRACT CHAIN STEPS MENTIONED ===
    seen_groups = {}

    step_vocab = {
        # raw_word: (normalized, order, group)
        'backup': ('backup', 1, 'save'), 'back up': ('backup', 1, 'save'),
        'snapshot': ('snapshot', 1, 'save'),
        'stop': ('stop', 5, 'stop'), 'down': ('down', 5, 'stop'),
        'pull': ('pull', 10, 'fetch'), 'fetch': ('fetch', 10, 'fetch'),
        'clone': ('clone', 11, 'clone'),
        'build': ('build', 15, 'build'),
        'deploy': ('deploy', 20, 'deploy'),
        'install': ('install', 22, 'install'),
        'migrate': ('migrate', 25, 'migrate'),
        'configure': ('configure', 28, 'configure'), 'config': ('configure', 28, 'configure'),
        'restore': ('restore', 29, 'restore'),
        'start': ('start', 30, 'start'), 'up': ('up', 30, 'start'),
        'restart': ('restart', 31, 'start'),
        'reload': ('reload', 32, 'start'),
        'verify': ('verify', 50, 'verify'), 'check': ('check', 50, 'verify'),
        'test': ('test', 51, 'verify'),
        'monitor': ('monitor', 55, 'monitor'),
    }

    for raw, (normalized, order, group) in step_vocab.items():
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower):
            if group not in seen_groups:
                seen_groups[group] = (normalized, order)
            else:
                existing, existing_order = seen_groups[group]
                prefer_map = {'restart': 'start', 'verify': 'check', 'deploy': 'install'}
                if normalized in prefer_map and existing == prefer_map[normalized]:
                    seen_groups[group] = (normalized, order)

    sorted_steps = sorted(seen_groups.values(), key=lambda x: x[1])
    chain_steps = [step for step, _ in sorted_steps[:5]]

    # === EXTRACT VERIFICATION/OUTCOME MENTIONED ===
    verifications = []
    verification_vocab = {
        '200': '200 OK', '200 ok': '200 OK',
        'healthy': 'healthy', 'running': 'running',
        'logs clean': 'logs clean', 'journalctl': 'logs clean',
        '/health': '/health OK', 'curl': 'curl OK',
        'docker ps': 'containers up',
        'success': 'success', 'complete': 'complete',
    }
    for raw, normalized in verification_vocab.items():
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower) and normalized not in verifications:
            verifications.append(normalized)
            if len(verifications) >= 2:
                break

    # === BUILD OUTPUT ===
    parts = []

    if routes:
        parts.append(f"via ({', '.join(routes)})")

    if chain_steps:
        parts.extend(chain_steps)

    last_step = chain_steps[-1] if chain_steps else ''
    if verifications and last_step not in ('verify', 'check', 'test', 'monitor'):
        parts.append(f"✓ {verifications[0]}")

    if len(parts) >= 2:
        return ' → '.join(parts)

    return ""


# =============================================================================
# GOAL HEART GENERATION
# =============================================================================

# Filler words to remove from goal heart
FILLER_WORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'this', 'that',
    'these', 'those', 'it', 'they', 'and', 'but', 'or', 'for', 'with',
    'successfully', 'completed', 'remember', 'apply', 'approach', 'similar',
    'future', 'tasks', 'ensure', 'verify', 'quick', 'clear', 'good', 'just',
    'also', 'very', 'really', 'actually', 'basically', 'simply', 'about',
    'now', 'then', 'here', 'there', 'when', 'where', 'how', 'what', 'which',
    'who', 'whom', 'whose', 'why', 'need', 'needs', 'needed', 'want', 'wants',
}


def generate_goal_heart(task: str) -> str:
    """
    Generate the GOAL heart for a process SOP.

    Unlike bug-fix hearts which need sophisticated scoring to capture problem essence,
    process hearts are SIMPLER - they preserve the original goal statement with cleanup.

    Philosophy: The task description IS the goal. Don't over-process it.

    Args:
        task: Original task description

    Returns:
        Clean goal heart (capitalized, trimmed, de-fillered if needed)
    """
    # If task already looks clean and descriptive, preserve it
    if len(task) >= 15 and len(task) <= 80:
        # Just capitalize and return
        heart = task.strip()
        if heart:
            heart = heart[0].upper() + heart[1:]
        return heart

    # For very long tasks, try to extract the core goal
    words = task.split()

    if len(words) <= 3:
        # Short task - use as-is
        heart = task.strip()
        if heart:
            heart = heart[0].upper() + heart[1:]
        return heart

    # For longer tasks, try to find the action + object pattern
    # "Deploy Django to production server using systemctl" → "Deploy Django to production"

    # Find first verb-like word (action)
    action_words = {'deploy', 'configure', 'setup', 'install', 'create', 'build',
                    'update', 'upgrade', 'migrate', 'backup', 'restore', 'start',
                    'stop', 'restart', 'provision', 'initialize', 'scale', 'rollback',
                    'add', 'remove', 'enable', 'disable', 'set', 'run', 'execute'}

    action_idx = -1
    for i, word in enumerate(words):
        if word.lower() in action_words:
            action_idx = i
            break

    if action_idx >= 0:
        # Take from action word to a reasonable endpoint
        # Stop at words like "using", "with", "via", "by"
        stop_words = {'using', 'with', 'via', 'by', 'through', 'after', 'before'}
        end_idx = len(words)
        for i in range(action_idx + 1, len(words)):
            if words[i].lower() in stop_words:
                end_idx = i
                break
            if i - action_idx > 6:  # Max 7 words in heart
                end_idx = i + 1
                break

        heart_words = words[action_idx:end_idx]
        heart = ' '.join(heart_words)
    else:
        # No clear action found - use first 6 meaningful words
        meaningful = [w for w in words if w.lower() not in FILLER_WORDS][:6]
        heart = ' '.join(meaningful) if meaningful else task[:60]

    # Clean up
    heart = heart.strip()
    if heart:
        heart = heart[0].upper() + heart[1:]

    return heart[:80]  # Max 80 chars for heart


# =============================================================================
# MAIN TITLE GENERATOR
# =============================================================================

def generate_process_sop_title(task: str, details: str = None) -> Optional[str]:
    """
    Generate optimal SOP title for PROCESS SOPs using chain format.

    BOUNDARY: This enhancer ONLY processes process SOPs.
    - Returns None for bug-fix SOPs (bugfix_sop_enhancer.py handles those)
    - Detects process via action keywords (deploy, configure, setup, etc.)

    PHILOSOPHY: Process SOPs describe HOW to do things. The heart is the GOAL.

    Process:
    1. FIRST: Verify this is a process SOP (not bug-fix)
    2. THEN: Generate goal heart from task description
    3. THEN: Extract chain zones (via tools → steps → verification)
    4. COMBINE: [process SOP] [GOAL]: zones

    Args:
        task: Original task description
        details: Additional details for zone extraction

    Returns:
        Process SOP title with chain format, or None if content is bug-fix
    """
    combined = f"{task} {details or ''}"

    # === BOUNDARY CHECK: Only process process SOPs ===
    # Bug-fix SOPs should use bugfix_sop_enhancer.py
    sop_type = detect_sop_type(combined)
    if sop_type == 'bugfix':
        return None  # Signal caller to use bugfix_sop_enhancer

    # === CHECK FOR ALREADY-FORMATTED TITLES ===
    already_has_arrows = '→' in task
    already_has_zones = ':' in task and ('→' in task or '(' in task.split(':')[-1])
    already_has_tag = task.startswith('[bug-fix SOP]') or task.startswith('[process SOP]')

    if already_has_arrows or already_has_zones or already_has_tag:
        # Title is already formatted - return as-is with minimal cleanup
        best = task.strip()
        if best and not best.startswith('['):
            best = best[0].upper() + best[1:]
        return best[:180]

    # === STEP 1: GENERATE GOAL HEART ===
    # The heart is the GOAL - what to accomplish
    goal_heart = generate_goal_heart(task)

    # === STEP 2: EXTRACT CHAIN ZONES ===
    zone_parts = extract_process_zones(combined)

    # === STEP 3: COMBINE INTO FINAL TITLE ===
    if zone_parts:
        title = f"{goal_heart}: {zone_parts}"
    else:
        # No zones extracted - just use the goal heart
        title = goal_heart

    # Capitalize first letter
    if title:
        title = title[0].upper() + title[1:]

    # === STEP 4: ADD PROCESS SOP TAG ===
    title = f"[process SOP] {title}"

    return title[:180]


# =============================================================================
# NEGATIVE PATTERN INTEGRATION — SOP Quality Evolution
# =============================================================================

def get_negative_patterns_for_goal(goal: str, min_frequency: int = DEFAULT_NEGATIVE_THRESHOLD) -> List[Dict]:
    """
    Get frequent negative patterns related to a process SOP goal.

    Only returns patterns that have been observed min_frequency+ times.
    This prevents SOP bloat — only proven anti-patterns earn a spot.

    Args:
        goal: The SOP goal (e.g., "Deploy Django to production")
        min_frequency: Minimum occurrences required (default 3)

    Returns:
        List of pattern dicts ready for SOP inclusion
    """
    try:
        from memory.sqlite_storage import get_sqlite_storage
        store = get_sqlite_storage()

        # First try exact goal match
        patterns = store.get_frequent_negative_patterns(
            min_frequency=min_frequency, goal=goal
        )

        # Also try fuzzy match on goal keywords if exact match yields nothing
        if not patterns and goal:
            all_frequent = store.get_frequent_negative_patterns(
                min_frequency=min_frequency
            )
            goal_words = set(goal.lower().split())
            for p in all_frequent:
                p_goal_words = set((p.get("goal", "") or "").lower().split())
                p_desc_words = set((p.get("description", "") or "").lower().split())
                overlap = goal_words & (p_goal_words | p_desc_words)
                if len(overlap) >= 2:  # At least 2 keyword overlap
                    patterns.append(p)

        return patterns
    except Exception:
        return []


def format_negative_patterns_section(patterns: List[Dict]) -> str:
    """
    Format negative patterns into the divider section for SOP content.

    Output format:
      ─── What doesn't work (learned from experience) ───
      ✗ docker restart (doesn't reload env vars — must recreate)
      ✗ direct gunicorn restart (orphans workers)

    Args:
        patterns: List of pattern dicts from get_negative_patterns_for_goal()

    Returns:
        Formatted string with divider + anti-patterns, or empty string if none
    """
    if not patterns:
        return ""

    lines = [NEGATIVE_DIVIDER]
    for p in patterns:
        desc = p.get("description", p.get("pattern_key", "unknown"))
        freq = p.get("frequency", 0)
        # Compact format: description + frequency hint
        lines.append(f"  ✗ {desc} ({freq}x observed)")

    return "\n".join(lines)


# =============================================================================
# MULTI-ROUTE SOP GENERATION (ADDITIVE)
# =============================================================================

def generate_process_sop_with_routes(task: str, details: str = None,
                                     include_routes: bool = True,
                                     include_negative: bool = True) -> Optional[str]:
    """
    Generate process SOP title with accumulated multi-route history.

    This extends generate_process_sop_title() by:
    1. Looking up existing routes for this goal
    2. Recording the current route as a success
    3. Returning multi-route format if routes exist
    4. Appending frequent negative patterns below a divider (3+ occurrences)

    Output format when routes AND negative patterns exist:
    [process SOP] Deploy Django to production:
      (passed 01/23/26) Route 1 (95%): via (systemctl) -> restart -> healthy
      (passed 01/20/26) Route 2 (80%): via (docker) -> rebuild -> deploy -> healthy
      ─── What doesn't work (learned from experience) ───
      ✗ docker restart (doesn't reload env vars — must recreate) (4x observed)
      ✗ direct gunicorn restart (orphans workers) (3x observed)

    Args:
        task: Original task description
        details: Additional details for zone extraction
        include_routes: Whether to include route history (False = single-line format)
        include_negative: Whether to include frequent negative patterns

    Returns:
        Process SOP title with routes, or None if content is bug-fix
    """
    combined = f"{task} {details or ''}"

    # === BOUNDARY CHECK: Only process process SOPs ===
    sop_type = detect_sop_type(combined)
    if sop_type == 'bugfix':
        return None

    # === GENERATE GOAL HEART ===
    goal_heart = generate_goal_heart(task)

    # === EXTRACT CHAIN ZONES ===
    zone_parts = extract_process_zones(combined)

    # === CHECK FOR EXISTING ROUTES ===
    sop_content = None
    if include_routes:
        try:
            from memory.route_tracker import format_sop_with_routes, get_sop_entry
            sop = get_sop_entry(goal_heart)
            if sop and sop["routes"]:
                # Has existing routes - return multi-route format
                sop_content = format_sop_with_routes(goal_heart, zone_parts)
        except ImportError:
            # route_tracker not available - fall through to single-line
            pass

    # === SINGLE-LINE FORMAT (no routes yet) ===
    if sop_content is None:
        if zone_parts:
            title = f"{goal_heart}: {zone_parts}"
        else:
            title = goal_heart

        if title:
            title = title[0].upper() + title[1:]

        sop_content = f"[process SOP] {title}"[:180]

    # === APPEND NEGATIVE PATTERNS (3+ occurrences only) ===
    if include_negative:
        negative_patterns = get_negative_patterns_for_goal(goal_heart)
        negative_section = format_negative_patterns_section(negative_patterns)
        if negative_section:
            sop_content = f"{sop_content}\n{negative_section}"

    return sop_content


def record_process_route(
    goal: str,
    route_description: str,
    chain: str = "",
    is_success: bool = True,
    failure_note: str = "",
    is_first_try: bool = True
) -> str:
    """
    Record a route attempt for a process SOP.

    This is the interface for brain.py to record routes.

    Args:
        goal: The SOP goal (e.g., "Deploy Django to production")
        route_description: Brief description of the route attempted
        chain: Full chain format if known
        is_success: Whether the route succeeded
        failure_note: What went wrong (if failed)
        is_first_try: Whether this was a first attempt at this route

    Returns:
        Formatted SOP with updated routes
    """
    try:
        from memory.route_tracker import (
            record_route_success,
            record_route_failure,
            format_sop_with_routes
        )

        if is_success:
            record_route_success(goal, route_description, chain, is_first_try)
        else:
            record_route_failure(goal, route_description, failure_note, chain)

        return format_sop_with_routes(goal, chain)
    except ImportError:
        # Fall back to simple format
        status = "passed" if is_success else "failed"
        return f"[process SOP] {goal}: ({status}) {route_description}"


# =============================================================================
# ROUTE-AWARE SOP GENERATION (ADDITIVE)
# =============================================================================

def get_most_preferred_route(goal: str) -> Optional[Dict]:
    """
    Get the most preferred/recent successful route for a process SOP.

    Selection priority:
    1. Highest preference score (first_try_success_rate * 0.6 + overall_rate * 0.4)
    2. On tie: most recent success (recency matters for staying current)

    Args:
        goal: The SOP goal to look up

    Returns:
        Best route dict or None if no successful routes exist
    """
    try:
        from memory.route_tracker import (
            get_sop_entry,
            sort_routes_by_preference,
            calculate_route_score
        )

        sop = get_sop_entry(goal)
        if not sop or not sop["routes"]:
            return None

        successful, _ = sort_routes_by_preference(sop["routes"])
        if not successful:
            return None

        # Already sorted by preference - first is best
        # But check for ties and prefer more recent
        best = successful[0]
        best_score = calculate_route_score(best)

        for route in successful[1:]:
            route_score = calculate_route_score(route)
            if route_score < best_score - 1:  # Clear winner
                break
            # Similar score - prefer more recent
            if route.get("last_success", "") > best.get("last_success", ""):
                best = route
                best_score = route_score

        return best

    except ImportError:
        return None


def get_all_successful_tools(goal: str) -> List[str]:
    """
    Get all unique tools from successful routes for Zone 2 via listing.

    Extracts tool names from route descriptions/chains and returns
    deduplicated list ordered by route preference.

    Args:
        goal: The SOP goal to look up

    Returns:
        List of tool names (e.g., ['systemctl', 'docker', 'ansible'])
    """
    try:
        from memory.route_tracker import get_sop_entry, sort_routes_by_preference

        sop = get_sop_entry(goal)
        if not sop or not sop["routes"]:
            return []

        successful, _ = sort_routes_by_preference(sop["routes"])

        # Common tools vocabulary for extraction
        tool_vocab = {
            'systemctl': 'systemctl', 'systemd': 'systemctl',
            'docker': 'docker', 'docker-compose': 'docker-compose', 'compose': 'docker-compose',
            'ansible': 'ansible', 'terraform': 'terraform',
            'kubectl': 'kubectl', 'k8s': 'kubectl', 'kubernetes': 'kubectl',
            'ssm': 'SSM', 'aws ssm': 'SSM',
            'ecs': 'ECS', 'lambda': 'Lambda',
            'ec2': 'EC2', 'boto3': 'boto3',
            'ssh': 'SSH', 'api': 'API', 'cli': 'CLI',
            'script': 'script', 'cron': 'cron', 'hook': 'hook',
            'git': 'git', 'npm': 'npm', 'pip': 'pip',
            'make': 'make', 'bash': 'bash', 'python': 'python',
            'gunicorn': 'gunicorn', 'nginx': 'nginx', 'redis': 'redis',
            'postgres': 'postgres', 'rabbitmq': 'rabbitmq',
        }

        tools = []
        seen = set()

        for route in successful:
            # Check description and chain for tools
            text = f"{route.get('description', '')} {route.get('chain', '')}".lower()

            for raw, normalized in tool_vocab.items():
                if re.search(r'\b' + re.escape(raw) + r'\b', text):
                    if normalized not in seen:
                        tools.append(normalized)
                        seen.add(normalized)

        return tools

    except ImportError:
        return []


def extract_chain_from_route(route: Dict) -> str:
    """
    Extract the chain steps (zones 3-6) from a route.

    Takes the chain field and removes the 'via (...)' prefix to get
    just the steps: step1 → step2 → ... → ✓ verification

    Args:
        route: Route dict with 'chain' field

    Returns:
        Chain steps without via prefix, or empty string
    """
    chain = route.get("chain", "")
    if not chain:
        return ""

    # Remove "via (...)" prefix if present
    via_match = re.match(r'^via\s*\([^)]+\)\s*→?\s*', chain, re.IGNORECASE)
    if via_match:
        chain = chain[via_match.end():]

    return chain.strip()


def generate_route_aware_process_sop(task: str, details: str = None) -> Optional[str]:
    """
    Generate process SOP title using route tracking data.

    This is the ROUTE-AWARE version that:
    1. Finds the most preferred/recent successful route
    2. Uses that route's chain for zones 3-6
    3. Includes ALL successful route tools in Zone 2 via (tool1, tool2, tool3)

    PHILOSOPHY: The enhancer learns which routes work best and presents
    the most reliable path while documenting all available alternatives.

    Args:
        task: Original task description
        details: Additional details for zone extraction

    Returns:
        Route-aware process SOP title, or None if content is bug-fix
    """
    combined = f"{task} {details or ''}"

    # === BOUNDARY CHECK: Only process process SOPs ===
    sop_type = detect_sop_type(combined)
    if sop_type == 'bugfix':
        return None

    # === GENERATE GOAL HEART ===
    goal_heart = generate_goal_heart(task)

    # === GET ROUTE DATA ===
    preferred_route = get_most_preferred_route(goal_heart)
    all_tools = get_all_successful_tools(goal_heart)

    # === BUILD CHAIN ===
    parts = []

    # Zone 2: Via with ALL successful tools
    if all_tools:
        parts.append(f"via ({', '.join(all_tools)})")
    else:
        # Fall back to extracting from combined text
        zone_parts = extract_process_zones(combined)
        if zone_parts.startswith("via"):
            # Extract just the via portion
            via_end = zone_parts.find("→")
            if via_end > 0:
                parts.append(zone_parts[:via_end].strip())
            else:
                parts.append(zone_parts)

    # Zones 3-6: Steps from preferred route OR extracted from combined
    if preferred_route:
        route_chain = extract_chain_from_route(preferred_route)
        if route_chain:
            # Add the chain steps
            parts.append(route_chain)
    else:
        # Fall back to extraction from combined text
        zone_parts = extract_process_zones(combined)
        if "→" in zone_parts:
            # Find where steps start (after via)
            via_end = zone_parts.find("→")
            if via_end > 0:
                steps = zone_parts[via_end+1:].strip()
                if steps:
                    parts.append(steps)

    # === BUILD FINAL TITLE ===
    if parts:
        chain_str = " → ".join(parts)
        title = f"{goal_heart}: {chain_str}"
    else:
        title = goal_heart

    # Capitalize first letter
    if title:
        title = title[0].upper() + title[1:]

    # Add process SOP tag
    title = f"[process SOP] {title}"

    return title[:180]


def get_process_sop_summary(goal: str) -> str:
    """
    Get a summary of route tracking data for a process SOP.

    Returns human-readable summary of:
    - Number of tracked routes
    - Best route and its score
    - Available tools across all routes

    Args:
        goal: The SOP goal to summarize

    Returns:
        Summary string
    """
    try:
        from memory.route_tracker import (
            get_sop_entry,
            sort_routes_by_preference,
            calculate_route_score,
            format_date_short
        )

        sop = get_sop_entry(goal)
        if not sop or not sop["routes"]:
            return f"No routes tracked for: {goal}"

        successful, failed = sort_routes_by_preference(sop["routes"])
        all_tools = get_all_successful_tools(goal)

        lines = [f"Process SOP: {goal}"]
        lines.append(f"  Routes: {len(successful)} successful, {len(failed)} failed")

        if all_tools:
            lines.append(f"  Available tools: {', '.join(all_tools)}")

        if successful:
            best = successful[0]
            score = calculate_route_score(best)
            date_str = format_date_short(best.get("last_success", ""))
            lines.append(f"  Best route ({score:.0f}%): {best['description']}")
            if date_str:
                lines.append(f"    Last success: {date_str}")

        return "\n".join(lines)

    except ImportError:
        return f"Route tracker not available for: {goal}"


# =============================================================================
# CLI INTERFACE
# =============================================================================

def main():
    """CLI for testing process SOP enhancer."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python process_sop_enhancer.py <command> [args]")
        print("")
        print("Commands:")
        print("  test                     Run test cases")
        print("  enhance <task> [details] Generate process SOP title")
        print("  detect <text>            Detect SOP type")
        print("  route-aware <task>       Generate route-aware SOP (uses tracking)")
        print("  summary <goal>           Show route summary for a goal")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "test":
        print("=== PROCESS SOP ENHANCER TESTS ===\n")

        # Test cases: (task, details, expected_type)
        test_cases = [
            # Process SOPs (should work)
            ("Deploy Django to production", "Used systemctl restart gunicorn", "process"),
            ("Setup ECS cluster", "terraform apply with auto-scaling", "process"),
            ("Configure CloudWatch alarms", "Added CPU and memory alerts", "process"),
            ("Backup database before migration", "pg_dump to S3", "process"),
            ("Install new npm packages", "npm install axios lodash", "process"),

            # Bug-fix SOPs (should return None)
            ("Fixed async blocking in LLM service", "Used asyncio.to_thread", "bugfix"),
            ("Container crash on startup", "Missing HOME env var", "bugfix"),
            ("Timeout errors in WebSocket", "NLB idle timeout was 60s", "bugfix"),
        ]

        for task, details, expected_type in test_cases:
            combined = f"{task} {details}"
            actual_type = detect_sop_type(combined)
            title = generate_process_sop_title(task, details)

            type_match = "✓" if actual_type == expected_type else "✗"
            print(f"{type_match} Type: {actual_type} (expected: {expected_type})")
            print(f"  Task: {task}")
            if title:
                print(f"  → {title}")
            else:
                print(f"  → None (correctly rejected)")
            print()

    elif cmd == "enhance":
        if len(sys.argv) < 3:
            print("Usage: python process_sop_enhancer.py enhance <task> [details]")
            sys.exit(1)

        task = sys.argv[2]
        details = sys.argv[3] if len(sys.argv) > 3 else None

        title = generate_process_sop_title(task, details)
        if title:
            print(title)
        else:
            print("None (not a process SOP - use bugfix_sop_enhancer.py)")

    elif cmd == "detect":
        if len(sys.argv) < 3:
            print("Usage: python process_sop_enhancer.py detect <text>")
            sys.exit(1)

        text = ' '.join(sys.argv[2:])
        sop_type = detect_sop_type(text)
        print(f"SOP Type: {sop_type}")

    elif cmd == "route-aware":
        if len(sys.argv) < 3:
            print("Usage: python process_sop_enhancer.py route-aware <task> [details]")
            sys.exit(1)

        task = sys.argv[2]
        details = sys.argv[3] if len(sys.argv) > 3 else None

        title = generate_route_aware_process_sop(task, details)
        if title:
            print(title)
        else:
            print("None (not a process SOP - use bugfix_sop_enhancer.py)")

    elif cmd == "summary":
        if len(sys.argv) < 3:
            print("Usage: python process_sop_enhancer.py summary <goal>")
            sys.exit(1)

        goal = sys.argv[2]
        summary = get_process_sop_summary(goal)
        print(summary)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
