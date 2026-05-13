#!/bin/bash
# SUPERHERO MODE: Synaptic 8th Intelligence Query
# Usage: ./scripts/ask-synaptic.sh [OPTIONS] "what you're working on"
#
# Options:
#   --midtask      Compact format for mid-task queries
#   --format [compact|full|gotchas]  Output format
#   --agent-id ID  Agent identifier
#   --pretty       Pretty-print JSON
#
# Returns JSON with:
#   - patterns: Past work patterns
#   - gotchas: Warnings to avoid
#   - intuitions: Professor's wisdom
#   - stop_signal: If set, STOP and verify

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python3"

SUBTASK=""
AGENT_ID="agent-$(date +%s)"
FORMAT="full"
PRETTY=0

# Parse flags
while [[ $# -gt 0 ]]; do
    case $1 in
        --midtask)
            FORMAT="midtask"
            shift
            ;;
        --format)
            FORMAT="$2"
            shift 2
            ;;
        --agent-id)
            AGENT_ID="$2"
            shift 2
            ;;
        --pretty)
            PRETTY=1
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS] \"task description\""
            echo ""
            echo "Options:"
            echo "  --midtask         Compact format for mid-task queries"
            echo "  --format FORMAT   Output format: compact|full|gotchas|midtask"
            echo "  --agent-id ID     Agent identifier (default: agent-timestamp)"
            echo "  --pretty          Pretty-print JSON output"
            echo "  -h, --help        Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0 \"deploying terraform\""
            echo "  $0 --midtask \"WebRTC connection failing\""
            echo "  $0 --format gotchas \"about to delete production\""
            echo "  $0 --pretty --agent-id atlas \"checking GPU status\""
            exit 0
            ;;
        -*)
            echo "Unknown option: $1"
            shift
            ;;
        *)
            SUBTASK="$1"
            shift
            ;;
    esac
done

if [ -z "$SUBTASK" ]; then
    echo "Usage: $0 [OPTIONS] \"task description\""
    echo "Example: $0 --midtask --format gotchas \"deploying terraform\""
    echo "Run '$0 --help' for more options."
    exit 1
fi

# URL encode the subtask
ENCODED=$("$PYTHON" -c "import urllib.parse; print(urllib.parse.quote('$SUBTASK'))")

# Query Synaptic's 8th Intelligence
RESPONSE=$(curl -s -X POST \
    "http://localhost:8080/contextdna/8th-intelligence?subtask=$ENCODED&agent_id=$AGENT_ID" 2>/dev/null)

# Check if curl failed or empty response
if [ -z "$RESPONSE" ]; then
    echo "Service unavailable (no response from localhost:8080)"
    exit 1
fi

# Format output based on selected format
if [ "$PRETTY" = "1" ]; then
    echo "$RESPONSE" | "$PYTHON" -m json.tool 2>/dev/null || echo "$RESPONSE"
elif [ "$FORMAT" = "midtask" ] || [ "$FORMAT" = "compact" ]; then
    # Extract stop_signal, gotchas, patterns in compact format
    "$PYTHON" << PYEOF
import sys, json
try:
    data = json.loads('''$RESPONSE''')
    syn = data.get('synaptic_response', {})

    if syn.get('stop_signal'):
        print(f"STOP: {syn['stop_signal']}")
        sys.exit(0)

    output = []

    if syn.get('gotchas'):
        print("Gotchas:")
        for g in syn['gotchas'][:3]:
            print(f"  - {g}")

    if syn.get('patterns'):
        print("Patterns:")
        for p in syn['patterns'][:2]:
            text = str(p)[:70]
            if len(str(p)) > 70:
                text += "..."
            print(f"  - {text}")

    if syn.get('intuitions'):
        print("Intuitions:")
        for i in syn['intuitions'][:2]:
            text = str(i)[:70]
            if len(str(i)) > 70:
                text += "..."
            print(f"  - {text}")

    conf = syn.get('confidence', 0)
    if conf:
        print(f"Confidence: {conf}")

except json.JSONDecodeError:
    print('Service unavailable (invalid JSON)')
except Exception as e:
    print(f'Service unavailable ({e})')
PYEOF
elif [ "$FORMAT" = "gotchas" ]; then
    # Only gotchas
    "$PYTHON" << PYEOF
import sys, json
try:
    data = json.loads('''$RESPONSE''')
    gotchas = data.get('synaptic_response', {}).get('gotchas', [])
    if gotchas:
        for g in gotchas:
            print(f"  {g}")
    else:
        print("No gotchas found for this task")
except:
    print('Service unavailable')
PYEOF
else
    # Full JSON (default)
    echo "$RESPONSE"
fi

# Example usage in agent:
# SYNAPTIC=$(./scripts/ask-synaptic.sh "deploying terraform")
# STOP_SIGNAL=$(echo "$SYNAPTIC" | python3 -c "
# import sys,json
# decoder = json.JSONDecoder(strict=False)  # Python 3.14 compatibility
# data = decoder.decode(sys.stdin.read())
# print(data.get('synaptic_response',{}).get('stop_signal','') or '')
# ")
# if [ -n "$STOP_SIGNAL" ]; then echo "STOP: $STOP_SIGNAL"; exit 1; fi
