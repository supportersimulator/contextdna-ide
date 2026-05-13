#!/usr/bin/env python3
"""
SOP ENHANCER - Smart Architecture Enhancement & Deduplication

This module bridges the gap between raw capture and quality architecture:

1. KEY INSIGHT EXTRACTION - Parses details to extract the "one thing that worked"
2. SMART DEDUPLICATION - Finds duplicates and merges to single source of truth
3. COMPREHENSIVE YET CONCISE - Combines best from duplicates, removes redundancy
4. AUTO-INTEGRATION - Runs automatically in capture pipeline

PHILOSOPHY:
Raw captures are noisy (10 failures, 1 success, many duplicates).
Enhanced architecture is signal (the key insights, deduplicated, actionable).

Usage:
    from memory.sop_enhancer import enhance_capture, run_dedup_cycle

    # Enhance a single capture
    enhanced = enhance_capture(task="Did X", details="Used Y approach because Z")

    # Run dedup cycle on all SOPs
    run_dedup_cycle()

CLI:
    python memory/sop_enhancer.py enhance "task" "details"
    python memory/sop_enhancer.py dedup
    python memory/sop_enhancer.py report
    python memory/sop_enhancer.py fix-all
"""

import sys
import json
import hashlib
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from difflib import SequenceMatcher

sys.path.insert(0, str(Path(__file__).parent.parent))

# Database paths
ENHANCER_DB = Path(__file__).parent / ".sop_enhancer.db"

try:
    import sqlite3
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

# Import process enhancer for fallback when content is not bug-fix
try:
    from memory.process_sop_enhancer import generate_process_sop_title
    PROCESS_ENHANCER_AVAILABLE = True
except ImportError:
    PROCESS_ENHANCER_AVAILABLE = False


# =============================================================================
# KEY INSIGHT EXTRACTION
# =============================================================================

# Patterns that indicate key insights in text
INSIGHT_PATTERNS = [
    r"(?:the key|key insight|the trick|what worked|solution was|worked because)\s*(?:is|was|:)?\s*(.{20,100})",
    r"(?:used|using|by)\s+(.{15,80})\s+(?:which|that|to)\s+(?:worked|solved|fixed)",
    r"(?:remember to|always|never|important:)\s*(.{20,100})",
    r"(?:the one thing|single most important)\s*(?:is|was|:)?\s*(.{20,100})",
]

# Stopwords for title extraction - AGGRESSIVE filtering
# Only keep words that help FIND or UNDERSTAND the SOP
TITLE_STOPWORDS = {
    # Basic stopwords
    'the', 'a', 'an', 'is', 'was', 'are', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'this', 'that',
    'these', 'those', 'i', 'we', 'you', 'he', 'she', 'it', 'they', 'my', 'our',
    'your', 'his', 'her', 'its', 'their', 'what', 'which', 'who', 'whom',
    'and', 'but', 'or', 'nor', 'for', 'yet', 'so', 'if', 'then', 'else',
    'when', 'where', 'why', 'how', 'all', 'each', 'every', 'both', 'few',
    'more', 'most', 'other', 'some', 'such', 'no', 'not', 'only', 'own',
    'same', 'than', 'too', 'very', 'just', 'also', 'now', 'here', 'there',
    # FILLER words that add NO signal
    'successfully', 'completed', 'done', 'finished', 'worked', 'working',
    'remember', 'apply', 'approach', 'similar', 'future', 'tasks', 'task',
    'always', 'never', 'important', 'note', 'ensure', 'make', 'sure',
    'quick', 'clear', 'good', 'best', 'better', 'great', 'nice', 'simple',
    'easy', 'basic', 'standard', 'normal', 'typical', 'usual', 'common',
    'way', 'method', 'process', 'step', 'steps', 'thing', 'things', 'stuff',
    'like', 'want', 'wanted', 'needs', 'needed', 'requires', 'required',
    'using', 'used', 'uses', 'with', 'without', 'from', 'into', 'onto',
    'about', 'after', 'before', 'during', 'through', 'between', 'under',
    'over', 'above', 'below', 'again', 'further', 'once', 'twice',
    'still', 'already', 'even', 'really', 'actually', 'basically',
    'simply', 'just', 'well', 'much', 'many', 'any', 'anything',
    'something', 'everything', 'nothing', 'anyone', 'someone', 'everyone',
    'please', 'thanks', 'thank', 'okay', 'fine', 'right', 'correct',
    'verification', 'verifying', 'verify', 'verified',
    # Action words that don't tell you WHAT was done
    'calls', 'call', 'calling', 'called',
    'added', 'add', 'adding', 'configured', 'configure', 'configuring',
    'automatic', 'automatically', 'manual', 'manually',
    'first', 'second', 'third', 'last', 'next', 'previous',
    'new', 'old', 'current', 'default', 'existing',
    'service', 'services',  # Too generic
    # Evaluation/outcome words (don't describe the solution)
    'successful', 'success', 'fail', 'failure', 'pass', 'passed',
    'healthy', 'unhealthy', 'ready', 'alive', 'dead',
    'correct', 'incorrect', 'proper', 'properly', 'improper',
    'expected', 'unexpected', 'valid', 'invalid',
    # Meta-commentary (describes the SOP, not the solution)
    'ensure', 'ensures', 'ensuring', 'ensured',
    'prefer', 'prefers', 'preferred', 'preferences',
    'remember', 'recall', 'note', 'notes', 'noted',
    'repeat', 'repeatable', 'reusable', 'applicable',
    'future', 'later', 'next', 'previous', 'past',
    'similar', 'same', 'different', 'other', 'another',
    'task', 'tasks', 'work', 'works', 'job', 'jobs',
    'situation', 'situations', 'case', 'cases', 'scenario', 'scenarios',
    'host', 'hosts',  # Too generic unless part of hostname
}


def extract_key_insight(task: str, details: str = None) -> str:
    """
    Extract the key insight from task description and details.

    The key insight is the "one thing that worked" - the specific
    technique, approach, or discovery that made the task succeed.

    Args:
        task: What was accomplished
        details: How it was done / additional context

    Returns:
        Extracted key insight string
    """
    if not details:
        return task  # Fall back to task as insight

    combined = f"{task} {details}"

    # Try pattern matching first
    for pattern in INSIGHT_PATTERNS:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            insight = match.group(1).strip()
            # Clean up
            insight = re.sub(r'^[:\-\s]+', '', insight)
            insight = re.sub(r'[:\-\s]+$', '', insight)
            if len(insight) >= 15:
                return insight

    # Fall back to extracting most specific sentence
    sentences = re.split(r'[.!?]\s+', details)
    if sentences:
        # Pick sentence with most specific/technical content
        best_sentence = max(sentences, key=lambda s: _specificity_score(s))
        if len(best_sentence) >= 15:
            return best_sentence.strip()

    # Last resort: first 100 chars of details
    return details[:100].strip()


def _specificity_score(text: str) -> int:
    """Score how specific/technical a sentence is."""
    score = 0
    text_lower = text.lower()

    # Reward technical terms
    technical_terms = [
        'command', 'function', 'method', 'class', 'file', 'path', 'config',
        'docker', 'container', 'service', 'api', 'endpoint', 'database',
        'query', 'script', 'package', 'module', 'import', 'export',
        'port', 'host', 'server', 'client', 'request', 'response',
    ]
    for term in technical_terms:
        if term in text_lower:
            score += 2

    # Reward specific patterns
    if re.search(r'\b[A-Z][a-z]+[A-Z]', text):  # CamelCase
        score += 3
    if re.search(r'_[a-z]+', text):  # snake_case
        score += 3
    if re.search(r'/[a-z]+', text):  # Paths
        score += 2
    if re.search(r'\d+', text):  # Numbers (ports, versions)
        score += 1

    # Penalize generic phrases
    generic_phrases = ['successfully', 'worked', 'fixed', 'done', 'completed']
    for phrase in generic_phrases:
        if phrase in text_lower:
            score -= 1

    return score


# =============================================================================
# SOP TYPE DETECTION
# =============================================================================

