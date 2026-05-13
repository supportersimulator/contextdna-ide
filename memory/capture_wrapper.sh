#!/bin/bash
# Wrapper to capture infrastructure commands for the Architecture Brain
# Usage: source this file, then use 'capture_cmd' instead of running commands directly

REPO_DIR="$HOME/dev/er-simulator-superrepo"
PYTHON="$REPO_DIR/.venv/bin/python3"
WORK_LOG="$REPO_DIR/memory/.work_log.jsonl"

# Ensure work log exists
touch "$WORK_LOG"

capture_cmd() {
    local cmd="$*"
    local start_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Execute command and capture output
    local output
    output=$(eval "$cmd" 2>&1)
    local exit_code=$?

    # Log to work log (JSONL format)
    local entry=$(cat <<EOF
{"timestamp": "$start_time", "entry_type": "command", "source": "atlas", "content": "$cmd", "exit_code": $exit_code}
EOF
)
    echo "$entry" >> "$WORK_LOG"

    # Also log output as observation
    if [ -n "$output" ]; then
        local obs_entry=$(cat <<EOF
{"timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")", "entry_type": "observation", "source": "system", "content": "$(echo "$output" | head -c 500 | sed 's/"/\\"/g' | tr '\n' ' ')"}
EOF
)
        echo "$obs_entry" >> "$WORK_LOG"
    fi

    # Print output to user
    echo "$output"
    return $exit_code
}

# Log user dialogue
capture_user() {
    local message="$*"
    local entry=$(cat <<EOF
{"timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")", "entry_type": "dialogue", "source": "user", "content": "$message"}
EOF
)
    echo "$entry" >> "$WORK_LOG"
}

echo "✅ Capture wrapper loaded. Use 'capture_cmd <command>' to log infrastructure commands."
