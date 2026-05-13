#!/bin/bash
# RTK Fleet Installer — install on all fleet nodes for 60-90% token savings
# Usage: bash scripts/install-rtk-fleet.sh
set -euo pipefail

echo "╔══════════════════════════════════════════════╗"
echo "║  RTK Fleet Installer — Token Savings 60-90%  ║"
echo "╚══════════════════════════════════════════════╝"

# Step 1: Install RTK
echo ""
echo "━━━ Step 1: Install RTK ━━━"
if command -v rtk &>/dev/null; then
    echo "  RTK already installed: $(rtk --version)"
else
    if command -v brew &>/dev/null; then
        brew install rtk
    else
        echo "  ERROR: brew not found. Install Homebrew first."
        exit 1
    fi
fi

# Step 2: Initialize hooks
echo ""
echo "━━━ Step 2: Initialize Claude Code hooks ━━━"
rtk init -g 2>&1 || true

# Step 3: Add PreToolUse hook to settings.json
echo ""
echo "━━━ Step 3: Configure settings.json ━━━"
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    # Check if PreToolUse already has RTK
    if grep -q "rtk-rewrite" "$SETTINGS" 2>/dev/null; then
        echo "  RTK hook already in settings.json"
    else
        echo "  Adding RTK PreToolUse hook..."
        python3 -c "
import json
with open('$SETTINGS') as f:
    d = json.load(f)
hooks = d.setdefault('hooks', {})
pre = hooks.get('PreToolUse', [])
# Check if already present
if not any('rtk-rewrite' in str(h) for h in pre):
    pre.append({
        'matcher': 'Bash',
        'hooks': [{
            'type': 'command',
            'command': '$HOME/.claude/hooks/rtk-rewrite.sh'
        }]
    })
    hooks['PreToolUse'] = pre
    with open('$SETTINGS', 'w') as f:
        json.dump(d, f, indent=2)
    print('  RTK hook added to settings.json')
else:
    print('  RTK hook already present')
"
    fi
else
    echo "  WARNING: $SETTINGS not found. Create it manually."
fi

# Step 4: Create fleet-optimized filters
echo ""
echo "━━━ Step 4: Fleet-optimized filters ━━━"
FILTER_DIR="$HOME/Library/Application Support/rtk"
mkdir -p "$FILTER_DIR"
cat > "$FILTER_DIR/filters.toml" << 'FILTERS'
# RTK Custom Filters — ContextDNA Multi-Fleet
[filters.fleet-health]
pattern = "curl.*8855/health"
strip_keys = ["sessions", "stats.started_at"]
collapse_arrays = true

[filters.fleet-cli]
pattern = "fleet (status|race|doctor|gains-gate|evidence)"
strip_ansi = true
collapse_repeated = true

[filters.pytest]
pattern = "pytest|python3 -m pytest"
strip_ansi = true
collapse_passed = true
max_lines = 30

[filters.git-log]
pattern = "git log"
max_lines = 20

[filters.pip-install]
pattern = "pip.*install"
show_last = 3
FILTERS
echo "  Fleet filters installed"

# Step 5: Verify
echo ""
echo "━━━ Step 5: Verify ━━━"
echo "  RTK version: $(rtk --version)"
echo "  Hook file: $(ls -la $HOME/.claude/hooks/rtk-rewrite.sh 2>/dev/null | awk '{print $NF}' || echo 'MISSING')"
echo "  Filters: $FILTER_DIR/filters.toml"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  RTK installed! Restart VS Code to activate. ║"
echo "║  Test: git status (should show compressed)   ║"
echo "║  Verify: rtk gain (shows savings stats)      ║"
echo "╚══════════════════════════════════════════════╝"
