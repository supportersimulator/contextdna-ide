#!/usr/bin/env bash
# validate-aaron-runbooks.sh — sanity-check Aaron paste-runbooks before he pastes them.
#
# Walks every docs/runbooks/*.md, extracts shell commands from ```bash fences,
# classifies each as dry-run-safe / destructive / broken / unknown, and
# executes the dry-run-safe ones under a 30s timeout. Aggregates a JSON
# report and prints a per-runbook PASS / WARN / FAIL summary.
#
# AAA4 — 2026-05-12.
#
# Posture:
#   - NEVER executes destructive commands (kaggle submit, git push, vercel,
#     launchctl, twine upload, brew install, rm, mv, sudo, kill, security
#     add-generic-password, --apply, --enable, --disable, --restart).
#   - NEVER calls any LLM.
#   - ZSF: every probe (extract, classify, execute) captured with exit code +
#     stderr. Errors increment counters that surface in the summary; nothing
#     is swallowed.
#   - Cost: $0.
#   - 30s timeout per executed command.
#
# Usage:
#   bash scripts/validate-aaron-runbooks.sh [--json OUT] [--runbooks DIR]
#                                           [--no-execute] [--quiet]
#
# Exit codes:
#   0 = every runbook PASS or WARN (no broken commands found)
#   2 = at least one runbook FAIL (broken commands found)
#   3 = invocation / IO error

set -uo pipefail

# ---- CLI ---------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNBOOKS_DIR="${REPO_ROOT}/docs/runbooks"
OUT_JSON=""
EXECUTE=1
QUIET=0
PER_CMD_TIMEOUT=30

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)        OUT_JSON="${2:-}"; shift 2 ;;
        --runbooks)    RUNBOOKS_DIR="${2:-}"; shift 2 ;;
        --no-execute)  EXECUTE=0; shift ;;
        --quiet)       QUIET=1; shift ;;
        --timeout)     PER_CMD_TIMEOUT="${2:-30}"; shift 2 ;;
        -h|--help)
            sed -n '1,30p' "$0"
            exit 0 ;;
        *)
            echo "validate-aaron-runbooks: unknown arg: $1" >&2
            exit 3 ;;
    esac
done

if [[ ! -d "$RUNBOOKS_DIR" ]]; then
    echo "validate-aaron-runbooks: runbooks dir not found: $RUNBOOKS_DIR" >&2
    exit 3
fi

# ---- ZSF counters ------------------------------------------------------------

ZSF_EXTRACT_ERRORS=0
ZSF_CLASSIFY_ERRORS=0
ZSF_EXECUTE_ERRORS=0

# ---- Helpers -----------------------------------------------------------------

# json_escape: best-effort JSON string escape (handles \ " and control chars).
json_escape() {
    python3 - <<'PY' "$1"
import json, sys
sys.stdout.write(json.dumps(sys.argv[1]))
PY
}

