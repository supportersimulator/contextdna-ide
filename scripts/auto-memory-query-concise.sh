#!/bin/bash
# Auto-Memory Query Hook - CONCISE VARIANT
# Implements improvements based on agent feedback:
# 1. Keyword extraction (not raw prompt)
# 2. Relevance threshold (>40% only)
# 3. Minimal formatting (bullets, no ASCII boxes)
# 4. Action-focused protocols

set -e

REPO_DIR="${CONTEXT_DNA_REPO:-${CLAUDE_PROJECT_DIR:-$HOME/dev/er-simulator-superrepo}}"
CONTEXT_DNA_DIR="${CONTEXT_DNA_DIR:-$HOME/.context-dna}"
PYTHON="${CONTEXT_DNA_PYTHON:-$REPO_DIR/.venv/bin/python3}"
QUERY_SCRIPT="$REPO_DIR/memory/query.py"
PROFESSOR_SCRIPT="$REPO_DIR/memory/professor.py"
SESSION_ID="${CLAUDE_SESSION_ID:-$(date +%Y%m%d_%H%M%S)_$$}"

mkdir -p "$CONTEXT_DNA_DIR"

# Read prompt
INPUT=$(cat)
PROMPT=$("$PYTHON" -c "
import sys, json
try:
    data = json.loads('''$INPUT''')
    print(data.get('prompt', data.get('message', '')))
except:
    print('''$INPUT''')
" 2>/dev/null || echo "$INPUT")

[ -z "$PROMPT" ] && exit 0

# =============================================================================
# IMPROVEMENT 1: KEYWORD EXTRACTION (not raw prompt)
# =============================================================================
KEYWORDS=$("$PYTHON" -c "
import re
prompt = '''$PROMPT'''

# Remove common words, extract meaningful terms
stopwords = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
             'do','does','did','will','would','could','should','may','might','must','shall',
             'can','need','dare','ought','used','to','of','in','for','on','with','at','by',
             'from','as','into','through','during','before','after','above','below','between',
             'under','again','further','then','once','here','there','when','where','why','how',
             'all','each','few','more','most','other','some','such','no','nor','not','only',
             'own','same','so','than','too','very','just','also','now','this','that','these',
             'those','i','me','my','we','our','you','your','he','him','his','she','her','it',
             'its','they','them','their','what','which','who','whom','and','but','if','or',
             'because','until','while','please','can','help','want','like','make','get','let'}

# Extract words, filter stopwords, keep meaningful terms
words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', prompt.lower())
keywords = [w for w in words if w not in stopwords and len(w) > 2]

# Dedupe while preserving order, limit to top 8
seen = set()
unique = []
for w in keywords:
    if w not in seen:
        seen.add(w)
        unique.append(w)
        if len(unique) >= 8:
            break

print(' '.join(unique))
" 2>/dev/null)

# =============================================================================
# RISK DETECTION
# =============================================================================
CRITICAL_RISK="destroy|migration.*prod|schema.*change|auth.*system|delete.*prod|force.*push|rollback|permission.*change|iam.*policy|ssl.*cert|dns.*record|database.*alter|drop.*table"
HIGH_RISK="deploy|terraform|migration|refactor|ecs.*service|lambda.*deploy|database|nginx.*config|cloudflare|subnet|security.*group|load.*balancer|asg|autoscaling"
MODERATE_RISK="docker|config|env|toggle|health|sync|api|endpoint|websocket|livekit|bedrock|gpu|stt|tts|llm|voice|webrtc|hook|memory|context"
LOW_RISK="admin|dashboard|display|show|add.*button|style|color|text|label|readme|docs|comment|log"

RISK_LEVEL="none"
if echo "$PROMPT" | grep -qiE "($CRITICAL_RISK)"; then
    RISK_LEVEL="critical"
elif echo "$PROMPT" | grep -qiE "($HIGH_RISK)"; then
    RISK_LEVEL="high"
elif echo "$PROMPT" | grep -qiE "($MODERATE_RISK)"; then
    RISK_LEVEL="moderate"
elif echo "$PROMPT" | grep -qiE "($LOW_RISK)"; then
    RISK_LEVEL="low"
fi

[ "$RISK_LEVEL" = "none" ] && exit 0

# =============================================================================
# SUCCESS CAPTURE (concise)
# =============================================================================
SUCCESS_KEYWORDS="success|worked|perfect|excellent|awesome|great|looks good|nailed it|exactly|done|fixed|solved"
if echo "$PROMPT" | grep -qiE "($SUCCESS_KEYWORDS)"; then
    echo ""
    echo "--- SUCCESS DETECTED ---"
    echo "Capture: python memory/brain.py success \"<task>\" \"<insight>\""
    echo ""
fi

# =============================================================================
# IMPROVEMENT 2: RELEVANCE THRESHOLD (>40% only)
# =============================================================================
QUERY_RESULTS=$("$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')

keywords = '$KEYWORDS'
if not keywords.strip():
    sys.exit(0)

try:
    from memory.context_dna_client import ContextDNAClient
    client = ContextDNAClient()
    results = client.query(keywords, limit=10)

    # Filter to >40% relevance and format concisely
    for r in results:
        relevance = r.get('relevance', 0)
        if relevance >= 0.40:
            title = r.get('title', '')[:60]
            key = r.get('key_insight', r.get('content', ''))[:80]
            print(f'- [{relevance:.0%}] {title}')
            if key:
                print(f'  Key: {key}')
except Exception as e:
    # Fallback to query.py
    pass
" 2>/dev/null)

# =============================================================================
# OUTPUT (concise format)
# =============================================================================
case "$RISK_LEVEL" in
    "critical") RISK_EMOJI="!!!" ; SUCCESS_RATE="~5%" ;;
    "high")     RISK_EMOJI="!!"  ; SUCCESS_RATE="~30%" ;;
    "moderate") RISK_EMOJI="!"   ; SUCCESS_RATE="~60%" ;;
    "low")      RISK_EMOJI="."   ; SUCCESS_RATE="~90%" ;;