def detect_sop_type(combined: str) -> str:
    """
    Detect whether content describes a bug-fix or process SOP.

    Returns:
        'bugfix' or 'process'
    """
    combined_lower = combined.lower()

    # Process words OVERRIDE problem words - if it's a process, treat it as process
    # "rollback failed deployment" is a PROCESS, not a bug fix
    # NOTE: Removed 'sync' - causes false positives with 'async', 'asyncio', 'Synchronous'
    process_words = {'deploy', 'rollback', 'setup', 'configure', 'install', 'migrate',
                     'backup', 'restore', 'update', 'upgrade', 'create', 'build',
                     'provision', 'initialize', 'init', 'bootstrap', 'spin up',
                     'scale', 'teardown', 'cleanup', 'generate'}
    is_process = any(pw in combined_lower for pw in process_words)

    problem_words = {'fix', 'error', 'block', 'blocking', 'fail', 'crash',
                     'broken', 'wrong', 'missing', 'bug', 'issue', 'latency',
                     'timeout', 'hang', 'freeze', 'slow', 'stuck',
                     '500', '502', '503', '504', '400', '401', '403', '404'}
    # Only treat as bug-fix if it has problem words AND is not clearly a process
    is_bug_fix = any(pw in combined_lower for pw in problem_words) and not is_process

    return 'bugfix' if is_bug_fix else 'process'


# =============================================================================
# BUG-FIX SOP ENHANCER (6-Zone Format)
# =============================================================================

def extract_bugfix_zones(combined: str) -> str:
    """
    Extract zones 2-6 for BUG-FIX SOPs using the 6-zone parentheses format.

    6-ZONE FORMAT:
    Zone 2: bad_sign     - Observable symptom (what you SEE)
    Zone 3: (antecedent) - Contributing factors (in parentheses)
    Zone 4: fix          - Treatment action (HOW to fix)
    Zone 5: (stack)      - Tools involved (in parentheses)
    Zone 6: outcome      - Desired state (opposite of bad_sign)

    Output: bad_sign (antecedent) → fix (stack) → outcome

    Philosophy: Bug-fix SOPs diagnose problems. They show:
    - What went wrong (symptom + cause)
    - How to fix it (action + tools)
    - What success looks like (outcome)

    Args:
        combined: Combined text (task + details) for zone extraction

    Returns:
        Arrow-separated zone parts (zones 2-6), or empty string if insufficient content
    """
    combined_lower = combined.lower()

    # === BUG FIX SOP: Full 6-zone parentheses format ===
    bad_sign = []       # Zone 2
    antecedent = []     # Zone 3
    fix_action = []     # Zone 4
    stack = []          # Zone 5
    outcome = []        # Zone 6

    # === Zone 2: BAD SIGNS (objective indicators) ===
    bad_signs_vocab = {
        'hang': 'hang', 'hangs': 'hang', 'hung': 'hang', 'hanging': 'hang',
        'freeze': 'freeze', 'freezes': 'freeze', 'frozen': 'freeze', 'freezing': 'freeze',
        'stuck': 'stuck', 'stalled': 'stuck', 'stalling': 'stuck',
        'unresponsive': 'unresponsive', 'dead': 'dead', 'down': 'down',
        'timeout': 'timeout', 'timeouts': 'timeout', 'timed out': 'timeout',
        'crash': 'crash', 'crashed': 'crash', 'crashes': 'crash', 'crashing': 'crash',
        '500': '500', '502': '502', '503': '503', '504': '504',
        '400': '400', '401': '401', '403': '403', '404': '404',
        'slow': 'slow', 'sluggish': 'slow', 'laggy': 'slow',
        'disconnect': 'disconnected', 'disconnected': 'disconnected',
        'dropped': 'dropped', 'lost connection': 'disconnected',
        'error': 'error', 'fail': 'failure', 'failed': 'failure',
    }

    bad_to_good = {
        'hang': 'responsive', 'freeze': 'responsive', 'stuck': 'responsive',
        'unresponsive': 'responsive', 'dead': 'healthy', 'down': 'online',
        'timeout': 'responsive', 'crash': 'stable', 'slow': 'fast',
        '500': 'working', '502': 'working', '503': 'working', '504': 'working',
        '400': 'working', '401': 'accessible', '403': 'accessible', '404': 'accessible',
        'disconnected': 'connected', 'dropped': 'connected',
        'error': 'working', 'failure': 'working',
    }

    for raw, normalized in bad_signs_vocab.items():
        if raw in combined_lower and normalized not in bad_sign:
            bad_sign.append(normalized)
            if normalized in bad_to_good:
                inferred_outcome = bad_to_good[normalized]
                if inferred_outcome not in outcome:
                    outcome.append(inferred_outcome)
            if len(bad_sign) >= 2:
                break

    if not bad_sign and 'latency' in combined_lower:
        bad_sign.append('latency')
        outcome.append('responsive')

    # === Zone 3: ANTECEDENT (contributing factors) ===
    antecedent_words = {
        'blocking': ('blocking', 1), 'block': ('blocking', 1), 'blocked': ('blocking', 1),
        'sync': ('sync I/O', 1), 'synchronous': ('sync I/O', 1),
        'missing': ('missing', 1), 'wrong': ('misconfigured', 1),
        'deadlock': ('deadlock', 1), 'race': ('race condition', 1),
        'no timeout': ('no timeout', 2), 'unbounded': ('unbounded', 2),
        'leak': ('memory leak', 2), 'overflow': ('overflow', 2),
        'lock': ('lock contention', 2), 'mutex': ('mutex', 2),
        'corrupt': ('corruption', 2), 'malformed': ('malformed data', 2),
        'invalid': ('invalid', 2), 'expired': ('expired', 2),
        'proxied': ('DNS proxied', 3), 'firewall': ('firewall', 3),
        'dns': ('DNS', 3), 'ssl': ('SSL/TLS', 3),
        'env': ('env var', 3), 'path': ('path issue', 3),
        'permission': ('permissions', 3), 'credentials': ('credentials', 3),
    }

    found_antecedents = []
    for word, (label, priority) in antecedent_words.items():
        if word in combined_lower and label not in [a[0] for a in found_antecedents]:
            found_antecedents.append((label, priority))

    found_antecedents.sort(key=lambda x: x[1])
    antecedent = [label for label, _ in found_antecedents[:4]]

    # === Zone 4: FIX (the treatment) ===
    if 'asyncio.to_thread' in combined_lower:
        fix_action.append('asyncio.to_thread')
    elif 'to_thread' in combined_lower:
        fix_action.append('to_thread')
    if 'wrap' in combined_lower and 'asyncio.to_thread' not in fix_action:
        fix_action.append('wrap')
    if 'check' in combined_lower and 'dns' in combined_lower:
        fix_action.append('check DNS')
    if 'restart' in combined_lower:
        fix_action.append('restart')
    if 'recreate' in combined_lower:
        fix_action.append('recreate')
    if 'proxied' in combined_lower and 'false' in combined_lower:
        fix_action.append('set proxied=false')
    if 'add' in combined_lower:
        fix_action.append('add')
    if 'configure' in combined_lower:
        fix_action.append('configure')

    # === Zone 5: STACK (tools involved) ===
    tech_in_content = ['boto3', 'bedrock', 'soundfile', 'python', 'asyncio',
                       'cloudflare', 'acm', 'api', 'gateway', 'llm', 'sdk',
                       'docker', 'ecs', 'terraform', 'websocket', 'nginx',
                       'gunicorn', 'django', 'postgres', 'redis', 'rabbitmq',
                       'livekit', 'webrtc', 'lambda', 's3', 'ec2', 'rds']
    for tech in tech_in_content:
        if tech in combined_lower and tech not in stack:
            stack.append(tech)

    # === Zone 6: DESIRED OUTCOME ===
    explicit_outcomes = ['responsive', 'working', 'healthy', 'stable',
                         'connected', 'fast', 'resolved', 'fixed', 'accessible', 'online']
    for o in explicit_outcomes:
        if o in combined_lower and o not in outcome:
            outcome.insert(0, o)
            break

    # === BUILD ZONE PARTS (2-6 only) ===
    if bad_sign or antecedent:
        parts = []

        # Zones 2-3: bad_sign (antecedent)
        if bad_sign:
            part1 = ' '.join(bad_sign[:2])
            if antecedent:
                part1 += f" ({', '.join(antecedent[:4])})"
            parts.append(part1)
        elif antecedent:
            parts.append(', '.join(antecedent[:4]))

        # Zones 4-5: fix (stack)
        if fix_action:
            part2 = ' '.join(fix_action[:2])
            if stack:
                part2 += f" ({', '.join(stack[:4])})"
            parts.append(part2)
        elif stack:
            parts.append(', '.join(stack[:4]))

        # Zone 6: desired outcome
        if outcome:
            parts.append(outcome[0])

        # Return ONLY the zone parts - caller handles the heart
        if len(parts) >= 2:
            return ' → '.join(parts)

    return ""  # Couldn't extract zone parts