# extract_bash_blocks: print every shell command in the runbook to stdout, one
# per line, prefixed by line number. Multi-line continuations are joined
# with literal '\n' so the classifier can still see ordering. Commands that
# start with '#' or are blank are skipped.
extract_bash_blocks() {
    local file="$1"
    python3 - "$file" <<'PY'
import re, sys, os

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
except OSError as e:
    sys.stderr.write(f"extract-error: {path}: {e}\n")
    sys.exit(1)

lines = text.splitlines()
in_block = False
block_start = 0
block_lines = []

def flush(block_start, block_lines):
    # Join line-continuations and split into commands by newlines that
    # aren't inside a continuation. Output one logical command per line.
    raw = "\n".join(block_lines)
    # Collapse backslash-newline continuations to a single space.
    raw = re.sub(r"\\\n\s*", " ", raw)
    # Re-fold lines that are inside an unclosed double-quote (covers
    # python3 -c "..." blocks that span multiple lines). Heredocs (<<EOF...EOF)
    # left as-is — extractor flags as unknown anyway.
    folded = []
    accum = ""
    in_dquote = False
    for ln in raw.splitlines():
        # Count unescaped " on this line to flip state.
        # (Best-effort — does not handle nested $(...) escaping.)
        cleaned = re.sub(r'\\\"', '', ln)
        for ch in cleaned:
            if ch == '"':
                in_dquote = not in_dquote
        if accum:
            accum += "\n" + ln
        else:
            accum = ln
        if not in_dquote:
            folded.append(accum)
            accum = ""
    if accum:
        folded.append(accum)
    for cmd_raw in folded:
        # Strip leading '> ' from quoted prereq blocks (first line only).
        first_nl = cmd_raw.find("\n")
        head = cmd_raw if first_nl == -1 else cmd_raw[:first_nl]
        rest = "" if first_nl == -1 else cmd_raw[first_nl:]
        head = head.lstrip()
        if head.startswith("> "):
            head = head[2:].lstrip()
        # Skip blanks, comments, fence remnants
        if not head or head.startswith("#"):
            continue
        # Skip pasted shell prompts (e.g. "$ ")
        if head.startswith("$ "):
            head = head[2:].lstrip()
        # Skip lines that are pure heredoc / python prompts
        if head.startswith(">>> ") or head.startswith("... "):
            continue
        # Fold internal newlines to spaces for single-line classify/execute.
        cmd = (head + rest).replace("\n", " ").strip()
        sys.stdout.write(f"{block_start}\t{cmd}\n")

for i, ln in enumerate(lines, 1):
    stripped = ln.strip()
    if stripped.startswith("```bash") or stripped == "```sh":
        in_block = True
        block_start = i + 1
        block_lines = []
        continue
    if in_block and stripped.startswith("```"):
        flush(block_start, block_lines)
        in_block = False
        continue
    if in_block:
        block_lines.append(ln)

if in_block:
    sys.stderr.write(f"extract-warn: {path}: unterminated ```bash fence\n")
    flush(block_start, block_lines)
PY
}

