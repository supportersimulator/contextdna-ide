#!/bin/bash
# Focus Toggle — manages which sub-repos Atlas can access
# Usage:
#   ./scripts/focus.sh status          — show current focus
#   ./scripts/focus.sh on <repo>       — activate a sub-repo
#   ./scripts/focus.sh off <repo>      — deactivate a sub-repo
#   ./scripts/focus.sh only <repo>     — activate ONLY this repo (deactivate others)
#   ./scripts/focus.sh list            — list available repos
#   ./scripts/focus.sh apply           — regenerate deny rules from focus.json

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python3"
FOCUS_FILE="$REPO_ROOT/.claude/focus.json"
SETTINGS_FILE="$REPO_ROOT/.claude/settings.local.json"

if [ ! -f "$FOCUS_FILE" ]; then
  echo "ERROR: $FOCUS_FILE not found"
  exit 1
fi

# Read focus.json with python (available everywhere)
read_focus() {
  "$PYTHON" -c "
import json, sys
with open('$FOCUS_FILE') as f:
    data = json.load(f)
$1
"
}

case "${1:-status}" in
  status)
    echo "=== FOCUS STATUS ==="
    echo ""
    echo "Core (always active):"
    read_focus "print('  ' + ', '.join(data['core_always_active']))"
    echo ""
    echo "Active:"
    read_focus "
active = data.get('active', [])
if active:
    for r in active:
        desc = data.get('available', {}).get(r, '')
        print(f'  {r} — {desc}' if desc else f'  {r}')
else:
    print('  (none)')
"
    echo ""
    echo "Inactive (denied):"
    read_focus "
active = set(data.get('active', []))
core = set(data.get('core_always_active', []))
for repo, desc in sorted(data.get('available', {}).items()):
    if repo not in active and repo not in core:
        print(f'  {repo} — {desc}')
"
    ;;

  on)
    # Single-project focus: replaces current active with new repo
    if [ -z "$2" ]; then echo "Usage: focus.sh on <repo>"; exit 1; fi
    "$PYTHON" -c "
import json
with open('$FOCUS_FILE') as f:
    data = json.load(f)
repo = '$2'
if repo not in data.get('available', {}):
    print(f'Unknown repo: {repo}')
    print('Available: ' + ', '.join(sorted(data['available'].keys())))
    exit(1)
old = data.get('active', [])
data['active'] = [repo]
with open('$FOCUS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
if old and old != [repo]:
    s = ', '.join(old)
    print(f'Switched: {s} -> {repo}')
else:
    print(f'Focus: {repo}')
"
    "$0" apply
    ;;

  add)
    # Multi-project: adds repo WITHOUT removing current (rare, explicit only)
    if [ -z "$2" ]; then echo "Usage: focus.sh add <repo>"; exit 1; fi
    "$PYTHON" -c "
import json
with open('$FOCUS_FILE') as f:
    data = json.load(f)
repo = '$2'
if repo not in data.get('available', {}):
    print(f'Unknown repo: {repo}')
    print('Available: ' + ', '.join(sorted(data['available'].keys())))
    exit(1)
if repo not in data.get('active', []):
    data.setdefault('active', []).append(repo)
    with open('$FOCUS_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    s = ', '.join(data['active'])
    print(f'Added: {repo} (now active: {s})')
else:
    print(f'Already active: {repo}')
"
    "$0" apply
    ;;

  off)
    if [ -z "$2" ]; then echo "Usage: focus.sh off <repo>"; exit 1; fi
    "$PYTHON" -c "
import json
with open('$FOCUS_FILE') as f:
    data = json.load(f)
repo = '$2'
if repo in data.get('active', []):
    data['active'].remove(repo)
    with open('$FOCUS_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    print(f'Deactivated: {repo}')
else:
    print(f'Not active: {repo}')
"
    "$0" apply
    ;;

  only)
    if [ -z "$2" ]; then echo "Usage: focus.sh only <repo>"; exit 1; fi
    "$PYTHON" -c "
import json
with open('$FOCUS_FILE') as f:
    data = json.load(f)
repo = '$2'
if repo not in data.get('available', {}):
    print(f'Unknown repo: {repo}')
    print('Available: ' + ', '.join(sorted(data['available'].keys())))
    exit(1)
data['active'] = [repo]
with open('$FOCUS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print(f'Focus set to ONLY: {repo}')
"
    "$0" apply
    ;;

  list)
    read_focus "
for repo, desc in sorted(data.get('available', {}).items()):
    active = '  *' if repo in data.get('active', []) else '   '
    print(f'{active} {repo} — {desc}')
"
    ;;

  apply)
    "$PYTHON" -c "
import json

with open('$FOCUS_FILE') as f:
    focus = json.load(f)
with open('$SETTINGS_FILE') as f:
    settings = json.load(f)

core = set(focus.get('core_always_active', []))
active = set(focus.get('active', []))
available = focus.get('available', {})

# Base deny rules (always present — heavy dirs)
base_deny = [
    'Read(**/node_modules/**)',
    'Read(**/.next/**)',
    'Read(**/dist/**)',
    'Read(**/build/**)',
    'Read(**/coverage/**)',
    'Read(**/__pycache__/**)',
    'Read(**/.venv*/**)',
    'Read(**/.git/objects/**)',
    'Read(**/context-dna-data-OLD/**)',
    'Read(**/session-archive/**)',
    'Read(**/*.pyc)',
    'Read(**/*.pyo)',
    'Read(**/*.whl)',
    'Read(**/*.egg-info/**)',
    'Read(**/.terraform/**)',
    'Read(**/.cache/**)',
]

# Add deny rules for inactive repos
inactive_deny = []
for repo in sorted(available.keys()):
    if repo not in active and repo not in core:
        inactive_deny.append(f'Read(**/{repo}/**)')

settings['permissions']['deny'] = base_deny + inactive_deny

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)

print(f'Applied: {len(active)} active, {len(inactive_deny)} denied repos')
print(f'Total deny rules: {len(base_deny)} base + {len(inactive_deny)} repos = {len(base_deny) + len(inactive_deny)}')
"
    ;;

  *)
    echo "Usage: focus.sh {status|on|off|only|list|apply} [repo]"
    exit 1
    ;;
esac