esac

echo ""
echo "[$RISK_EMOJI] $RISK_LEVEL risk | First-try: $SUCCESS_RATE | Keywords: $KEYWORDS"

# Professor guidance (concise - just the critical parts)
if [ "$RISK_LEVEL" = "critical" ] || [ "$RISK_LEVEL" = "high" ] || [ "$RISK_LEVEL" = "moderate" ]; then
    PROFESSOR=$("$PYTHON" "$PROFESSOR_SCRIPT" "$KEYWORDS" 2>/dev/null | head -40)

    # Extract just THE ONE THING and LANDMINES (the most useful parts)
    ONE_THING=$(echo "$PROFESSOR" | grep -A3 "THE ONE THING" | tail -3 | head -2)
    LANDMINES=$(echo "$PROFESSOR" | grep -A5 "LANDMINES" | grep "^  " | head -3)

    if [ -n "$ONE_THING" ] || [ -n "$LANDMINES" ]; then
        echo ""
        if [ -n "$ONE_THING" ]; then
            echo "KEY: $ONE_THING"
        fi
        if [ -n "$LANDMINES" ]; then
            echo "AVOID:"
            echo "$LANDMINES" | sed 's/^  /  - /'
        fi
    fi
fi

# Relevant learnings (only if >40% match found)
if [ -n "$QUERY_RESULTS" ]; then
    echo ""
    echo "RELEVANT:"
    echo "$QUERY_RESULTS"
fi

# Protocol (ultra-concise)
echo ""
case "$RISK_LEVEL" in
    "critical")
        echo "PROTOCOL: Read context | Verify prereqs | Plan steps | Test first | Know rollback"
        ;;
    "high")
        echo "PROTOCOL: Review context | Check gotchas | Query deeper if uncertain"
        ;;
    "moderate")
        echo "PROTOCOL: Quick review | Note gotchas | Proceed carefully"
        ;;
    "low")
        echo "OK: Proceed normally"
        ;;
esac
echo ""

exit 0