# classify_cmd <cmd> -> emits one of: dry|safe|destructive|broken|unknown
# plus a reason on a tab-separated line.
classify_cmd() {
    local cmd="$1"
    python3 - "$cmd" "$REPO_ROOT" <<'PY'
import os, re, shlex, sys

cmd, repo_root = sys.argv[1], sys.argv[2]

DESTRUCTIVE_NEEDLES = [
    # External writes / paid actions
    "kaggle competitions submit",
    "twine upload",
    "vercel --prod",
    "vercel env add",
    "vercel env rm",
    "vercel logout",
    "vercel login",
    "brew install",
    "tailscale up",
    "tailscale logout",
    "sudo ",
    # Local state mutations
    "launchctl kickstart",
    "launchctl load",
    "launchctl unload",
    "kill -HUP",
    "kill -9",
    "rm -rf",
    "rm -r",
    "mv ",
    "security add-generic-password",
    # Git network ops
    "git push",
    "git commit",
    "git revert",
    "git add",
    # Mutating flags on our own scripts
    "--apply",
    "--enable",
    "--disable",
    "--restart",
]

# Verbs that are inherently read-only — short-list to bias toward safe.
SAFE_PREFIXES = [
    "ls ", "ls\t", "ls\n",
    "cat ", "head ", "tail ", "grep ", "wc ", "awk ", "sed -n",
    "echo ", "printf ",
    "which ", "command -v",
    "curl -sf", "curl -s ", "curl -sfo", "curl -sfI", "curl -I",
    "python3 -c", "python3 -m json.tool",
    "git status", "git log", "git remote", "git diff", "git fetch",
    "git branch", "git rev-parse",
    "kaggle competitions list",
    "kaggle competitions submissions",
    "vercel whoami",
    "tailscale status",
    "shasum", "sha256sum",
    "test ", "[ ",
]

def emit(kind, reason):
    sys.stdout.write(f"{kind}\t{reason}\n")
    sys.exit(0)

low = cmd.strip()

# Skip interactive markdown placeholders (e.g. "Reply with the single word READY.")
if not low or low.startswith("Reply ") or low.startswith("paste "):
    emit("note", "non-shell instruction")

# Strip env-var prefix(es): VAR=value VAR2=value <cmd...>
work = low
while True:
    m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*=("[^"]*"|\'[^\']*\'|\S+)\s+(.*)', work)
    if not m:
        break
    work = m.group(2)

# Strip leading sudo (handled as destructive elsewhere but be defensive)
if work.startswith("export "):
    emit("safe", "shell builtin: export (no side effects outside shell)")

# Destructive needles first — these win even if --dry-run appears later
for needle in DESTRUCTIVE_NEEDLES:
    if needle in cmd:
        # Exception: explicit --dry-run flag flips writes back to safe
        if "--dry-run" in cmd and not needle.startswith(("kaggle competitions submit",
                                                          "twine upload",
                                                          "vercel ",
                                                          "brew install",
                                                          "tailscale up",
                                                          "tailscale logout",
                                                          "sudo ",
                                                          "launchctl ",
                                                          "kill ",
                                                          "rm ",
                                                          "mv ",
                                                          "security add",
                                                          "git push",
                                                          "git commit",
                                                          "git revert",
                                                          "git add")):
            emit("dry", f"writes inferred from '{needle}', overridden by --dry-run")
        emit("destructive", f"matched '{needle}'")

# Broken-script detection FIRST — even --dry-run can't save a missing script.
def _resolve(rel):
    """Return list of candidate absolute paths to check for existence."""
    cands = []
    if rel.startswith("~"):
        cands.append(os.path.expanduser(rel))
    if rel.startswith("/"):
        cands.append(rel)
    else:
        cands.append(rel)
        cands.append(os.path.normpath(os.path.join(repo_root, rel)))
    return cands

# Detect calls to our own scripts via interpreter wrapper.
# Tolerates leading env-var assignments stripped above.
m = re.match(r'^(?:bash|sh|python3?)\s+(\S+\.(?:sh|py))', work)
if m:
    rel = m.group(1)
    if not any(os.path.exists(p) for p in _resolve(rel)):
        # Skip bare `baseline.py` style — could be relative to a prior cd.
        if "/" in rel or rel.startswith("~"):
            emit("broken", f"script not found: {rel}")

# Direct path to a script (e.g. "./scripts/foo.sh" or "scripts/foo.sh ...")
m = re.match(r'^(\./|~/|/)?(\S*scripts/\S+\.(?:sh|py))', work)
if m:
    rel = (m.group(1) or "") + m.group(2)
    if not any(os.path.exists(p) for p in _resolve(rel)):
        emit("broken", f"script not found: {rel}")

# --dry-run flag → safe to execute
if "--dry-run" in cmd:
    emit("dry", "has --dry-run flag")

# Read-only prefixes
for pfx in SAFE_PREFIXES:
    if work.startswith(pfx) or low.startswith(pfx):
        emit("safe", f"read-only prefix: '{pfx.strip()}'")

# Heuristic: pure pipeline of safe verbs
if re.match(r'^(curl|grep|head|tail|cat|wc|sort|uniq|python3|awk|sed|jq|ls|echo)\b', work):
    if " > " not in work and " >> " not in work:
        emit("safe", "starts with read-only verb, no redirect")

# Path probes — `ls -la file`, `test -f`, `[ -f file ]`
if re.match(r'^\[\s+-[fdwerx]\s', work) or work.startswith("test -"):
    emit("safe", "filesystem test, no writes")

# Unknown — neither obviously safe nor obviously destructive.
emit("unknown", "no classifier rule matched")
PY
}

# execute_cmd: run a command with a hard timeout and capture exit + stderr.
# Echoes "EXIT\t<rc>\nSTDERR\t<first 200 chars of stderr>".
execute_cmd() {
    local cmd="$1"
    local timeout="$2"
    local stderr_file
    stderr_file="$(mktemp)"
    # We DO NOT cd. Commands that need cwd MUST cd themselves. This means
    # `cd /some/path && cmd` patterns work; bare commands run from $REPO_ROOT.
    (
        cd "$REPO_ROOT"
        # Use perl-based alarm; macOS lacks GNU `timeout` by default.
        perl -e '
            use strict; use warnings;
            my $t = shift; my $c = shift;
            my $pid = fork();
            if ($pid == 0) { exec("/bin/bash", "-c", $c) or exit 127; }
            local $SIG{ALRM} = sub { kill 9, $pid; exit 124; };
            alarm $t;
            waitpid($pid, 0);
            exit ($? >> 8);
        ' "$timeout" "$cmd" 2>"$stderr_file"
    )
    local rc=$?
    local stderr_snip
    stderr_snip="$(head -c 200 "$stderr_file" 2>/dev/null | tr '\n' ' ' | tr '"' "'" )"
    rm -f "$stderr_file"
    printf 'EXIT\t%s\nSTDERR\t%s\n' "$rc" "$stderr_snip"
}

