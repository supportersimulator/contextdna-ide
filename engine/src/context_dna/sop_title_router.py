"""
SOP TITLE ROUTER - Study First, Then Route

This module coordinates SOP title generation by:
1. STUDYING the content to determine SOP type (bug-fix vs process)
2. DETECTING if title is already formatted (enhance path) or raw (create path)
3. ROUTING to the appropriate creator or enhancer

PHILOSOPHY: Study before acting. Know what you're dealing with before choosing the tool.

TYPE DETECTION uses multi-signal scoring:
- Keyword weights (strong=3, medium=2, weak=1)
- Contextual patterns (sentence structure analysis)
- POTENTIAL LENGTH signals (growth vs finality):
  * Growth signals → process (will accumulate routes over time)
    "another way", "alternative", "preferred method", "depends on"
  * Finality signals → bugfix (single solution, done)
    "the fix was", "root cause was", "finally fixed", "culprit was"

WHY POTENTIAL LENGTH MATTERS:
- Bug-fix SOPs: Convergent → one problem, one fix, DONE
- Process SOPs: Divergent → one goal, many routes, GROWS over time
Content implying future growth routes to process; content implying
single-answer finality routes to bugfix.

Flow:
    Content → Study → Detect Format → Route → Creator or Enhancer

Usage:
    from memory.sop_title_router import generate_sop_title

    # Raw content - will create fresh title
    title = generate_sop_title("Fixed timeout in API", "Added retry logic")
    # → "[bug-fix SOP] Fixed timeout in API: timeout → add retry → responsive"

    # Already formatted - will enhance if needed
    title = generate_sop_title("[process SOP] Deploy app", "Used terraform")
    # → "[process SOP] Deploy app: via (terraform) → deploy" (enhanced)

Creators vs Enhancers:
    - Creator: Raw task → Full structured title (always generates)
    - Enhancer: Existing title → Improved if needed (may pass through)
"""

import re
from typing import Optional, Tuple

# =============================================================================
# STUDY: Determine SOP Type
# =============================================================================

