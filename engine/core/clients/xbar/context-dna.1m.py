#!/usr/bin/env python3
# <xbar.title>Context DNA</xbar.title>
# <xbar.version>v2.0.0</xbar.version>
# <xbar.author>Context DNA</xbar.author>
# <xbar.author.github>context-dna</xbar.author.github>
# <xbar.desc>Full Architecture Brain - Autonomous Learning with Adaptive Domains</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.var>string(API_URL="http://127.0.0.1:3456"): Context DNA API URL</xbar.var>
# <xbar.var>string(PROJECT_DIR=""): Project directory (auto-detects if empty)</xbar.var>

"""
Context DNA xbar Plugin v2.0.0 - Full Architecture Brain Clone

CLONED FROM ACONTEXT MENU - FULL FEATURE PARITY:
- Brain Core Systems status (brain.py, professor.py, context.py, etc.)
- Capture & Learning Systems (auto_capture, auto_learn, objective_success)
- Storage & Verification (artifact_store, sandbox_verify, sop_types)
- Enhancement Systems (work_log, architecture_enhancer)
- SaaS-style metrics (captures today, wins, brain cycles, SOPs, last capture)
- Professor quick queries (ADAPTIVE to user's domains)
- Query Memory (ADAPTIVE to user's tags)
- Context/Blueprint generation
- Recent Wins display
- Auto-Capture Hooks status
- Copy Commands for clipboard
- Full Docker/Service management

ADAPTIVE FEATURES:
- Detects YOUR project's domains from tag usage
- Shows YOUR most-used areas and patterns
- Quick consults for YOUR top domains
- Hierarchical learning organization

Installation:
1. Install xbar: brew install --cask xbar
2. Copy to ~/Library/Application Support/xbar/plugins/
3. chmod +x context-dna.1m.py
4. Run: context-dna serve

Or: context-dna extras install xbar
"""

import json
import logging
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
from datetime import datetime

# Log to file so xbar stdout menu output is not polluted
_log_file = os.path.join(os.path.expanduser("~"), ".context-dna", "xbar.log")
os.makedirs(os.path.dirname(_log_file), exist_ok=True)
logging.basicConfig(
    filename=_log_file,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
_logger = logging.getLogger("context-dna-xbar")

# =============================================================================
# CONFIGURATION (Adapts to user's setup)
# =============================================================================

API_URL = os.environ.get("API_URL", "http://127.0.0.1:3456")
HELPER_URL = os.environ.get("HELPER_URL", "http://127.0.0.1:8080")  # Helper Agent for fallback
PROJECT_DIR = os.environ.get("PROJECT_DIR", "")

# Auto-detect project directory
if not PROJECT_DIR:
    # Try common locations
    for candidate in [
        os.path.expanduser("~/dev/er-simulator-superrepo"),
        os.path.expanduser("~/Documents/er-simulator-superrepo"),
        os.getcwd(),
        str(Path(__file__).parent.parent.parent.parent),
    ]:
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, ".context-dna")):
            PROJECT_DIR = candidate
            break
        elif os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "memory")):
            PROJECT_DIR = candidate
            break

# Find Python interpreter
PYTHON = None
for p in [
    os.path.join(PROJECT_DIR, ".venv/bin/python3") if PROJECT_DIR else None,
    "/usr/bin/python3",
    "/usr/local/bin/python3",
    subprocess.run(["which", "python3"], capture_output=True, text=True).stdout.strip(),
]:
    if p and os.path.isfile(p):
        PYTHON = p
        break

if not PYTHON:
    PYTHON = "python3"

# Context DNA directories
CONTEXT_DNA_DIR = os.path.join(PROJECT_DIR, ".context-dna") if PROJECT_DIR else ""
MEMORY_DIR = os.path.join(PROJECT_DIR, "memory") if PROJECT_DIR else ""  # Legacy local system

# Type emojis
TYPE_EMOJI = {
    "win": "🏆",
    "fix": "🔧",
    "pattern": "🔄",
    "sop": "📋",
    "insight": "💡",
    "gotcha": "⚠️",
}


# =============================================================================
# API HELPERS
# =============================================================================

def api_get(endpoint):
    """GET request to API."""
    try:
        req = Request(f"{API_URL}{endpoint}")
        with urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        _logger.debug(f"API GET {endpoint} failed: {e}")
        return None