# ---- Walk runbooks -----------------------------------------------------------

declare -a RUNBOOKS=()
while IFS= read -r f; do
    RUNBOOKS+=("$f")
done < <(find -L "$RUNBOOKS_DIR" -maxdepth 1 -type f -name '*.md' | sort)

if [[ ${#RUNBOOKS[@]} -eq 0 ]]; then
    echo "validate-aaron-runbooks: no *.md files in $RUNBOOKS_DIR" >&2
    exit 3
fi

# Aggregate buffer (newline-separated JSON objects, one per runbook)
AGG_TMP="$(mktemp)"
trap 'rm -f "$AGG_TMP"' EXIT

# Summary table
SUMMARY_TMP="$(mktemp)"
trap 'rm -f "$AGG_TMP" "$SUMMARY_TMP"' EXIT

TOTAL_PASS=0
TOTAL_WARN=0
TOTAL_FAIL=0

[[ "$QUIET" -eq 0 ]] && echo "validate-aaron-runbooks: scanning ${#RUNBOOKS[@]} runbook(s) under $RUNBOOKS_DIR"
[[ "$QUIET" -eq 0 ]] && echo

for runbook in "${RUNBOOKS[@]}"; do
    name="$(basename "$runbook")"
    [[ "$QUIET" -eq 0 ]] && echo "=== $name ==="

    # Extract commands; collect classification + execution outcomes.
    extract_out="$(extract_bash_blocks "$runbook" 2>/dev/null)" || ZSF_EXTRACT_ERRORS=$((ZSF_EXTRACT_ERRORS + 1))

    cmd_count=0
    safe_count=0
    dry_count=0
    destructive_count=0
    broken_count=0
    unknown_count=0
    note_count=0
    exec_fail_count=0

    # commands.json buffer for this runbook
    cmds_json="["
    first_cmd=1

    while IFS=$'\t' read -r lineno cmd; do
        [[ -z "${cmd:-}" ]] && continue
        cmd_count=$((cmd_count + 1))

        classification="$(classify_cmd "$cmd" 2>/dev/null)" || {
            ZSF_CLASSIFY_ERRORS=$((ZSF_CLASSIFY_ERRORS + 1))
            classification=$'unknown\tclassifier error'
        }
        kind="${classification%%$'\t'*}"
        reason="${classification#*$'\t'}"

        exit_code="null"
        stderr_snip=""

        case "$kind" in
            safe|dry)
                if [[ "$EXECUTE" -eq 1 ]]; then
                    exec_out="$(execute_cmd "$cmd" "$PER_CMD_TIMEOUT" 2>/dev/null)" || true
                    exit_code="$(printf '%s' "$exec_out" | awk -F'\t' '/^EXIT/{print $2}')"
                    stderr_snip="$(printf '%s' "$exec_out" | awk -F'\t' '/^STDERR/{print $2}')"
                    if [[ "$exit_code" != "0" ]]; then
                        exec_fail_count=$((exec_fail_count + 1))
                        ZSF_EXECUTE_ERRORS=$((ZSF_EXECUTE_ERRORS + 1))
                    fi
                fi
                [[ "$kind" == "safe" ]] && safe_count=$((safe_count + 1))
                [[ "$kind" == "dry"  ]] && dry_count=$((dry_count + 1))
                ;;
            destructive) destructive_count=$((destructive_count + 1)) ;;
            broken)      broken_count=$((broken_count + 1)) ;;
            unknown)     unknown_count=$((unknown_count + 1)) ;;
            note)        note_count=$((note_count + 1)) ;;
        esac

        # Append to commands array
        if [[ $first_cmd -eq 1 ]]; then
            first_cmd=0
        else
            cmds_json+=","
        fi
        cmds_json+="$(python3 - "$lineno" "$cmd" "$kind" "$reason" "$exit_code" "$stderr_snip" <<'PY'
import json, sys
lineno, cmd, kind, reason, exit_code, stderr_snip = sys.argv[1:7]
try:
    exit_val = None if exit_code in ("", "null") else int(exit_code)
except ValueError:
    exit_val = exit_code