def study_sop_type(content: str) -> str:
    """
    Study content to determine if it describes a bug-fix or process.

    This is THE decision point - all routing flows from this.

    Uses SCORING approach with multiple signal types:
    1. Keyword weights (strong=3, medium=2, weak=1)
    2. Contextual patterns (sentence structure, regex)
    3. Potential length signals (growth → process, finality → bugfix)

    Higher score wins. Ties go to bugfix (safer to track problems).

    Returns:
        'bugfix' or 'process'
    """
    content_lower = content.lower()

    # === SCORE BUGFIX INDICATORS ===
    bugfix_score = 0

    # Strong bugfix signals (weight 3)
    strong_bugfix = ['crash', 'crashed', 'error', 'errors', 'exception', 'traceback', 'broken']
    for word in strong_bugfix:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            bugfix_score += 3

    # Medium bugfix signals (weight 2)
    medium_bugfix = ['fix', 'fixed', 'fail', 'failed', 'failing', 'bug', 'issue', 'wrong', 'timeout', 'hang', 'hung']
    for word in medium_bugfix:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            bugfix_score += 2

    # Weak bugfix signals (weight 1)
    weak_bugfix = ['missing', 'slow', 'stuck', 'latency', 'block', 'freeze']
    for word in weak_bugfix:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            bugfix_score += 1

    # HTTP error codes (weight 2)
    http_errors = ['500', '502', '503', '504', '400', '401', '403', '404']
    for code in http_errors:
        if code in content_lower:
            bugfix_score += 2

    # === SCORE PROCESS INDICATORS ===
    process_score = 0

    # Strong process signals (weight 3)
    strong_process = ['deploy', 'install', 'setup', 'configure', 'migrate']
    for word in strong_process:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            process_score += 3

    # Medium process signals (weight 2)
    medium_process = ['backup', 'restore', 'create', 'build', 'update', 'upgrade']
    for word in medium_process:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            process_score += 2

    # Weak process signals (weight 1)
    weak_process = ['start', 'stop', 'restart', 'enable', 'disable', 'scale']
    for word in weak_process:
        if re.search(r'\b' + re.escape(word) + r'\b', content_lower):
            process_score += 1

    # === CONTEXTUAL PATTERNS (weight 2 each) ===
    # These look at sentence structure, not just keywords

    # Bugfix contextual patterns
    bugfix_patterns = [
        r'\b(why did|why does|why is)\b',           # Diagnostic questions
        r'\b(caused by|due to|because of)\b',       # Causality (root cause)
        r'\b(doesn\'t|didn\'t|won\'t|can\'t)\b',    # Negation (something wrong)
        r'\b(after|when|whenever).*\b(crash|fail|error|break)',  # Temporal failure
        r'\b(should have|was supposed to)\b',       # Expectation violation
        r'\b(instead of|rather than)\b',            # Wrong behavior
        r'\bno longer\b',                           # Regression
        r'\b(deploy|setup|install|build|start|restart)\s+(fail|crash|error)',  # Process + failure
        r'\b(fail|crash|error)\s+(during|after|on)\b',  # Failure timing
    ]
    for pattern in bugfix_patterns:
        if re.search(pattern, content_lower):
            bugfix_score += 2

    # Process contextual patterns
    process_patterns = [
        r'\b(how to|how do|steps to|guide for)\b',  # Procedural questions
        r'\b(in order to|so that|to enable)\b',     # Goal-oriented
        r'\b(first|then|next|finally)\b',           # Sequential steps
        r'\b(using|via|with|through)\b.*\b(tool|script|command)',  # Tool usage
        r'\b(set up|spin up|stand up)\b',           # Infrastructure verbs
        r'\b(new|add|create|implement)\b.*\b(feature|service|endpoint)',  # Creation
    ]
    for pattern in process_patterns:
        if re.search(pattern, content_lower):
            process_score += 2

    # === POTENTIAL LENGTH / GROWTH SIGNALS (weight 2) ===
    # Process SOPs are living documents that grow; bugfix SOPs are point-in-time

    # Growth potential signals → likely process (will accumulate routes)
    growth_patterns = [
        r'\b(another way|alternative|also works)\b',    # Multiple routes implied
        r'\b(can also|or you can|option)\b',            # Alternatives exist
        r'\b(best practice|recommended|preferred)\b',   # Preferences among routes
        r'\b(depends on|choose|select)\b',              # Decision points
        r'\b(multiple|several|various)\s+(ways|methods|approaches)',  # Explicit multiplicity
    ]
    for pattern in growth_patterns:
        if re.search(pattern, content_lower):
            process_score += 2

    # Finality signals → likely bugfix (single solution, done)
    finality_patterns = [
        r'\b(the fix|the solution|this resolved)\b',    # Single answer found
        r'\b(root cause was|actually was)\b',           # One underlying problem
        r'\b(finally fixed|solved by|fixed by)\b',      # Search is over
        r'\b(culprit was|turned out to be)\b',          # Single root cause identified
        r'\b(only|just)\s+(needed|required|had to)\b',  # Single action needed
    ]
    for pattern in finality_patterns:
        if re.search(pattern, content_lower):
            bugfix_score += 2

    # === DECIDE ===
    # Higher score wins, ties go to bugfix (safer to track problems)
    return 'bugfix' if bugfix_score >= process_score else 'process'


# =============================================================================
# DETECT: Check if Already Formatted
# =============================================================================

def detect_format_state(task: str) -> str:
    """
    Detect if the task/title is already formatted or needs creation.

    Returns:
        'raw' - Needs fresh title creation
        'partial' - Has some structure, may need enhancement
        'complete' - Fully formatted, should pass through
    """
    # Check for complete formatting (tag + zones)
    has_tag = task.startswith('[bug-fix SOP]') or task.startswith('[process SOP]')
    has_arrows = '→' in task
    has_zones = ':' in task and ('→' in task or '(' in task.split(':')[-1])

    if has_tag and has_arrows and has_zones:
        return 'complete'  # Fully formatted
    elif has_tag or has_arrows:
        return 'partial'   # Has some structure
    else:
        return 'raw'       # Needs creation


# =============================================================================
# ROUTE: Dispatch to Appropriate Handler
# =============================================================================