# =============================================================================
# PROCESS SOP ENHANCER (Chain Format)
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

    Extract what's ACTUALLY in the content, organize it clearly.
    Don't force structure - let the content speak.

    Args:
        combined: Combined text (task + details) for zone extraction

    Returns:
        Arrow-separated chain parts, or empty string if insufficient content
    """
    combined_lower = combined.lower()

    # === EXTRACT ROUTES/TOOLS MENTIONED ===
    # These are the HOW - the methods/tools to accomplish the task
    routes = []
    route_vocab = {
        # Normalize variations → canonical form
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
    }
    for raw, normalized in route_vocab.items():
        # Word boundary matching for routes
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower) and normalized not in routes:
            routes.append(normalized)
            if len(routes) >= 3:
                break

    # === EXTRACT CHAIN STEPS MENTIONED ===
    # These are the WHAT - the sequence of actions
    # Only include steps that are ACTUALLY mentioned
    chain_steps = []
    seen_groups = {}  # group → (step, order) to track what we've seen

    step_vocab = {
        # raw_word: (normalized, order, group)
        # group = conceptual grouping (only keep one per group)
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
        # Use word boundary matching to avoid "up" in "backup"
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower):
            if group not in seen_groups:
                seen_groups[group] = (normalized, order)
            else:
                # Same group - keep more specific (restart > start, etc.)
                existing, existing_order = seen_groups[group]
                # Prefer: restart > start, verify > check
                prefer_map = {'restart': 'start', 'verify': 'check', 'deploy': 'install'}
                if normalized in prefer_map and existing == prefer_map[normalized]:
                    seen_groups[group] = (normalized, order)

    # Sort by order and extract steps
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
    }
    for raw, normalized in verification_vocab.items():
        # Word boundary matching for verifications
        if re.search(r'\b' + re.escape(raw) + r'\b', combined_lower) and normalized not in verifications:
            verifications.append(normalized)
            if len(verifications) >= 2:
                break

    # === BUILD OUTPUT FROM WHAT'S ACTUALLY THERE ===
    # Don't force structure - emerge from content
    parts = []

    # Routes (if mentioned)
    if routes:
        parts.append(f"via ({', '.join(routes)})")

    # Chain (if steps mentioned)
    if chain_steps:
        parts.extend(chain_steps)

    # Verification (if mentioned and not redundant with last step)
    last_step = chain_steps[-1] if chain_steps else ''
    if verifications and last_step not in ('verify', 'check', 'test', 'monitor'):
        parts.append(f"✓ {verifications[0]}")

    # Return ONLY the zone parts - caller handles the heart
    if len(parts) >= 2:
        return ' → '.join(parts)

    return ""  # Couldn't extract zone parts


# =============================================================================
# DISPATCHER: Routes to correct enhancer based on SOP type
# =============================================================================

def extract_zone_parts(combined: str) -> str:
    """
    Extract zone parts from content for SOP title enhancement.

    ORIGINAL BEHAVIOR PRESERVED:
    - Detects if content is bug-fix or process based on keywords
    - For bug-fix: returns 6-zone format (bad_sign, antecedent, fix, stack, outcome)
    - For process: returns chain format (via routes → steps → verification)

    Args:
        combined: Combined text (task + details) for zone extraction

    Returns:
        Arrow-separated zone parts string, or empty string if insufficient content
    """
    sop_type = detect_sop_type(combined)

    if sop_type == 'bugfix':
        return extract_bugfix_zones(combined)
    else:
        return extract_process_zones(combined)


def generate_bugfix_sop_title(task: str, details: str = None) -> str:
    """
    Generate optimal SOP title with beautiful core + 6-zone structure.

    BOUNDARY: This enhancer ONLY processes bug-fix SOPs.
    - Returns None for process SOPs (process_sop_enhancer.py handles those)
    - Detects bug-fix via problem keywords (fix, error, crash, timeout, etc.)

    PHILOSOPHY: Heart FIRST, then structure. Keep what works, add zones.

    Process:
    1. FIRST: Run multi-candidate approach to generate beautiful core title
    2. THEN: Extract zone parts (zones 2-6) from content
    3. APPEND: If core doesn't have zones AND zones exist, add them

    The 6-zone format:
    [HEART from candidates]: bad_sign (antecedent) → fix (stack) → outcome

    Zone 1 (HEART) = the beautiful title from multi-candidate scoring
    Zones 2-6 = additive structure extracted separately

    Args:
        task: Original task description
        details: Additional details for zone extraction

    Returns:
        Beautiful core title + zone parts when available
        None if content is not a bug-fix SOP (caller should use process_sop_enhancer)
    """
    combined = f"{task} {details or ''}"

    # === BOUNDARY CHECK: Only process bug-fix SOPs ===
    # This enhancer is SPECIFICALLY for bug-fix SOPs.
    # Process SOPs should be handled by process_sop_enhancer.py
    sop_type = detect_sop_type(combined)
    if sop_type != 'bugfix':
        # Not a bug-fix SOP - return None to signal caller to use process enhancer
        return None

    # === STEP 1: Generate beautiful core title via multi-candidate approach ===
    # This is the existing proven logic - produces rich, descriptive titles

    # === SCORE ALL WORDS FOR USEFULNESS ===
    # Examples guide scoring, but any useful word can win

    # Example technical terms (bonus, not requirement)
    TECH_EXAMPLES = {
        'docker', 'terraform', 'aws', 'api', 'nginx', 'django', 'python', 'bash', 'git',
        'ecs', 'lambda', 'rds', 's3', 'async', 'asyncio', 'boto3', 'bedrock', 'whisper',
        'livekit', 'webrtc', 'websocket', 'container', 'endpoint', 'health', 'healthcheck',
        'config', 'port', 'ssl', 'tls', 'http', 'https', 'postgres', 'redis', 'rabbitmq',
        'opensearch', 'seaweedfs', 'database', 'db', 'gunicorn', 'systemctl', 'compose',
        'dockerfile', 'kubernetes', 'k8s', 'yaml', 'json', 'llm', 'stt', 'tts', 'gpu',
        'cpu', 'nlb', 'alb', 'vpc', 'ec2', 'ami', 'iam', 'asg', 'timeout', 'retry',
        'cache', 'queue', 'stream', 'batch', 'loop', 'thread', 'pool', 'curl', 'wget',
        'ssh', 'restart', 'reload', 'deploy', 'rollback', 'policy', 'trigger', 'hook',
        'event', 'handler', 'callback', 'recovery', 'failover', 'backup', 'restore',
    }

    # Filler words (skip entirely)
    FILLER = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have',
        'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may',
        'might', 'must', 'shall', 'can', 'this', 'that', 'these', 'those', 'it', 'they',
        'and', 'but', 'or', 'for', 'with', 'from', 'to', 'of', 'in', 'on', 'at', 'by',
        'successfully', 'completed', 'remember', 'apply', 'approach', 'similar', 'future',
        'tasks', 'ensure', 'verify', 'quick', 'clear', 'good', 'just', 'also', 'very',
        'really', 'actually', 'basically', 'simply', 'about', 'using', 'used', 'uses',
        'added', 'configured', 'set', 'made', 'got', 'get', 'put', 'take',
        'automatic', 'automatically', 'manual', 'manually', 'default', 'defaults',
    }

    def word_usefulness(word):
        """Score word for usefulness. Higher = better. Negative = skip."""
        w = word.lower()
        if w in FILLER or len(w) < 2:
            return -10
        s = 0
        if w in TECH_EXAMPLES:
            s += 5
        if w in PROBLEM_WORDS:  # Problem words are valuable for symptom zone
            s += 4
        if '.' in word:  # module.function
            s += 8
        if '_' in word:  # snake_case
            s += 4
        if '/' in word:  # path
            s += 6
        if len(w) >= 6:  # longer = more specific
            s += 2
        if any(c.isdigit() for c in w):  # numbers = specific
            s += 3
        return s

    # === ENHANCEMENT 1: COMPOUND TERM DETECTION ===
    # Pairs that should be hyphenated for clarity
    COMPOUND_PAIRS = {
        ('restart', 'policy'): 'restart-policy',
        ('health', 'check'): 'health-check',
        ('event', 'loop'): 'event-loop',
        ('time', 'out'): 'timeout',
        ('time', 'zone'): 'timezone',
        ('dead', 'letter'): 'dead-letter',
        ('load', 'balancer'): 'load-balancer',
        ('auto', 'scaling'): 'auto-scaling',
        ('rate', 'limit'): 'rate-limit',
        ('back', 'off'): 'backoff',
        ('retry', 'policy'): 'retry-policy',
        ('connection', 'pool'): 'connection-pool',
        ('thread', 'pool'): 'thread-pool',
        ('idle', 'timeout'): 'idle-timeout',
        ('keep', 'alive'): 'keepalive',
    }

    def make_compounds(words):
        """Detect adjacent word pairs that should be hyphenated."""
        result = []
        skip_next = False
        words_lower = [w.lower() for w in words]
        for i, w in enumerate(words_lower):
            if skip_next:
                skip_next = False
                continue
            if i < len(words_lower) - 1:
                pair = (w, words_lower[i+1])
                if pair in COMPOUND_PAIRS:
                    result.append(COMPOUND_PAIRS[pair])
                    skip_next = True
                    continue
            result.append(w)
        return result

    # === ENHANCEMENT 2: LOGICAL WORD ORDERING ===
    #
    # ER MEDICINE ANALOGY:
    #   Bad signs: "patient has chest pain" (observable bad state)
    #   Antecedent: "myocardial infarction" (what caused it)
    #   Treatment: "administer aspirin" (the fix)
    #
    # BAD_SIGNS: Observable indicators that something went wrong
    #   - Objective: 403 error, timeout, crash, CPU 100%
    #   - Experiential: slow, laggy, unresponsive, stuck
    #   - These are EXAMPLES - extend vocabulary freely
    #   - ONLY include signs that were ACTUALLY observed/stated
    #   - DO NOT infer bad signs from antecedents
    #
    # LEARNING ANTECEDENTS:
    #   - When we observe "bad sign X was caused by antecedent Y", record it
    #   - Over time, build causal chains: bad sign → antecedent → fix
    #   - Balance: be objective but learn patterns
    BAD_SIGNS = {
        # Observable states
        'hang', 'hangs', 'hanging', 'hung',
        'freeze', 'freezes', 'freezing', 'frozen',
        'stuck', 'stall', 'stalled', 'stalling',
        'unresponsive', 'dead', 'down',
        # Observable failures
        'timeout', 'timeouts', 'timed',
        'crash', 'crashed', 'crashes', 'crashing',
        'fail', 'fails', 'failed', 'failing',
        # Error manifestations
        '500', '502', '503', '504', '400', '401', '403', '404',
        'error', 'errors',
        # Performance symptoms
        'slow', 'sluggish', 'lag', 'laggy', 'latency',
        # Connection symptoms
        'disconnect', 'disconnected', 'dropped', 'lost',
    }

    # ANTECEDENT_WORDS: Technical WHY (what caused the bad sign)
    # In medicine: antecedent = what came before / triggered the condition
    ANTECEDENT_WORDS = {
        'blocking', 'blocked', 'block',
        'sync', 'synchronous',
        'loop', 'deadlock',
        'leak', 'leaked', 'leaking',
        'race', 'racing',
        'overflow', 'underflow',
        'corruption', 'corrupted',
        'missing', 'absent',
        'wrong', 'incorrect', 'invalid',
        'broken', 'malformed',
    }

    # GOOD_OUTCOMES: Zone 5 - Desired state after fix (opposite of bad_sign)
    # Only add if explicitly stated OR inferrable as opposite of bad_sign
    GOOD_OUTCOMES = {
        # Opposites of bad signs
        'responsive',    # opposite of: hang, stuck, unresponsive
        'working',       # opposite of: broken, failed
        'healthy',       # opposite of: dead, down
        'stable',        # opposite of: crash, unstable
        'connected',     # opposite of: disconnect, dropped
        'fast',          # opposite of: slow, laggy
        'resolved',      # generic success
        'fixed',         # generic success
        'accessible',    # opposite of: 403, 404
        'online',        # opposite of: offline, down
    }

    # Legacy aliases for backward compatibility
    CAUSE_WORDS = ANTECEDENT_WORDS
    PROBLEM_WORDS = ANTECEDENT_WORDS | {'fix', 'fixed', 'error', 'fail', 'crash'}

    SOLUTION_WORDS = {'wrap', 'wrapped', 'add', 'added', 'use', 'used', 'set', 'change', 'move', 'create', 'enable', 'disable'}
    # Tools/tech go last (already in TECH_EXAMPLES)

    def order_descriptors(words):
        """Order descriptors: problem → solution → tools/tech."""
        problem = []
        solution = []
        tools = []
        other = []
        for w in words:
            wl = w.lower()
            if wl in PROBLEM_WORDS:
                problem.append(w)
            elif wl in SOLUTION_WORDS:
                solution.append(w)
            elif wl in TECH_EXAMPLES or '.' in w or '/' in w:
                tools.append(w)
            else:
                other.append(w)
        # Order: problem context, solution action, then tools (most specific last)
        return problem + solution + other + tools

    def order_descriptors_arrows(words):
        """
        Five-zone ER-medicine blueprint with parentheses grouping:
        bad_sign (antecedent) → fix (stack) → desired_outcome

        Zone 1: BAD SIGN (what you SEE - hang, timeout, 500, crash)
        Zone 2: ANTECEDENT (what caused it - blocking, sync, deadlock) [in parentheses]
        Zone 3: FIX (treatment - asyncio.to_thread, wrap)
        Zone 4: STACK (tools - boto3, soundfile) [in parentheses]
        Zone 5: DESIRED OUTCOME (success state - responsive, working) [optional]

        ER Medicine Analogy:
          bad_sign: "chest pain" (antecedent: "arterial blockage") → fix: "stent" (stack: "cath lab") → outcome: "pain-free"

        Example output:
          latency (sync I/O) → asyncio.to_thread (boto3) → responsive

        Benefits:
          - Parentheses group cause with symptom, tool with fix
          - Quick scan: see problem, see solution, see goal
          - Zone 5 gives success criterion for verification

        IMPORTANT: Only include bad signs that were ACTUALLY stated, not inferred.
        """
        bad_sign = []
        antecedent = []
        fix = []
        stack = []
        outcome = []

        for w in words:
            wl = w.lower()
            if wl in BAD_SIGNS:
                bad_sign.append(w)
            elif wl in ANTECEDENT_WORDS:
                antecedent.append(w)
            elif wl in GOOD_OUTCOMES:
                outcome.append(w)
            elif '.' in w:  # Methods are THE fix (asyncio.to_thread, etc)
                fix.append(w)
            elif wl in SOLUTION_WORDS:
                fix.append(w)
            elif wl in TECH_EXAMPLES or '/' in w:
                stack.append(w)
            else:
                stack.append(w)  # Unknown goes to stack

        # Build five-zone parentheses format
        # Format: bad_sign (antecedent) → fix (stack) → outcome
        parts = []

        # Part 1: bad_sign (antecedent)
        if bad_sign:
            part1 = ' '.join(bad_sign[:2])
            if antecedent:
                part1 += f" ({' '.join(antecedent[:2])})"
            parts.append(part1)
        elif antecedent:
            # No bad_sign stated, start with antecedent alone
            parts.append(' '.join(antecedent[:2]))

        # Part 2: fix (stack)
        if fix:
            part2 = ' '.join(fix[:2])
            if stack:
                part2 += f" ({' '.join(stack[:3])})"
            parts.append(part2)
        elif stack:
            # No fix method, just stack
            parts.append(' '.join(stack[:3]))

        # Part 3: desired outcome (optional)
        if outcome:
            parts.append(' '.join(outcome[:1]))  # Max 1 outcome word

        # Return arrow-joined string
        if len(parts) > 1:
            return ' → '.join(parts)
        elif parts:
            return parts[0]
        else:
            return ' '.join(words)  # Fallback

    # Score all words from combined text
    all_words = re.findall(r'\b[a-zA-Z0-9_./]+\b', combined)
    scored = [(word_usefulness(w), w) for w in all_words]
    scored.sort(reverse=True)

    # Take unique words with positive scores
    tech = []
    seen = set()
    for score, word in scored:
        if score > 0:
            wl = word.lower()
            if wl not in seen:
                tech.append(wl)
                seen.add(wl)

    # Apply compound detection to tech words
    tech = make_compounds(tech)

    # Methods (module.function) - high value
    methods = re.findall(r'\b([a-z_]+\.[a-z_]+)\b', combined)
    methods = list(dict.fromkeys(methods))

    # Paths (/health, /api/v1/users, etc) - high value, GUARANTEED inclusion
    # Include digits for versioned paths like /api/v1/
    paths = re.findall(r'(/[a-z0-9_/-]+)', combined, re.IGNORECASE)
    paths = list(dict.fromkeys(paths))  # Dedupe paths

    # Clean core task
    core = task.strip()
    core = re.sub(r'\b(successfully|completed|running|verified)\b', '', core, flags=re.IGNORECASE)
    core = ' '.join(core.split())

    # === GENERATE 5 CANDIDATE TITLES ===

    def dedupe(words, exclude=None):
        """Remove duplicates and excluded words."""
        exclude = set(w.lower() for w in (exclude or []))
        seen = set()
        result = []
        for w in words:
            wl = w.lower()
            if wl not in seen and wl not in exclude and len(wl) > 1:
                seen.add(wl)
                result.append(wl)
        return result

    core_words = core.lower().split()

    # V1: Core + all tech (comprehensive) - ordered
    v1_tech = dedupe(tech, core_words)
    v1_tech = order_descriptors(v1_tech)
    v1 = f"{core}: {' '.join(v1_tech)}" if v1_tech else core

    # V2: Core + methods + key tech (focused) - ordered
    v2_parts = dedupe(methods + tech[:5], core_words)
    v2_parts = order_descriptors(v2_parts)
    v2 = f"{core}: {' '.join(v2_parts)}" if v2_parts else core

    # V3: Core + paths + methods (specific) - paths guaranteed first
    v3_parts = dedupe(paths + methods + tech[:3], core_words)
    v3 = f"{core}: {' '.join(v3_parts)}" if v3_parts else core

    # V4: Short core + dense tech (scannable) - ordered
    short_core = ' '.join(core.split()[:4])
    v4_tech = dedupe(tech[:8], short_core.lower().split())
    v4_tech = order_descriptors(v4_tech)
    v4 = f"{short_core}: {' '.join(v4_tech)}" if v4_tech else short_core

    # V5: Methods first (action-focused)
    v5_parts = dedupe(methods + paths + tech[:4], core_words)
    v5 = f"{core}: {' '.join(v5_parts)}" if v5_parts else core

    candidates = [v1, v2, v3, v4, v5]

    # === ENHANCEMENT 3: GUARANTEE PATH INCLUSION ===
    # Create a path-prioritized candidate that ALWAYS includes paths
    if paths:
        # V6: Path-first variant - ensures paths are never lost
        v6_parts = paths + methods[:2] + [t for t in tech[:4] if t not in paths]
        v6_parts = dedupe(v6_parts, core_words)
        v6 = f"{core}: {' '.join(v6_parts)}" if v6_parts else core
        candidates.append(v6)

    # === ENHANCEMENT 4: ARROW-SEPARATED BLUEPRINT FORMAT ===
    # V7: Arrow format - symptom → fix → stack (three-zone blueprint)
    # Don't exclude problem words from core - they're the symptom we want!
    core_words_except_problems = [w for w in core_words if w not in PROBLEM_WORDS]
    v7_tech = dedupe(tech, core_words_except_problems)
    v7_arrows = order_descriptors_arrows(v7_tech)
    v7 = f"{core}: {v7_arrows}" if v7_arrows else core
    candidates.append(v7)

    # === SCORE AND SELECT BEST ===

    def score_title(title):
        """Score a title for usefulness. Higher = better."""
        s = 0
        words = title.lower().split()

        # Reward technical term density
        for w in words:
            if w in tech or '.' in w or '/' in w:
                s += 3  # Technical terms are valuable

        # Reward optimal length (60-100 chars is sweet spot)
        length = len(title)
        if 60 <= length <= 100:
            s += 10
        elif 40 <= length <= 120:
            s += 5

        # Penalize filler words
        filler = {'the', 'and', 'for', 'with', 'from', 'that', 'this', 'was', 'were'}
        for w in words:
            if w in filler:
                s -= 2

        # Penalize meta-commentary words
        meta = {'remember', 'apply', 'approach', 'similar', 'future', 'ensure', 'verify'}
        for w in words:
            if w in meta:
                s -= 5

        # Reward methods/paths (very specific) - BOOSTED
        if any('.' in w for w in words):
            s += 8  # Methods are highly valuable
        if any('/' in w for w in words):
            s += 10  # Paths are extremely valuable - guarantee inclusion

        # Reward compound terms (hyphenated)
        if any('-' in w for w in words):
            s += 3  # Compound terms show clarity

        # Reward arrow format (hierarchical blueprint)
        if '→' in title:
            s += 5  # Arrow format shows clear problem→solution→tools flow

        return s

    # Score all candidates
    scored = [(score_title(c), c) for c in candidates]
    scored.sort(reverse=True)

    # Return the best one
    best = scored[0][1]

    # === POST-SELECTION: GUARANTEE PATH INJECTION ===
    # If paths exist but didn't make it into the best title, inject them
    if paths and not any(p in best for p in paths):
        # Inject paths right after the colon
        if ':' in best:
            before_colon, after_colon = best.split(':', 1)
            path_str = ' '.join(paths)
            best = f"{before_colon}: {path_str}{after_colon}"
        else:
            path_str = ' '.join(paths)
            best = f"{best}: {path_str}"

    # === STEP 2: ASSIGN ZONE 1 (HEART) FROM ORIGINAL TASK ===
    # The heart comes from the ORIGINAL task description - not shortened
    # Only clean minimal filler, preserve the full descriptive meaning
    filler = {'the', 'a', 'an'}  # Minimal filler only - keep prepositions for meaning
    task_words = task.split()
    # Only remove articles if task is long enough
    if len(task_words) > 5:
        task_words = [w for w in task_words if w.lower() not in filler]
    zone1_heart = ' '.join(task_words)
    # Cap at 90 chars but find word boundary
    if len(zone1_heart) > 90:
        cut = zone1_heart[:90].rfind(' ')
        zone1_heart = zone1_heart[:cut] if cut > 40 else zone1_heart[:90]

    # === STEP 3: EXTRACT ZONES 2-6 ===
    # These are the structured parts: bad_sign (antecedent) → fix (stack) → outcome
    zone_parts = extract_zone_parts(combined)

    # === STEP 3.5: DETECT SOP TYPE ===
    # Needed to add the correct tag at the end
    sop_type = detect_sop_type(combined)

    # === STEP 4: COMBINE INTO FINAL 6-ZONE TITLE ===
    # Format: [Zone 1 HEART]: [Zones 2-6 or tech keywords]
    # ALWAYS preserve the full heart from original task
    #
    # BUT: If the task ALREADY has arrow format or zone structure, DON'T re-process
    # This prevents double-formatting: "title: zones: more zones"

    # Detect already-formatted titles
    already_has_arrows = '→' in task
    already_has_zones = ':' in task and ('→' in task or '(' in task.split(':')[-1])
    already_has_tag = task.startswith('[bug-fix SOP]') or task.startswith('[process SOP]')

    if already_has_arrows or already_has_zones or already_has_tag:
        # Title is already formatted - return as-is with minimal cleanup
        best = task.strip()
        # Just capitalize first letter (unless starts with tag)
        if best and not best.startswith('['):
            best = best[0].upper() + best[1:]
        return best[:180]

    if zone_parts:
        # Full zones available: heart + zones
        best = f"{zone1_heart}: {zone_parts}"
    else:
        # No zones extracted: use heart + tech keywords from multi-candidate
        # Extract just the tech keywords (after colon) from best candidate
        if ':' in best:
            tech_keywords = best.split(':', 1)[1].strip()
            if tech_keywords:
                best = f"{zone1_heart}: {tech_keywords}"
            else:
                best = zone1_heart
        else:
            # No colon in best - check if we have useful tech words to add
            best = zone1_heart

    # Capitalize first letter
    best = best[0].upper() + best[1:] if best else task

    # === STEP 5: ADD SOP TYPE TAG ===
    # Add tag at the BEGINNING of the complete title
    if sop_type == 'bugfix':
        best = f"[bug-fix SOP] {best}"
    else:
        best = f"[process SOP] {best}"

    return best[:180]


# =============================================================================
# SOP DEDUPLICATION
# =============================================================================

class SOPDeduplicator:
    """
    Smart deduplication for SOPs.

    Finds duplicate/similar SOPs and merges them into a single
    source of truth that is comprehensive yet concise.
    """

    SIMILARITY_THRESHOLD = 0.6  # 60% similar = potential duplicate

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """Initialize local tracking database."""
        self.db = sqlite3.connect(str(ENHANCER_DB))
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS sop_hashes (
                id INTEGER PRIMARY KEY,
                content_hash TEXT UNIQUE,
                sop_id TEXT,
                title TEXT,
                created_at TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS merge_log (
                id INTEGER PRIMARY KEY,
                kept_id TEXT,
                removed_id TEXT,
                reason TEXT,
                merged_at TEXT
            )
        """)
        self.db.commit()

    def content_hash(self, title: str, content: str) -> str:
        """Generate hash for SOP content."""
        normalized = f"{title.lower().strip()}|{content.lower().strip()}"
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    def is_duplicate(self, title: str, content: str) -> Tuple[bool, Optional[str]]:
        """
        Check if an SOP is a duplicate of existing one.

        Returns:
            Tuple of (is_duplicate, existing_id_if_duplicate)
        """
        hash_val = self.content_hash(title, content)

        row = self.db.execute(
            "SELECT sop_id FROM sop_hashes WHERE content_hash = ?",
            (hash_val,)
        ).fetchone()

        if row:
            return True, row[0]
        return False, None

    def similarity(self, s1: str, s2: str) -> float:
        """Calculate similarity ratio between two strings."""
        if not s1 or not s2:
            return 0.0
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    def find_similar_sops(self, sops: List[Dict]) -> List[Tuple[Dict, Dict, float]]:
        """
        Find pairs of similar SOPs that might be duplicates.

        Args:
            sops: List of SOP dicts with 'title', 'content', 'id'

        Returns:
            List of (sop1, sop2, similarity_score) tuples
        """
        similar_pairs = []

        for i, sop1 in enumerate(sops):
            for sop2 in sops[i+1:]:
                # Check title similarity
                title_sim = self.similarity(
                    sop1.get('title', ''),
                    sop2.get('title', '')
                )

                # Check content similarity
                content_sim = self.similarity(
                    sop1.get('content', '') or sop1.get('props', ''),
                    sop2.get('content', '') or sop2.get('props', '')
                )

                # Weighted average (title more important)
                combined_sim = title_sim * 0.6 + content_sim * 0.4

                if combined_sim >= self.SIMILARITY_THRESHOLD:
                    similar_pairs.append((sop1, sop2, combined_sim))

        return sorted(similar_pairs, key=lambda x: -x[2])

    def smart_merge(self, sop1: Dict, sop2: Dict) -> Dict:
        """
        Intelligently merge two SOPs, keeping the BEST from both.

        - Better/more specific title
        - Combined key insights
        - More comprehensive content
        - Union of tags

        Args:
            sop1: First SOP dict
            sop2: Second SOP dict

        Returns:
            Merged SOP dict (comprehensive yet concise)
        """
        # Pick better title (more specific = higher specificity score)
        title1 = sop1.get('title', '')
        title2 = sop2.get('title', '')
        best_title = title1 if _specificity_score(title1) >= _specificity_score(title2) else title2

        # Combine key insights
        insight1 = sop1.get('key_insight', '')
        insight2 = sop2.get('key_insight', '')
        if insight1 and insight2 and insight1 != insight2:
            # Both have insights - combine if different enough
            if self.similarity(insight1, insight2) < 0.7:
                combined_insight = f"{insight1}. Also: {insight2}"
            else:
                # Similar - keep longer one
                combined_insight = insight1 if len(insight1) >= len(insight2) else insight2
        else:
            combined_insight = insight1 or insight2

        # Keep longer/more comprehensive content
        content1 = sop1.get('content', '') or json.dumps(sop1.get('props', {}))
        content2 = sop2.get('content', '') or json.dumps(sop2.get('props', {}))
        best_content = content1 if len(content1) >= len(content2) else content2

        # Union of tags
        tags1 = set(sop1.get('tags', []) or [])
        tags2 = set(sop2.get('tags', []) or [])
        combined_tags = list(tags1 | tags2)

        # Track which IDs were merged
        merged_ids = [sop1.get('id'), sop2.get('id')]

        return {
            'title': best_title,
            'key_insight': combined_insight,
            'content': best_content,
            'tags': combined_tags,
            'merged_from': merged_ids,
            'merged_at': datetime.now().isoformat()
        }

    def register_sop(self, sop_id: str, title: str, content: str):
        """Register an SOP hash for future duplicate detection."""
        hash_val = self.content_hash(title, content)
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO sop_hashes (content_hash, sop_id, title, created_at) VALUES (?, ?, ?, ?)",
                (hash_val, sop_id, title, datetime.now().isoformat())
            )
            self.db.commit()
        except Exception:
            pass  # Ignore duplicate key errors

    def log_merge(self, kept_id: str, removed_id: str, reason: str):
        """Log a merge operation."""
        self.db.execute(
            "INSERT INTO merge_log (kept_id, removed_id, reason, merged_at) VALUES (?, ?, ?, ?)",
            (kept_id, removed_id, reason, datetime.now().isoformat())
        )
        self.db.commit()