print(json.dumps({
    "line": int(lineno) if lineno.isdigit() else lineno,
    "cmd": cmd,
    "kind": kind,
    "reason": reason,
    "exit": exit_val,
    "stderr": stderr_snip,
}))
PY
)"
    done <<< "$extract_out"

    cmds_json+="]"

    # Per-runbook verdict.
    verdict="PASS"
    if [[ $broken_count -gt 0 || $exec_fail_count -gt 0 ]]; then
        verdict="FAIL"
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
    elif [[ $unknown_count -gt 0 ]]; then
        verdict="WARN"
        TOTAL_WARN=$((TOTAL_WARN + 1))
    else
        TOTAL_PASS=$((TOTAL_PASS + 1))
    fi

    printf '%-40s  %-4s  cmds=%-3d safe=%-2d dry=%-2d destr=%-2d unknown=%-2d broken=%-2d execfail=%-2d\n' \
        "$name" "$verdict" "$cmd_count" "$safe_count" "$dry_count" "$destructive_count" \
        "$unknown_count" "$broken_count" "$exec_fail_count" >> "$SUMMARY_TMP"

    [[ "$QUIET" -eq 0 ]] && tail -1 "$SUMMARY_TMP"

    # Append per-runbook JSON object to aggregate buffer.
    python3 - "$runbook" "$verdict" "$cmd_count" "$safe_count" "$dry_count" \
                       "$destructive_count" "$unknown_count" "$broken_count" \
                       "$exec_fail_count" "$cmds_json" >> "$AGG_TMP" <<'PY'
import json, os, sys
path, verdict, cmd_count, safe, dry, destr, unknown, broken, execfail, cmds_json = sys.argv[1:11]
obj = {
    "runbook": os.path.basename(path),
    "path": path,
    "verdict": verdict,
    "commands_total": int(cmd_count),
    "safe": int(safe),
    "dry_run": int(dry),
    "destructive": int(destr),
    "unknown": int(unknown),
    "broken": int(broken),
    "exec_fail": int(execfail),
    "commands": json.loads(cmds_json),
}
print(json.dumps(obj))
PY

done

# ---- Summary -----------------------------------------------------------------

[[ "$QUIET" -eq 0 ]] && echo
[[ "$QUIET" -eq 0 ]] && echo "=== Summary ==="
[[ "$QUIET" -eq 0 ]] && cat "$SUMMARY_TMP"
[[ "$QUIET" -eq 0 ]] && echo
[[ "$QUIET" -eq 0 ]] && echo "ZSF counters: extract_errors=$ZSF_EXTRACT_ERRORS classify_errors=$ZSF_CLASSIFY_ERRORS execute_errors=$ZSF_EXECUTE_ERRORS"
[[ "$QUIET" -eq 0 ]] && echo "Totals: PASS=$TOTAL_PASS  WARN=$TOTAL_WARN  FAIL=$TOTAL_FAIL"

# ---- JSON output -------------------------------------------------------------

if [[ -n "$OUT_JSON" ]]; then
    python3 - "$AGG_TMP" "$OUT_JSON" "$TOTAL_PASS" "$TOTAL_WARN" "$TOTAL_FAIL" \
                       "$ZSF_EXTRACT_ERRORS" "$ZSF_CLASSIFY_ERRORS" "$ZSF_EXECUTE_ERRORS" <<'PY'
import json, sys
agg_path, out_path, p, w, f, ze, zc, zx = sys.argv[1:9]
runbooks = []
with open(agg_path) as fh:
    for line in fh:
        line = line.strip()
        if line:
            runbooks.append(json.loads(line))
out = {
    "schema": "aaron-runbook-validator/v1",
    "totals": {"pass": int(p), "warn": int(w), "fail": int(f)},
    "zsf": {
        "extract_errors": int(ze),
        "classify_errors": int(zc),
        "execute_errors": int(zx),
    },
    "runbooks": runbooks,
}
with open(out_path, "w") as fh:
    json.dump(out, fh, indent=2)
print(f"validate-aaron-runbooks: wrote {out_path}", file=sys.stderr)
PY
fi

# Exit code policy: FAIL trumps WARN trumps PASS.
if [[ $TOTAL_FAIL -gt 0 ]]; then
    exit 2
fi
exit 0
