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
Current implementation: Single chain. Future: Multi-route accumulation.

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
from typing import Optional, Tuple, List, Set

# =============================================================================
# SOP TYPE DETECTION
# =============================================================================

def detect_sop_type(combined: str) -> str:
    """
    Detect whether content describes a bug-fix or process SOP.

    Uses multi-signal detection including POTENTIAL LENGTH signals:
    - Growth signals → process (will accumulate routes over time)
    - Finality signals → bugfix (single solution, done)

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

    # === POTENTIAL LENGTH SIGNALS (can override keyword detection) ===
    # Growth signals → likely process (will accumulate routes)
    growth_signals = [
        r'\b(another way|alternative|also works)\b',
        r'\b(can also|or you can|option)\b',
        r'\b(best practice|recommended|preferred)\b',
        r'\b(depends on|choose|select)\b',
        r'\b(multiple|several|various)\s+(ways|methods|approaches)\b',
    ]
    has_growth = any(re.search(p, combined_lower) for p in growth_signals)

    # Finality signals → likely bugfix (single solution, done)
    finality_signals = [
        r'\b(the fix|the solution|this resolved)\b',
        r'\b(root cause was|actually was)\b',
        r'\b(finally fixed|solved by|fixed by)\b',
        r'\b(culprit was|turned out to be)\b',
        r'\b(only|just)\s+(needed|required|had to)\b',
    ]
    has_finality = any(re.search(p, combined_lower) for p in finality_signals)

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
# CLI INTERFACE
# =============================================================================

def main():
    """CLI for testing process SOP enhancer."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python process_sop_enhancer.py <command> [args]")
        print("")
        print("Commands:")
        print("  test                    Run test cases")
        print("  enhance <task> [details] Generate process SOP title")
        print("  detect <text>           Detect SOP type")
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

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