# =============================================================================
# ARTIFACT DEDUPLICATION
# =============================================================================

class ArtifactDeduplicator:
    """Deduplicate artifacts before storage."""

    def __init__(self):
        self._init_db()

    def _init_db(self):
        self.db = sqlite3.connect(str(ENHANCER_DB))
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS artifact_hashes (
                id INTEGER PRIMARY KEY,
                content_hash TEXT UNIQUE,
                filename TEXT,
                path TEXT,
                stored_at TEXT
            )
        """)
        self.db.commit()

    def content_hash(self, content: str) -> str:
        """Generate hash for artifact content."""
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def is_duplicate(self, filename: str, content: str) -> Tuple[bool, Optional[str]]:
        """
        Check if artifact content is a duplicate.

        Returns:
            Tuple of (is_duplicate, existing_path_if_duplicate)
        """
        hash_val = self.content_hash(content)

        row = self.db.execute(
            "SELECT path FROM artifact_hashes WHERE content_hash = ?",
            (hash_val,)
        ).fetchone()

        if row:
            return True, row[0]
        return False, None

    def register_artifact(self, filename: str, path: str, content: str):
        """Register an artifact hash."""
        hash_val = self.content_hash(content)
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO artifact_hashes (content_hash, filename, path, stored_at) VALUES (?, ?, ?, ?)",
                (hash_val, filename, path, datetime.now().isoformat())
            )
            self.db.commit()
        except Exception:
            pass


# =============================================================================
# ENHANCEMENT PIPELINE
# =============================================================================

def enhance_capture(task: str, details: str = None, area: str = None) -> Dict:
    """
    Enhance a raw capture with extracted insights and deduplication check.

    This is the main entry point for improving capture quality.

    Args:
        task: What was accomplished
        details: How it was done
        area: Architecture area

    Returns:
        Enhanced capture dict with key_insight, specific_title, etc.
    """
    # Extract key insight
    key_insight = extract_key_insight(task, details)

    # Generate specific title
    specific_title = generate_bugfix_sop_title(task, details)

    # Handle None return (not a bug-fix SOP)
    if specific_title is None and PROCESS_ENHANCER_AVAILABLE:
        # Not a bug-fix - use process SOP enhancer for chain format
        specific_title = generate_process_sop_title(task, details)
    if specific_title is None:
        # Fallback if process enhancer also returns None or unavailable
        specific_title = f"[process SOP] {task}"

    # Check for duplicates
    dedup = SOPDeduplicator()
    content = f"{task} {details or ''}"
    is_dup, existing_id = dedup.is_duplicate(specific_title, content)

    return {
        'original_task': task,
        'specific_title': specific_title,
        'key_insight': key_insight,
        'area': area,
        'is_duplicate': is_dup,
        'duplicate_of': existing_id,
        'enhanced_at': datetime.now().isoformat()
    }


def run_dedup_cycle(dry_run: bool = True) -> Dict:
    """
    Run deduplication cycle on all SOPs in database.

    Connects to Context DNA postgres and finds/merges duplicates.

    Args:
        dry_run: If True, only report what would be merged

    Returns:
        Report dict with findings
    """
    import subprocess

    # Query SOPs from postgres
    result = subprocess.run([
        'docker', 'exec', 'contextdna-pg',
        'psql', '-U', 'postgres', '-d', 'acontext', '-t', '-A', '-c',
        "SELECT id, title, props::text FROM blocks WHERE type = 'sop' ORDER BY created_at DESC LIMIT 100"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        return {'error': 'Could not connect to database', 'stderr': result.stderr}

    # Parse results
    sops = []
    for line in result.stdout.strip().split('\n'):
        if '|' in line:
            parts = line.split('|')
            if len(parts) >= 3:
                sops.append({
                    'id': parts[0],
                    'title': parts[1],
                    'props': parts[2]
                })

    # Find duplicates
    dedup = SOPDeduplicator()
    similar_pairs = dedup.find_similar_sops(sops)

    report = {
        'total_sops': len(sops),
        'duplicate_pairs': len(similar_pairs),
        'pairs': [],
        'dry_run': dry_run
    }

    for sop1, sop2, similarity in similar_pairs[:10]:  # Top 10
        pair_info = {
            'sop1_title': sop1.get('title', '')[:50],
            'sop2_title': sop2.get('title', '')[:50],
            'similarity': f"{similarity:.0%}",
            'sop1_id': sop1.get('id'),
            'sop2_id': sop2.get('id')
        }

        if not dry_run:
            # Actually merge
            merged = dedup.smart_merge(sop1, sop2)
            pair_info['merged_title'] = merged['title']
            pair_info['merged_insight'] = merged.get('key_insight', '')[:50]
            dedup.log_merge(sop1.get('id'), sop2.get('id'), f"Similarity: {similarity:.0%}")

        report['pairs'].append(pair_info)

    return report


def fix_missing_insights() -> Dict:
    """
    Fix SOPs that are missing key_insight field.

    Reads existing SOPs and extracts key insights from their content.
    """
    import subprocess

    # Query SOPs from postgres
    result = subprocess.run([
        'docker', 'exec', 'contextdna-pg',
        'psql', '-U', 'postgres', '-d', 'acontext', '-t', '-A', '-c',
        "SELECT id, title, props::text FROM blocks WHERE type = 'sop' ORDER BY created_at DESC LIMIT 50"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        return {'error': 'Could not connect to database'}

    fixed = []
    for line in result.stdout.strip().split('\n'):
        if '|' in line:
            parts = line.split('|')
            if len(parts) >= 3:
                sop_id = parts[0]
                title = parts[1]
                props_str = parts[2]

                try:
                    props = json.loads(props_str)
                except:
                    props = {}

                # Check if missing key_insight
                if not props.get('key_insight'):
                    # Extract from preferences or title
                    prefs = props.get('preferences', '')
                    insight = extract_key_insight(title, prefs)

                    fixed.append({
                        'id': sop_id,
                        'title': title[:50],
                        'extracted_insight': insight[:80]
                    })

    return {
        'total_checked': len(result.stdout.strip().split('\n')),
        'missing_insights': len(fixed),
        'fixed': fixed[:10]  # Show first 10
    }


# =============================================================================
# CLI
# =============================================================================

def enhance_existing_sops(dry_run: bool = True) -> Dict:
    """
    Post-process existing SOPs to enhance titles and add key_insight.

    This runs AFTER Context DNA's SOP extraction to improve quality.
    """
    import subprocess

    # Query SOPs from postgres
    result = subprocess.run([
        'docker', 'exec', 'contextdna-pg',
        'psql', '-U', 'postgres', '-d', 'acontext', '-t', '-A', '-c',
        "SELECT id, title, props::text FROM blocks WHERE type = 'sop' ORDER BY created_at DESC LIMIT 100"
    ], capture_output=True, text=True)

    if result.returncode != 0:
        return {'error': 'Could not connect to database'}

    enhanced = []
    for line in result.stdout.strip().split('\n'):
        if '|' in line:
            parts = line.split('|')
            if len(parts) >= 3:
                sop_id = parts[0]
                old_title = parts[1]
                props_str = parts[2]

                try:
                    props = json.loads(props_str)
                except:
                    props = {}

                # === ACCURACY-FOCUSED ENHANCEMENT ===
                # Philosophy: Only enhance if something is genuinely WRONG or MISSING
                # Don't rework for the sake of reworking - if it's good, leave it
                #
                # SKIP if:
                # 1. Already has proper format with parentheses
                # 2. Title accurately reflects SOP content
                # 3. Marked as enhanced in props
                #
                # ENHANCE if:
                # 1. Title is missing critical keywords from content
                # 2. Title has malformed artifacts (:::)
                # 3. Title is genuinely too vague ("approach", "remember")

                # SKIP: Already marked as enhanced (prevent re-processing loop)
                if props.get('title_enhanced'):
                    continue

                # SKIP: Already has new parentheses format (contains both → and ())
                # These are already up to standard
                if '→' in old_title and '(' in old_title and ')' in old_title:
                    continue

                # SKIP: Has arrows and is reasonably complete (2+ zones)
                # "blocking sync → asyncio.to_thread → responsive" = already good
                # "verify → health" = 2 zones, already structured
                if '→' in old_title:
                    zones = old_title.split('→')
                    if len(zones) >= 2:  # Has at least 2 arrow-separated parts
                        continue  # Already structured, don't touch

                # SKIP: Has proper "heart: content" structure with meaningful content
                # "Docker verifying containers: remembered" - already has structure
                if ':' in old_title and old_title.count(':') == 1:
                    parts = old_title.split(':')
                    heart = parts[0].strip()
                    content = parts[1].strip() if len(parts) > 1 else ''
                    # If heart is meaningful (>15 chars) and content exists, skip
                    if len(heart) > 15 and len(content) > 3:
                        continue  # Already has decent structure

                # FIX: Clean up malformed titles with multiple colons (:::)
                # These are artifacts of previous buggy processing - MUST fix
                has_malformed_colons = '::' in old_title
                if has_malformed_colons:
                    old_title = old_title.split(':')[0].strip()

                # CHECK: Is title genuinely too vague?
                # Only flag as vague if the ENTIRE title is dominated by vague words
                vague_indicators = [
                    'remember', 'apply', 'approach', 'similar', 'future',
                    'successful', 'success', 'remembered', 'solution'
                ]
                # Count how many words are vague vs meaningful
                title_words = old_title.lower().split()
                vague_word_count = sum(1 for w in title_words if any(vi in w for vi in vague_indicators))
                is_vague = vague_word_count > len(title_words) / 2  # >50% vague words

                # CHECK: Is title missing critical info from content?
                prefs = props.get('preferences', '')
                combined = f"{old_title} {prefs}".lower()

                # Critical keywords that should appear in title if in content
                critical_keywords = ['asyncio', 'boto3', 'docker', 'terraform', 'ecs',
                                     'blocking', 'timeout', 'crash', 'error', 'fix']
                keywords_in_content = [kw for kw in critical_keywords if kw in prefs.lower()]
                keywords_in_title = [kw for kw in critical_keywords if kw in old_title.lower()]
                missing_critical_keywords = set(keywords_in_content) - set(keywords_in_title)

                # Only enhance if there's a REAL problem
                needs_enhancement = (
                    has_malformed_colons or  # Artifact that must be fixed
                    (is_vague and len(old_title) < 50) or  # Too vague AND short
                    len(missing_critical_keywords) >= 2  # Missing 2+ critical keywords from content
                )

                if needs_enhancement:
                    # Extract content from props
                    prefs = props.get('preferences', '')
                    combined = f"{old_title} {prefs}"

                    # === STEP-WISE 6-ZONE TITLE GENERATION ===
                    # Same approach as generate_bugfix_sop_title() for consistency

                    # Step 1: Extract Zone 1 (HEART) - the beautiful core title
                    # Preserve the existing heart if it has one
                    zone1_heart = old_title.split(':')[0].strip()

                    # Step 2: Extract Zones 2-6 from content
                    zone_parts = extract_zone_parts(combined)

                    # Step 3: Combine - heart + zones
                    if zone_parts:
                        new_title = f"{zone1_heart}: {zone_parts}"
                    else:
                        # Fallback to generate_bugfix_sop_title if no zones extracted
                        new_title = generate_bugfix_sop_title(old_title, prefs)
                        # Handle None return (not a bug-fix SOP)
                        if new_title is None and PROCESS_ENHANCER_AVAILABLE:
                            new_title = generate_process_sop_title(old_title, prefs)
                        if new_title is None:
                            new_title = f"[process SOP] {old_title}"

                    # Capitalize first letter
                    if new_title:
                        new_title = new_title[0].upper() + new_title[1:]

                    # Extract key insight
                    key_insight = extract_key_insight(old_title, prefs)

                    enhanced.append({
                        'id': sop_id,
                        'old_title': old_title[:50],
                        'new_title': new_title[:80],
                        'key_insight': key_insight[:60]
                    })

                    if not dry_run:
                        # Update the SOP in database
                        # Add key_insight and mark as enhanced to prevent loops
                        props['key_insight'] = key_insight
                        props['title_enhanced'] = True  # Prevent re-enhancement loop
                        props['enhanced_at'] = datetime.now().isoformat()
                        new_props_json = json.dumps(props).replace("'", "''")

                        # Escape title for SQL
                        safe_title = new_title.replace("'", "''")

                        update_sql = f"""
                            UPDATE blocks
                            SET title = '{safe_title}',
                                props = '{new_props_json}'::jsonb
                            WHERE id = '{sop_id}'
                        """

                        subprocess.run([
                            'docker', 'exec', 'contextdna-pg',
                            'psql', '-U', 'postgres', '-d', 'acontext', '-c',
                            update_sql
                        ], capture_output=True)

    return {
        'total_checked': len(result.stdout.strip().split('\n')),
        'enhanced': len(enhanced),
        'dry_run': dry_run,
        'changes': enhanced[:20]  # Show first 20
    }


def run_full_enhancement_cycle(apply: bool = False) -> Dict:
    """
    Run complete enhancement cycle:
    1. Enhance existing SOP titles and key_insights
    2. Find and merge duplicates
    3. Clean up

    Args:
        apply: If True, actually apply changes. If False, dry run.

    Returns:
        Full report of changes
    """
    report = {
        'timestamp': datetime.now().isoformat(),
        'mode': 'APPLY' if apply else 'DRY RUN',
        'enhancement': {},
        'deduplication': {}
    }

    # Step 1: Enhance existing SOPs
    print("Step 1: Enhancing existing SOPs...")
    enhancement_report = enhance_existing_sops(dry_run=not apply)
    report['enhancement'] = enhancement_report

    # Step 2: Find and report duplicates
    print("Step 2: Finding duplicates...")
    dedup_report = run_dedup_cycle(dry_run=True)  # Always dry run dedup for now (needs manual review)
    report['deduplication'] = dedup_report

    return report


def main():
    if len(sys.argv) < 2:
        print("SOP Enhancer - Smart Architecture Enhancement & Deduplication")
        print("")
        print("Commands:")
        print("  enhance <task> <details>  - Enhance a capture (preview)")
        print("  dedup [--apply]           - Find/merge duplicate SOPs")
        print("  enhance-existing          - Enhance existing SOP titles/insights (dry run)")
        print("  enhance-existing --apply  - Actually update existing SOPs")
        print("  full-cycle                - Run complete enhancement + dedup cycle")
        print("  full-cycle --apply        - Run and apply all enhancements")
        print("  report                    - Full enhancement report")
        print("  fix-insights              - Fix missing key_insight fields")
        print("  artifact-check <file>     - Check if artifact is duplicate")
        print("")
        print("Examples:")
        print('  python sop_enhancer.py enhance "Fixed async" "Used asyncio.to_thread"')
        print("  python sop_enhancer.py enhance-existing --apply")
        print("  python sop_enhancer.py full-cycle --apply")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "enhance":
        if len(sys.argv) < 3:
            print("Usage: enhance <task> [details]")
            sys.exit(1)
        task = sys.argv[2]
        details = sys.argv[3] if len(sys.argv) > 3 else None
        result = enhance_capture(task, details)
        print(json.dumps(result, indent=2))

    elif cmd == "dedup":
        apply = "--apply" in sys.argv
        print(f"Running deduplication {'(APPLY MODE)' if apply else '(DRY RUN)'}...")
        report = run_dedup_cycle(dry_run=not apply)

        print(f"\nTotal SOPs: {report.get('total_sops', 0)}")
        print(f"Duplicate pairs found: {report.get('duplicate_pairs', 0)}")

        if report.get('pairs'):
            print("\nTop duplicate pairs:")
            for pair in report['pairs']:
                print(f"  [{pair['similarity']}] {pair['sop1_title'][:30]}...")
                print(f"            ↔ {pair['sop2_title'][:30]}...")
                if pair.get('merged_title'):
                    print(f"         → Merged to: {pair['merged_title'][:40]}")
                print()

        if report.get('error'):
            print(f"Error: {report['error']}")

    elif cmd == "report":
        print("=== SOP Enhancement Report ===\n")

        # Dedup report
        dedup_report = run_dedup_cycle(dry_run=True)
        print(f"Total SOPs: {dedup_report.get('total_sops', 0)}")
        print(f"Potential duplicates: {dedup_report.get('duplicate_pairs', 0)}")

        # Missing insights
        insight_report = fix_missing_insights()
        print(f"Missing key_insight: {insight_report.get('missing_insights', 0)}")

        # Artifact stats
        artifact_dedup = ArtifactDeduplicator()
        artifact_count = artifact_dedup.db.execute(
            "SELECT COUNT(*) FROM artifact_hashes"
        ).fetchone()[0]
        print(f"Unique artifacts tracked: {artifact_count}")

    elif cmd == "fix-insights":
        print("Analyzing SOPs for missing key_insight...")
        report = fix_missing_insights()
        print(f"\nChecked: {report.get('total_checked', 0)} SOPs")
        print(f"Missing insights: {report.get('missing_insights', 0)}")

        if report.get('fixed'):
            print("\nExtracted insights (preview):")
            for item in report['fixed']:
                print(f"\n  Title: {item['title']}")
                print(f"  Insight: {item['extracted_insight']}")

    elif cmd == "artifact-check":
        if len(sys.argv) < 3:
            print("Usage: artifact-check <file_path>")
            sys.exit(1)

        file_path = sys.argv[2]
        if not Path(file_path).exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = Path(file_path).read_text()
        dedup = ArtifactDeduplicator()
        is_dup, existing = dedup.is_duplicate(Path(file_path).name, content)

        if is_dup:
            print(f"DUPLICATE - Already stored at: {existing}")
        else:
            print("UNIQUE - Not a duplicate")

    elif cmd == "enhance-existing":
        apply = "--apply" in sys.argv
        print(f"Enhancing existing SOPs {'(APPLYING)' if apply else '(DRY RUN)'}...")
        report = enhance_existing_sops(dry_run=not apply)

        print(f"\nTotal SOPs checked: {report.get('total_checked', 0)}")
        print(f"SOPs needing enhancement: {report.get('enhanced', 0)}")

        if report.get('changes'):
            print("\nEnhancements (preview):")
            for item in report['changes'][:10]:
                print(f"\n  OLD: {item['old_title']}")
                print(f"  NEW: {item['new_title']}")
                print(f"  KEY: {item['key_insight']}")

        if report.get('error'):
            print(f"Error: {report['error']}")

    elif cmd == "full-cycle":
        apply = "--apply" in sys.argv
        print(f"Running full enhancement cycle {'(APPLYING)' if apply else '(DRY RUN)'}...")
        report = run_full_enhancement_cycle(apply=apply)

        print(f"\n{'='*60}")
        print("FULL ENHANCEMENT CYCLE REPORT")
        print(f"{'='*60}")
        print(f"Mode: {report['mode']}")
        print(f"Timestamp: {report['timestamp']}")

        print(f"\n--- Enhancement ---")
        enhance = report.get('enhancement', {})
        print(f"SOPs checked: {enhance.get('total_checked', 0)}")
        print(f"SOPs enhanced: {enhance.get('enhanced', 0)}")

        print(f"\n--- Deduplication ---")
        dedup = report.get('deduplication', {})
        print(f"Total SOPs: {dedup.get('total_sops', 0)}")
        print(f"Duplicate pairs: {dedup.get('duplicate_pairs', 0)}")

        if enhance.get('changes'):
            print(f"\n--- Sample Enhancements ---")
            for item in enhance['changes'][:5]:
                print(f"  {item['old_title'][:30]}... → {item['new_title'][:40]}...")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