def api_post(endpoint, data):
    """POST request to API."""
    try:
        req = Request(
            f"{API_URL}{endpoint}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        _logger.debug(f"API POST {endpoint} failed: {e}")
        return None


def get_stats_with_fallback():
    """
    Get stats with smart fallback.

    1. Try primary API (3456) /api/stats
    2. If returns 0 total, calculate from Helper Agent (8080) /api/learnings/recent

    This handles the case where Python API uses empty local SQLite
    while Helper Agent connects to PostgreSQL with real data.
    """
    # Try primary API first
    stats = api_get("/api/stats")
    if stats and stats.get("total", 0) > 0:
        return stats

    # Fallback: Calculate stats from Helper Agent's learnings
    try:
        req = Request(f"{HELPER_URL}/api/learnings/recent?limit=500")
        with urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            learnings = data.get("learnings", [])

            if not learnings:
                return stats or {"total": 0, "wins": 0, "fixes": 0, "patterns": 0, "today": 0, "streak": 0}

            # Calculate stats from learnings
            from datetime import datetime
            today = datetime.now().strftime('%Y-%m-%d')

            wins = sum(1 for l in learnings if l.get("type") == "win")
            fixes = sum(1 for l in learnings if l.get("type") == "fix")
            patterns = sum(1 for l in learnings if l.get("type") == "pattern")
            today_count = sum(1 for l in learnings if l.get("timestamp", "").startswith(today))

            # Calculate streak (consecutive days with learnings)
            dates = set()
            for l in learnings:
                ts = l.get("timestamp", "")
                if ts:
                    dates.add(ts[:10])  # Extract YYYY-MM-DD

            streak = 0
            check_date = datetime.now()
            while check_date.strftime('%Y-%m-%d') in dates:
                streak += 1
                check_date = check_date.replace(day=check_date.day - 1) if check_date.day > 1 else check_date.replace(month=check_date.month - 1 if check_date.month > 1 else 12, day=28)

            return {
                "total": len(learnings),
                "wins": wins,
                "fixes": fixes,
                "patterns": patterns,
                "today": today_count,
                "streak": streak,
                "source": "helper_agent"  # Indicate data source for debugging
            }
    except Exception as e:
        _logger.debug(f"Stats fallback to helper agent failed: {e}")
        return stats or {"total": 0, "wins": 0, "fixes": 0, "patterns": 0, "today": 0, "streak": 0}


# =============================================================================
# HEALTH CHECKS
# =============================================================================

def check_tcp_port(host, port):
    """Check if a TCP port is open."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        _logger.debug(f"TCP port check {host}:{port} failed: {e}")
        return False


def check_command(command):
    """Check if a command runs successfully."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception as e:
        _logger.debug(f"Command check {command} failed: {e}")
        return False


def check_url(url):
    """Check if a URL is accessible."""
    try:
        req = Request(url)
        with urlopen(req, timeout=3) as response:
            return response.status == 200
    except Exception as e:
        _logger.debug(f"URL check {url} failed: {e}")
        return False


def check_file(path):
    """Check if a file exists."""
    return os.path.isfile(path)


def run_health_checks():
    """Run all health checks."""
    results = {}
    results["docker"] = check_command(["docker", "info"])
    results["postgres"] = check_tcp_port("localhost", 5432)
    results["redis"] = check_tcp_port("localhost", 6379)
    results["opensearch"] = check_tcp_port("localhost", 9200)
    results["jaeger"] = check_tcp_port("localhost", 16686)
    results["ollama"] = check_url("http://localhost:11434/api/tags")
    results["server"] = api_get("/api/health") is not None
    return results


def get_fix_command(service):
    """Get fix command for a service."""
    fixes = {
        "docker": "open -a Docker",
        "postgres": "context-dna up",
        "redis": "context-dna up",
        "opensearch": "context-dna up",
        "jaeger": "context-dna up",
        "ollama": "context-dna up --llm",
        "server": "context-dna up",
    }
    return fixes.get(service, "echo 'No fix available'")


# =============================================================================
# BRAIN CORE SYSTEMS (File-based - Fast check like original)
# =============================================================================

def check_brain_systems():
    """Check which brain core systems are installed (file-based check)."""
    systems = {}

    # Context DNA package (new structure: context-dna/core/)
    if CONTEXT_DNA_DIR:
        systems["brain_core"] = {
            "Brain": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/brain.py")),
            "Professor": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/professor.py")),
            "ObjectiveSuccess": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/objective_success.py")),
            "WorkLog": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/work_log.py")),
        }
        systems["capture"] = {
            "SOPTypes": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/sop_types.py")),
            "VectorStore": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/storage/vector_store.py")),
        }
        systems["hooks"] = {
            "ClaudeHook": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/hooks/claude.py")),
            "CursorHook": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/hooks/cursor.py")),
            "GitHook": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/hooks/git.py")),
        }
        systems["llm"] = {
            "OpenAI": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/llm/openai_provider.py")),
            "Anthropic": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/llm/anthropic_provider.py")),
            "Ollama": check_file(os.path.join(PROJECT_DIR, "context-dna/core/src/context_dna/llm/ollama_provider.py")),
        }

    # Also check legacy memory system (your current local setup)
    if MEMORY_DIR and os.path.isdir(MEMORY_DIR):
        systems["legacy"] = {
            "brain.py": check_file(os.path.join(MEMORY_DIR, "brain.py")),
            "professor.py": check_file(os.path.join(MEMORY_DIR, "professor.py")),
            "context.py": check_file(os.path.join(MEMORY_DIR, "context.py")),
            "query.py": check_file(os.path.join(MEMORY_DIR, "query.py")),
            "objective_success.py": check_file(os.path.join(MEMORY_DIR, "objective_success.py")),
            "auto_capture.py": check_file(os.path.join(MEMORY_DIR, "auto_capture.py")),
            "auto_learn.py": check_file(os.path.join(MEMORY_DIR, "auto_learn.py")),
            "context_dna_client.py": check_file(os.path.join(MEMORY_DIR, "context_dna_client.py")),
            "artifact_store.py": check_file(os.path.join(MEMORY_DIR, "artifact_store.py")),
            "sop_types.py": check_file(os.path.join(MEMORY_DIR, "sop_types.py")),
            "knowledge_graph.py": check_file(os.path.join(MEMORY_DIR, "knowledge_graph.py")),
        }

    return systems


def count_active_systems(systems):
    """Count active systems for automation level."""
    total = 0
    active = 0
    for category, items in systems.items():
        for name, exists in items.items():
            total += 1
            if exists:
                active += 1
    return active, total


# =============================================================================
# SAAS-STYLE METRICS (From cache files - Fast)
# =============================================================================

def get_saas_metrics():
    """Get SaaS-style metrics from cache files."""
    metrics = {
        "brain_cycles": 0,
        "successes_captured": 0,
        "captures_today": 0,
        "last_capture": "never",
        "sops_stored": "?",
    }

    # Try Context DNA state file
    state_file = os.path.join(CONTEXT_DNA_DIR, "state/brain_state.json") if CONTEXT_DNA_DIR else ""
    if os.path.isfile(state_file):
        try:
            with open(state_file) as f:
                data = json.load(f)
                metrics["brain_cycles"] = data.get("cycles_run", 0)
                metrics["successes_captured"] = data.get("successes_captured", 0)
        except Exception as e:
            _logger.debug(f"Failed to read Context DNA state file: {e}")

    # Try legacy cache files
    brain_cache = os.path.join(MEMORY_DIR, ".brain_cache.json") if MEMORY_DIR else ""
    if os.path.isfile(brain_cache):
        try:
            with open(brain_cache) as f:
                data = json.load(f)
                metrics["brain_cycles"] = data.get("cycles_run", metrics["brain_cycles"])
                metrics["successes_captured"] = data.get("successes_captured", metrics["successes_captured"])
        except Exception as e:
            _logger.debug(f"Failed to read legacy brain cache: {e}")

    capture_state = os.path.join(MEMORY_DIR, ".auto_capture_state.json") if MEMORY_DIR else ""
    if os.path.isfile(capture_state):
        try:
            with open(capture_state) as f:
                data = json.load(f)
                metrics["captures_today"] = data.get("captures_today", 0)
                last = data.get("last_capture", "")
                if last:
                    # Extract time portion
                    try:
                        metrics["last_capture"] = last.split("T")[1][:5]
                    except Exception as e:
                        _logger.debug(f"Failed to parse last_capture timestamp: {e}")
                        metrics["last_capture"] = last[:16]
        except Exception as e:
            _logger.debug(f"Failed to read auto capture state: {e}")

    # Try SOP registry
    sop_cache = os.path.join(MEMORY_DIR, ".sop_registry_cache.json") if MEMORY_DIR else ""
    if os.path.isfile(sop_cache):
        try:
            with open(sop_cache) as f:
                data = json.load(f)
                metrics["sops_stored"] = len(data) if isinstance(data, list) else "?"
        except Exception as e:
            _logger.debug(f"Failed to read SOP registry cache: {e}")

    return metrics


# =============================================================================
# ADAPTIVE DOMAINS (User's actual usage patterns)
# =============================================================================

def extract_user_domains(recent_learnings):
    """Extract user's domain hierarchy from their tags."""
    if not recent_learnings:
        return {"tags": [], "counts": {}, "by_tag": {}}

    tag_counts = Counter()
    tag_learnings = {}

    for learning in recent_learnings:
        tags = learning.get("tags", [])
        for tag in tags:
            tag = tag.lower().strip()
            if tag:
                tag_counts[tag] += 1
                if tag not in tag_learnings:
                    tag_learnings[tag] = []
                tag_learnings[tag].append(learning)

    top_tags = [tag for tag, count in tag_counts.most_common(10) if count >= 2]

    return {
        "tags": top_tags,
        "counts": dict(tag_counts.most_common(15)),
        "by_tag": tag_learnings,
    }


def get_recent_wins(limit=5):
    """Get recent wins from various sources."""
    wins = []

    # Try API first
    recent = api_get(f"/api/recent?limit={limit}&type=win")
    if recent and recent.get("recent"):
        wins = recent["recent"]
    else:
        # Try legacy capture state
        capture_state = os.path.join(MEMORY_DIR, ".auto_capture_state.json") if MEMORY_DIR else ""
        if os.path.isfile(capture_state):
            try:
                with open(capture_state) as f:
                    data = json.load(f)
                    recent_cmds = data.get("recent_commands", [])
                    for cmd in reversed(recent_cmds[-limit:]):
                        if cmd.get("type") == "success":
                            wins.append({
                                "title": cmd.get("details", "")[:50],
                                "type": "win",
                            })
            except Exception as e:
                _logger.debug(f"Failed to read recent wins from capture state: {e}")

    return wins


# =============================================================================
# MAIN MENU BUILDER
# =============================================================================

def get_setup_status():
    """Get setup status from API."""
    result = api_get("/api/setup/status")
    return result if result else {"is_complete": False, "issues": [], "warnings": []}


def get_api_keys_status():
    """Get API keys status from API."""
    result = api_get("/api/keys")
    return result if result else {"keys": {}, "any_configured": False}


def send_notification(title, message, notification_type="info"):
    """Send a system notification via API."""
    api_post("/api/notify", {
        "title": title,
        "message": message,
        "type": notification_type,
    })


def get_ai_setup_help(component, error=None):
    """Get AI-generated setup guidance."""
    result = api_post("/api/setup/ai-help", {
        "component": component,
        "error": error,
    })
    return result.get("guidance", "") if result else ""


def main():
    # Gather all data (with smart fallback for stats)
    stats = get_stats_with_fallback()
    health = run_health_checks()
    brain_systems = check_brain_systems()
    saas_metrics = get_saas_metrics()
    setup_status = get_setup_status()
    api_keys = get_api_keys_status()
    recent_response = api_get("/api/recent?limit=50")
    recent_learnings = recent_response.get("recent", []) if recent_response else []
    user_domains = extract_user_domains(recent_learnings)
    recent_wins = get_recent_wins(5)

    # Calculate health
    all_healthy = all(health.values())
    healthy_count = sum(1 for v in health.values() if v)
    active_systems, total_systems = count_active_systems(brain_systems)
    automation_level = (active_systems / total_systems * 100) if total_systems > 0 else 0

    # ==========================================================================
    # MENU BAR ICON
    # ==========================================================================
    if not health.get("docker"):
        print("🧠 | color=#888888 size=12")
    elif stats:
        total = stats.get("total", 0)
        streak = stats.get("streak", 0)

        # Color based on automation level
        if automation_level >= 80:
            color = "#22c55e"  # Bright green
        elif automation_level >= 50:
            color = "#84cc16"  # Yellow-green
        elif automation_level > 0:
            color = "#f59e0b"  # Orange
        else:
            color = "#666666"  # Gray

        if streak > 0:
            print(f"🧠 {streak}🔥 | color={color} size=12")
        else:
            print(f"🧠 {total} | color={color} size=12")
    else:
        print(f"🧠 | color={'#22c55e' if all_healthy else '#f59e0b'} size=12")

    print("---")

    # ==========================================================================
    # DOCKER CHECK
    # ==========================================================================
    if not health.get("docker"):
        print("Docker not running | color=red")
        print("Start Docker Desktop first | color=gray")
        print("---")
        print("🔧 Start Docker | bash=open param1=-a param2=Docker terminal=false refresh=true")
        return

    # ==========================================================================
    # SETUP ALERTS (Click-to-Configure)
    # ==========================================================================
    if not setup_status.get("is_complete", True):
        issues = setup_status.get("issues", [])
        if issues:
            print("⚠️ Setup Required | color=orange size=12")
            for issue in issues[:3]:  # Show top 3 issues
                issue_name = issue.get("name", "Unknown")
                fix_cmd = issue.get("fix_command", "")
                fix_url = issue.get("fix_url", "")

                print(f"--⚠️ {issue_name} | color=orange")
                print(f"----{issue.get('description', 'Configuration needed')[:50]}... | size=11 color=gray")

                if fix_cmd:
                    # Parse command for xbar
                    parts = fix_cmd.split()
                    if len(parts) >= 2:
                        print(f"----🔧 Fix: {fix_cmd} | bash={parts[0]} param1={' '.join(parts[1:])} terminal=true refresh=true")
                    else:
                        print(f"----🔧 Fix: {fix_cmd} | bash={fix_cmd} terminal=true refresh=true")

                if fix_url:
                    print(f"----🌐 Open Setup Page | href={API_URL}{fix_url}")

                # AI-assisted setup (click copies commands to clipboard)
                print(f"----🤖 Get AI Help | bash=/bin/bash param1=-c param2=context-dna setup --wizard terminal=true")

            print("---")

    # API Keys Status (if any not configured)
    if not api_keys.get("any_configured", True):
        print("🔑 API Keys Needed | color=yellow")
        print("--No LLM provider configured | size=11 color=gray")
        print("--🦙 Setup Ollama (FREE) | bash=context-dna param1=setup param2=--wizard terminal=true")
        print("--🔑 Setup OpenAI | bash=/bin/bash param1=-c param2=open https://platform.openai.com/api-keys terminal=false")
        print("--🔑 Setup Anthropic | bash=/bin/bash param1=-c param2=open https://console.anthropic.com/settings/keys terminal=false")
        print("--📖 Setup Instructions | bash=context-dna param1=setup param2=--help terminal=true")
        print("---")
    else:
        # Show configured keys (masked)
        keys = api_keys.get("keys", {})
        configured = [k for k, v in keys.items() if v]
        if configured:
            # Just show a subtle indicator
            pass  # Keys are configured, no need to show

    # ==========================================================================
    # BRAIN STATUS HEADER
    # ==========================================================================
    if automation_level >= 80:
        print(f"✅ Architecture Brain: {automation_level:.0f}% Automated | color=green")
    elif automation_level >= 50:
        print(f"🟡 Architecture Brain: {automation_level:.0f}% | color=orange")
    else:
        print(f"⚠️ Architecture Brain: Limited | color=orange")

    if stats:
        print(f"Context DNA: {stats.get('total', 0)} learnings | size=11")
    else:
        print("Context DNA: Server offline | size=11 color=gray")

    print("---")

    # ==========================================================================
    # SAAS-STYLE METRICS DASHBOARD
    # ==========================================================================
    print("📊 Today's Stats | size=11")
    print(f"--Captures today: {saas_metrics['captures_today']} | size=11 font=Menlo")
    print(f"--Total wins: {saas_metrics['successes_captured']} | size=11 font=Menlo")
    print(f"--Brain cycles: {saas_metrics['brain_cycles']} | size=11 font=Menlo")
    print(f"--SOPs stored: {saas_metrics['sops_stored']} | size=11 font=Menlo")
    print(f"--Last capture: {saas_metrics['last_capture']} | size=11 font=Menlo")
    if stats:
        print(f"--Streak: {stats.get('streak', 0)} days 🔥 | size=11 font=Menlo")
    print("---")

    # ==========================================================================
    # BRAIN ACTIONS
    # ==========================================================================
    print("🧠 Architecture Brain")
    print("--🔄 Run Brain Cycle | bash=context-dna param1=cycle terminal=true refresh=true")
    print("--📊 View Status | bash=context-dna param1=status terminal=true")
    print("--🎯 Detect Successes | bash=context-dna param1=detect terminal=true")

    # Legacy brain tools if available
    if MEMORY_DIR and check_file(os.path.join(MEMORY_DIR, "brain.py")):
        print("--📈 Legacy Brain State | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "brain.py") + " param2=state terminal=true")

    print("---")

    # ==========================================================================
    # PROFESSOR (Distilled Wisdom) - ADAPTIVE
    # ==========================================================================
    print("🎓 Professor (Ask for Wisdom)")

    # User's top domains for quick access
    if user_domains["tags"]:
        for tag in user_domains["tags"][:5]:
            print(f"--Ask: {tag} | bash=context-dna param1=consult param2=\"{tag} best practices\" terminal=true")
    else:
        # Default suggestions
        print("--Ask: deploy django | bash=context-dna param1=consult param2=\"deploy django\" terminal=true")
        print("--Ask: docker issues | bash=context-dna param1=consult param2=\"docker issues\" terminal=true")
        print("--Ask: async patterns | bash=context-dna param1=consult param2=\"async patterns\" terminal=true")

    print("--Custom query... | bash=/usr/bin/osascript param1=-e param2=set t to text returned of (display dialog \"What do you need wisdom for?\" default answer \"\") param3=-e param4=do shell script \"context-dna consult '\" & t & \"'\" terminal=true")

    # Legacy professor if available
    if MEMORY_DIR and check_file(os.path.join(MEMORY_DIR, "professor.py")):
        print("---")
        print("--Legacy Professor:")
        print("----async boto3 | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "professor.py") + " param2=async param3=boto3 terminal=true")
        print("----voice pipeline | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "professor.py") + " param2=voice param3=pipeline terminal=true")

    print("---")

    # ==========================================================================
    # QUERY MEMORY - ADAPTIVE
    # ==========================================================================
    print("🔍 Query Memory")

    # User's top tags for quick search
    if user_domains["counts"]:
        for tag, count in list(user_domains["counts"].items())[:5]:
            print(f"--{tag} ({count}) | bash=context-dna param1=query param2={tag} terminal=true")
    else:
        # Default searches
        print("--async patterns | bash=context-dna param1=query param2=async terminal=true")
        print("--docker deploy | bash=context-dna param1=query param2=docker terminal=true")
        print("--database | bash=context-dna param1=query param2=database terminal=true")

    print("--Custom search... | bash=/usr/bin/osascript param1=-e param2=set t to text returned of (display dialog \"Search learnings:\" default answer \"\") param3=-e param4=do shell script \"context-dna query '\" & t & \"'\" terminal=true")

    # Legacy query if available
    if MEMORY_DIR and check_file(os.path.join(MEMORY_DIR, "query.py")):
        print("---")
        print("--Legacy Query:")
        print("----terraform aws | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "query.py") + " param2=terraform param3=aws terminal=true")
        print("----voice livekit | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "query.py") + " param2=voice param3=livekit terminal=true")

    print("---")

    # ==========================================================================
    # CONTEXT/BLUEPRINT - ADAPTIVE
    # ==========================================================================
    print("📋 Get Context (Blueprint)")

    if user_domains["tags"]:
        for tag in user_domains["tags"][:4]:
            print(f"--Context: {tag} | bash=context-dna param1=consult param2=\"working with {tag}\" terminal=true")
    else:
        print("--Context: deployment | bash=context-dna param1=consult param2=\"deployment\" terminal=true")
        print("--Context: infrastructure | bash=context-dna param1=consult param2=\"infrastructure\" terminal=true")

    # Legacy context if available
    if MEMORY_DIR and check_file(os.path.join(MEMORY_DIR, "context.py")):
        print("---")
        print("--Legacy Context:")
        print("----ECS deployment | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "context.py") + " param2=ECS param3=deployment terminal=true")
        print("----GPU toggle | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "context.py") + " param2=GPU param3=toggle terminal=true")

    print("---")

    # ==========================================================================
    # QUICK ACTIONS
    # ==========================================================================
    print("➕ Quick Add | size=14")
    print("--🏆 Record Win | bash=/usr/bin/osascript param1=-e param2=set t to text returned of (display dialog \"What win do you want to record?\" default answer \"\") param3=-e param4=do shell script \"context-dna win '\" & t & \"' ''\" terminal=false refresh=true")
    print("--🔧 Record Fix | bash=/usr/bin/osascript param1=-e param2=set t to text returned of (display dialog \"What fix/gotcha do you want to record?\" default answer \"\") param3=-e param4=do shell script \"context-dna fix '\" & t & \"' ''\" terminal=false refresh=true")
    print("--🔄 Record Pattern | bash=/usr/bin/osascript param1=-e param2=set t to text returned of (display dialog \"What pattern do you want to record?\" default answer \"\") param3=-e param4=do shell script \"context-dna pattern '\" & t & \"' ''\" terminal=false refresh=true")

    print("---")

    # ==========================================================================
    # RECENT WINS
    # ==========================================================================
    print("🏆 Recent Wins: | size=11")
    if recent_wins:
        for win in recent_wins[:5]:
            title = win.get("title", "")[:45]
            if len(win.get("title", "")) > 45:
                title += "..."
            print(f"--🎯 {title} | size=11 font=Menlo")
    else:
        print("--No recent wins | size=11 color=gray")
        print("--Start with: context-dna win \"My first win\" | bash=context-dna param1=win param2=\"My first win\" param3=\"\" terminal=true")

    print("---")

    # ==========================================================================
    # HEALTH STATUS
    # ==========================================================================
    health_color = "green" if all_healthy else ("orange" if healthy_count >= 3 else "red")
    print(f"🏥 Health ({healthy_count}/{len(health)}) | color={health_color}")

    service_icons = {"docker": "🐳", "postgres": "🐘", "redis": "🔴", "opensearch": "🔍", "jaeger": "📊", "ollama": "🦙", "server": "🌐"}
    service_names = {"docker": "Docker", "postgres": "PostgreSQL", "redis": "Redis", "opensearch": "OpenSearch", "jaeger": "Jaeger", "ollama": "Ollama (LLM)", "server": "API Server"}

    for service, is_healthy in health.items():
        icon = service_icons.get(service, "•")
        status = "✓" if is_healthy else "✗"
        color = "green" if is_healthy else "red"
        name = service_names.get(service, service)
        print(f"--{icon} {name}: {status} | color={color}")
        if not is_healthy:
            fix_cmd = get_fix_command(service)
            parts = fix_cmd.split()
            print(f"----🔧 Fix | bash={parts[0]} param1={' '.join(parts[1:])} terminal=true refresh=true")

    print("---")

    # ==========================================================================
    # BRAIN CORE SYSTEMS
    # ==========================================================================
    print("🧠 Brain Core: | size=11")
    if "brain_core" in brain_systems:
        for name, exists in brain_systems["brain_core"].items():
            status = "✅" if exists else "❌"
            print(f"--{status} {name} | size=11 font=Menlo")

    if "legacy" in brain_systems:
        print("--Legacy Memory: | size=11")
        for name, exists in list(brain_systems["legacy"].items())[:5]:
            status = "✅" if exists else "❌"
            print(f"----{status} {name} | size=11 font=Menlo")

    print("---")

    # ==========================================================================
    # CAPTURE & LEARNING SYSTEMS
    # ==========================================================================
    print("📥 Capture & Learning: | size=11")
    if "capture" in brain_systems:
        for name, exists in brain_systems["capture"].items():
            status = "✅" if exists else "❌"
            print(f"--{status} {name} | size=11 font=Menlo")

    print("---")

    # ==========================================================================
    # IDE HOOKS STATUS
    # ==========================================================================
    print("🪝 IDE Hooks: | size=11")
    if "hooks" in brain_systems:
        for name, exists in brain_systems["hooks"].items():
            status = "✅" if exists else "❌"
            print(f"--{status} {name} | size=11 font=Menlo")

    print("--Install Claude | bash=context-dna param1=hooks param2=install param3=claude terminal=true refresh=true")
    print("--Install Cursor | bash=context-dna param1=hooks param2=install param3=cursor terminal=true refresh=true")
    print("--Install Git | bash=context-dna param1=hooks param2=install param3=git terminal=true refresh=true")
    print("--Install All | bash=context-dna param1=hooks param2=install param3=all terminal=true refresh=true")

    print("---")

    # ==========================================================================
    # LLM PROVIDERS
    # ==========================================================================
    print("🤖 LLM Providers: | size=11")
    if "llm" in brain_systems:
        for name, exists in brain_systems["llm"].items():
            status = "✅" if exists else "❌"
            print(f"--{status} {name} | size=11 font=Menlo")

    print("---")

    # ==========================================================================
    # RESOURCES & SYSTEM
    # ==========================================================================
    print("⚡ Resources | size=14")
    print("--🔍 Detect System | bash=context-dna param1=resources param2=detect terminal=true")
    print("--📊 View Status | bash=context-dna param1=resources param2=status terminal=true")
    print("-----")
    print("--Apply Profile:")
    print("----🪶 Light (8GB RAM) | bash=context-dna param1=resources param2=apply param3=light terminal=true refresh=true")
    print("----⚖️ Standard (16GB RAM) | bash=context-dna param1=resources param2=apply param3=standard terminal=true refresh=true")
    print("----💪 Heavy (32GB+ RAM) | bash=context-dna param1=resources param2=apply param3=heavy terminal=true refresh=true")

    print("---")

    # ==========================================================================
    # LOCAL LLM (OLLAMA)
    # ==========================================================================
    print("🦙 Local LLM (Ollama) | size=14")
    print("--📊 Status | bash=context-dna param1=llm param2=status terminal=true")
    print("--✅ Enable | bash=context-dna param1=llm param2=enable terminal=true refresh=true")
    print("--📋 List Models | bash=context-dna param1=llm param2=list terminal=true")
    print("-----")
    print("--📥 Pull Model:")
    print("----qwen2.5:3b (2GB, fast) | bash=context-dna param1=llm param2=pull param3=qwen2.5:3b terminal=true")
    print("----qwen2.5:7b (4GB, balanced) | bash=context-dna param1=llm param2=pull param3=qwen2.5:7b terminal=true")
    print("----llama3.2:3b (2GB, chat) | bash=context-dna param1=llm param2=pull param3=llama3.2:3b terminal=true")
    print("----mistral:7b (4GB, code) | bash=context-dna param1=llm param2=pull param3=mistral:7b terminal=true")

    print("---")

    # ==========================================================================
    # DOCKER SERVICES
    # ==========================================================================
    print("🐳 Docker Services | size=14")
    print("--▶️ Start | bash=context-dna param1=up terminal=true refresh=true")
    print("--▶️ Start with LLM | bash=context-dna param1=up param2=--llm terminal=true refresh=true")
    print("--⏹️ Stop | bash=context-dna param1=down terminal=true refresh=true")
    print("--🔄 Restart | bash=context-dna param1=restart terminal=true refresh=true")
    print("--📋 View Logs | bash=context-dna param1=logs terminal=true")
    print("--📦 Pull Updates | bash=context-dna param1=pull terminal=true")

    print("---")

    # ==========================================================================
    # COPY COMMANDS (Clipboard)
    # ==========================================================================
    print("📋 Copy Command")
    print("--Query memory | bash=/bin/bash param1=-c param2=echo \"context-dna query \\\"your search\\\"\" | pbcopy terminal=false")
    print("--Ask professor | bash=/bin/bash param1=-c param2=echo \"context-dna consult \\\"your task\\\"\" | pbcopy terminal=false")
    print("--Capture win | bash=/bin/bash param1=-c param2=echo \"context-dna win \\\"title\\\" \\\"details\\\"\" | pbcopy terminal=false")
    print("--Run cycle | bash=/bin/bash param1=-c param2=echo \"context-dna cycle\" | pbcopy terminal=false")

    print("---")

    # ==========================================================================
    # LINKS - Dashboard Deep Links
    # ==========================================================================
    print("🔗 Open | size=14")
    print("--🏠 Dashboard (Overview) | href=http://localhost:3457")
    print("--🎓 Professor Query | href=http://localhost:3457?tab=professor")
    print("--📊 Activity Feed | href=http://localhost:3457?tab=activity")
    print("--🏥 Health Status | href=http://localhost:3457?tab=health")
    print("-----")
    print("--API Docs | href=http://127.0.0.1:8080/docs")
    if check_url("http://localhost:3000"):
        print("--Context DNA Dashboard | href=http://localhost:3000")
    print("--GitHub | href=https://github.com/supportersimulator/context-dna")

    print("---")

    # ==========================================================================
    # TROUBLESHOOT
    # ==========================================================================
    print("🔧 Troubleshoot & Debug")
    print("--Full Status | bash=context-dna param1=status terminal=true")
    print("--Hook Status | bash=context-dna param1=hooks param2=status terminal=true")
    print("--Detect Health | bash=context-dna param1=detect terminal=true")

    if MEMORY_DIR and check_file(os.path.join(MEMORY_DIR, "troubleshoot.py")):
        print("--Legacy Troubleshoot | bash=" + PYTHON + " param1=" + os.path.join(MEMORY_DIR, "troubleshoot.py") + " terminal=true")

    print("---")

    # ==========================================================================
    # SETUP WIZARD (AI-Assisted Configuration)
    # ==========================================================================
    print("⚙️ Setup & Configuration | size=14")
    print("--🧙 Run Setup Wizard | bash=context-dna param1=setup param2=--wizard terminal=true")
    print("--📋 Check Setup Status | bash=context-dna param1=setup param2=status terminal=true")
    print("-----")
    print("--🔑 Configure API Keys (Secure)")
    print("----OpenAI (via Keychain) | bash=context-dna param1=setup param2=key param3=openai terminal=true")
    print("----Anthropic (via Keychain) | bash=context-dna param1=setup param2=key param3=anthropic terminal=true")
    print("----Ollama (Local - No Key) | bash=context-dna param1=setup param2=ollama terminal=true")
    print("-----")
    print("--🛡️ Security Notes | color=gray")
    print("----⚠️ NEVER share API keys in chat | color=red")
    print("----✓ Keys stored in system keychain | color=green")
    print("----✓ Secrets auto-sanitized before storage | color=green")

    print("---")

    # ==========================================================================
    # NOTIFICATIONS
    # ==========================================================================
    print("🔔 Notifications")
    print("--Test Notification | bash=context-dna param1=notify param2=test terminal=false")
    print("--Enable Setup Alerts | bash=context-dna param1=notify param2=enable terminal=false")
    print("--Disable Setup Alerts | bash=context-dna param1=notify param2=disable terminal=false")

    print("---")
    print("Context DNA v2.0.0 | size=10 color=gray")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