def generate_sop_title(task: str, details: str = None) -> str:
    """
    Main entry point - studies content, then routes to appropriate handler.

    Flow:
    1. STUDY: Analyze content to determine type (bug-fix vs process)
    2. DETECT: Check format state (raw, partial, complete)
    3. ROUTE: Call appropriate creator or enhancer

    Args:
        task: Task description or existing title
        details: Additional context for zone extraction

    Returns:
        Formatted SOP title with appropriate tag and zones
    """
    combined = f"{task} {details or ''}"

    # === STEP 1: STUDY - Determine SOP type ===
    sop_type = study_sop_type(combined)

    # === STEP 2: DETECT - Check format state ===
    format_state = detect_format_state(task)

    # === STEP 3: ROUTE - Dispatch to appropriate handler ===
    if sop_type == 'bugfix':
        if format_state == 'complete':
            # Already complete - pass through
            return task
        elif format_state == 'partial':
            # Needs enhancement
            return enhance_bugfix_title(task, details)
        else:
            # Raw - needs creation
            return create_bugfix_title(task, details)
    else:
        if format_state == 'complete':
            # Already complete - pass through
            return task
        elif format_state == 'partial':
            # Needs enhancement
            return enhance_process_title(task, details)
        else:
            # Raw - needs creation
            return create_process_title(task, details)


# =============================================================================
# BUG-FIX TITLE FUNCTIONS
# =============================================================================

def create_bugfix_title(task: str, details: str = None) -> str:
    """
    Create a fresh bug-fix SOP title from raw content.

    Always generates full 6-zone structure:
    [bug-fix SOP] [HEART]: bad_sign (antecedent) → fix (stack) → outcome

    This is the CREATOR - assumes raw input, generates complete title.
    """
    # Import here to avoid circular imports
    try:
        from memory.bugfix_sop_enhancer import extract_bugfix_zones
    except ImportError:
        from bugfix_sop_enhancer import extract_bugfix_zones

    combined = f"{task} {details or ''}"

    # Generate heart (the descriptive core)
    # For bug-fix, we use multi-candidate scoring
    heart = _generate_bugfix_heart(task, details)

    # Extract zones (bad_sign, antecedent, fix, stack, outcome)
    zones = extract_bugfix_zones(combined)

    # Combine
    if zones:
        title = f"{heart}: {zones}"
    else:
        title = heart

    # Capitalize and add tag
    if title:
        title = title[0].upper() + title[1:]

    return f"[bug-fix SOP] {title}"


def enhance_bugfix_title(task: str, details: str = None) -> str:
    """
    Enhance an existing bug-fix title if needed.

    This is the ENHANCER - checks if improvement needed, may pass through.

    Only enhances if:
    - Has tag but missing zones
    - Has partial structure that can be improved

    Passes through if already complete.
    """
    try:
        from memory.bugfix_sop_enhancer import extract_bugfix_zones
    except ImportError:
        from bugfix_sop_enhancer import extract_bugfix_zones

    combined = f"{task} {details or ''}"

    # If already has arrows and zones, pass through
    if '→' in task and ':' in task:
        return task

    # Extract existing heart (before colon if present)
    if ':' in task:
        heart = task.split(':')[0].strip()
        # Remove tag if present for clean heart
        heart = heart.replace('[bug-fix SOP]', '').strip()
    else:
        heart = task.replace('[bug-fix SOP]', '').strip()

    # Extract zones from combined content
    zones = extract_bugfix_zones(combined)

    # Combine
    if zones:
        title = f"{heart}: {zones}"
    else:
        title = heart

    # Capitalize
    if title:
        title = title[0].upper() + title[1:]

    # Ensure tag
    if not title.startswith('[bug-fix SOP]'):
        title = f"[bug-fix SOP] {title}"

    return title


def _generate_bugfix_heart(task: str, details: str = None) -> str:
    """
    Generate the heart (core title) for a bug-fix SOP.

    Bug-fix hearts capture the PROBLEM essence.
    Uses multi-candidate scoring for best result.
    """
    # For now, use simple extraction - can be expanded
    # Remove common filler and preserve the problem description

    filler = {'the', 'a', 'an', 'is', 'was', 'were', 'been', 'have', 'has',
              'successfully', 'completed', 'remember', 'fixed', 'resolved'}

    words = task.split()
    meaningful = [w for w in words if w.lower() not in filler]

    if meaningful:
        heart = ' '.join(meaningful[:8])  # Max 8 words
    else:
        heart = task[:60]

    return heart.strip()


# =============================================================================
# PROCESS TITLE FUNCTIONS
# =============================================================================

def create_process_title(task: str, details: str = None) -> str:
    """
    Create a fresh process SOP title from raw content.

    Always generates chain structure:
    [process SOP] [GOAL]: via (tools) → step1 → step2 → ✓ verification

    This is the CREATOR - assumes raw input, generates complete title.
    """
    try:
        from memory.process_sop_enhancer import (
            generate_goal_heart, extract_process_zones
        )
    except ImportError:
        from process_sop_enhancer import (
            generate_goal_heart, extract_process_zones
        )

    combined = f"{task} {details or ''}"

    # Generate heart (the goal)
    heart = generate_goal_heart(task)

    # Extract zones (via, steps, verification)
    zones = extract_process_zones(combined)

    # Combine
    if zones:
        title = f"{heart}: {zones}"
    else:
        title = heart

    # Capitalize and add tag
    if title:
        title = title[0].upper() + title[1:]

    return f"[process SOP] {title}"


def enhance_process_title(task: str, details: str = None) -> str:
    """
    Enhance an existing process title if needed.

    This is the ENHANCER - checks if improvement needed, may pass through.

    Only enhances if:
    - Has tag but missing zones
    - Has partial structure that can be improved

    Passes through if already complete.
    """
    try:
        from memory.process_sop_enhancer import extract_process_zones
    except ImportError:
        from process_sop_enhancer import extract_process_zones

    combined = f"{task} {details or ''}"

    # If already has arrows and zones, pass through
    if '→' in task and ':' in task:
        return task

    # Extract existing heart (before colon if present)
    if ':' in task:
        heart = task.split(':')[0].strip()
        # Remove tag if present for clean heart
        heart = heart.replace('[process SOP]', '').strip()
    else:
        heart = task.replace('[process SOP]', '').strip()

    # Extract zones from combined content
    zones = extract_process_zones(combined)

    # Combine
    if zones:
        title = f"{heart}: {zones}"
    else:
        title = heart

    # Capitalize
    if title:
        title = title[0].upper() + title[1:]

    # Ensure tag
    if not title.startswith('[process SOP]'):
        title = f"[process SOP] {title}"

    return title


# =============================================================================
# CLI
# =============================================================================

def main():
    """CLI for testing the router."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sop_title_router.py <command> [args]")
        print("")
        print("Commands:")
        print("  study <text>           Study content to determine SOP type")
        print("  detect <text>          Detect format state of title")
        print("  generate <task> [details]  Generate SOP title (smart routing)")
        print("  test                   Run test cases")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "study":
        if len(sys.argv) < 3:
            print("Usage: python sop_title_router.py study <text>")
            sys.exit(1)
        text = ' '.join(sys.argv[2:])
        sop_type = study_sop_type(text)
        print(f"SOP Type: {sop_type}")

    elif cmd == "detect":
        if len(sys.argv) < 3:
            print("Usage: python sop_title_router.py detect <text>")
            sys.exit(1)
        text = ' '.join(sys.argv[2:])
        state = detect_format_state(text)
        print(f"Format State: {state}")

    elif cmd == "generate":
        if len(sys.argv) < 3:
            print("Usage: python sop_title_router.py generate <task> [details]")
            sys.exit(1)
        task = sys.argv[2]
        details = sys.argv[3] if len(sys.argv) > 3 else None
        title = generate_sop_title(task, details)
        print(title)

    elif cmd == "test":
        print("=== SOP TITLE ROUTER TESTS ===\n")

        test_cases = [
            # Raw bug-fix (should CREATE)
            ("Fixed timeout error", "Added retry logic", "bugfix", "raw"),
            ("Container crash on startup", "Missing HOME env", "bugfix", "raw"),

            # Raw process (should CREATE)
            ("Deploy Django to production", "systemctl restart", "process", "raw"),
            ("Backup database", "pg_dump to S3", "process", "raw"),

            # Partial bug-fix (should ENHANCE)
            ("[bug-fix SOP] Fixed timeout", "Added retry", "bugfix", "partial"),

            # Partial process (should ENHANCE)
            ("[process SOP] Deploy app", "terraform apply", "process", "partial"),

            # Complete (should PASS THROUGH)
            ("[bug-fix SOP] Fixed timeout: error → retry → working", None, "bugfix", "complete"),
            ("[process SOP] Deploy: via (terraform) → deploy → ✓ healthy", None, "process", "complete"),
        ]

        for task, details, expected_type, expected_state in test_cases:
            combined = f"{task} {details or ''}"
            actual_type = study_sop_type(combined)
            actual_state = detect_format_state(task)

            type_ok = "✓" if actual_type == expected_type else "✗"
            state_ok = "✓" if actual_state == expected_state else "✗"

            print(f"{type_ok} Type: {actual_type} (expected: {expected_type})")
            print(f"{state_ok} State: {actual_state} (expected: {expected_state})")

            title = generate_sop_title(task, details)
            print(f"  Input: {task[:50]}...")
            print(f"  Output: {title[:70]}...")
            print()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
