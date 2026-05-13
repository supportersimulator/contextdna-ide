#!/usr/bin/env bash
#
# wire-submodules.sh (migrate2) — dynamic-resolution submodule wiring
#
# Replaces the migrate/ version's hardcoded URLs with a four-tier resolver:
#   1. CLI flag     --url-<name>=<url>           (highest)
#   2. Env var      SUBMODULE_<NAME>_URL          (uppercased, dashes→underscores)
#   3. Lock file    .gitmodules.lock              (url= + sha= per submodule)
#   4. Interactive  read -p (only if stdin is a TTY)
#   5. FAIL LOUDLY  (no silent default — ZSF)
#
# Pin captures (--pin-current): after each `git submodule add`, the resolved
# commit SHA is written back into .gitmodules.lock so the next clone is
# byte-identical without operator memory. This preserves the operational-
# invariance promise: clone → docker compose → working.
#
# Usage:
#   wire-submodules.sh --target <dir> [--pin-current]
#                      [--template <path>]   (default: ../.gitmodules.template)
#                      [--lock <path>]       (default: ../.gitmodules.lock)
#                      [--url-3-surgeons=<url>] [--url-multi-fleet=<url>] ...
#
# ZSF counters surfaced in summary: WIRED / SKIPPED_UNREACHABLE / PINNED / ERRORS

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- defaults ----------------------------------------------------------------
TARGET=""
TEMPLATE_FILE="$MIGRATE_ROOT/.gitmodules.template"
LOCK_FILE="$MIGRATE_ROOT/.gitmodules.lock"
PIN_CURRENT=0

# CLI URL overrides stored as parallel arrays (bash 3.2 compatible — macOS ships
# /bin/bash 3.2 which has no associative arrays; /usr/bin/env bash may resolve
# to a newer one, but we don't require it).
CLI_URL_KEYS=()
CLI_URL_VALS=()

cli_url_lookup() {
    # Try full name first, then the path basename — so the operator can pass
    # --url-3-surgeons even when the [submodule] section is "engines/3-surgeons".
    local needle="$1" leaf="${1##*/}" i
    for i in "${!CLI_URL_KEYS[@]}"; do
        if [[ "${CLI_URL_KEYS[$i]}" == "$needle" ]] || \
           [[ "${CLI_URL_KEYS[$i]}" == "$leaf" ]]; then
            echo "${CLI_URL_VALS[$i]}"
            return 0
        fi
    done
    return 1
}

# ---- argument parsing --------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET="${2:-}"; shift 2 ;;
        --target=*)
            TARGET="${1#*=}"; shift ;;
        --template)
            TEMPLATE_FILE="${2:-}"; shift 2 ;;
        --template=*)
            TEMPLATE_FILE="${1#*=}"; shift ;;
        --lock)
            LOCK_FILE="${2:-}"; shift 2 ;;
        --lock=*)
            LOCK_FILE="${1#*=}"; shift ;;
        --pin-current)
            PIN_CURRENT=1; shift ;;
        --url-*=*)
            # --url-3-surgeons=https://... → key=3-surgeons, value=https://...
            kv="${1#--url-}"
            key="${kv%%=*}"
            val="${kv#*=}"
            CLI_URL_KEYS+=("$key")
            CLI_URL_VALS+=("$val")
            shift ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "usage: $0 --target <dir> [--pin-current] [--template <p>] [--lock <p>]" >&2
            exit 2 ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    echo "ERROR: --target <dir> required" >&2
    exit 2
fi
if [[ ! -d "$TARGET" ]]; then
    echo "ERROR: target dir does not exist: $TARGET" >&2
    exit 2
fi
if ! git -C "$TARGET" status >/dev/null 2>&1; then
    echo "ERROR: target dir is not a git repo: $TARGET" >&2
    exit 2
fi
if [[ ! -f "$TEMPLATE_FILE" ]]; then
    echo "ERROR: template not found: $TEMPLATE_FILE" >&2
    exit 2
fi

TODO_FILE="$TARGET/submodule-todo.md"

# Counters (ZSF — every branch increments exactly one)
WIRED=0
SKIPPED_UNREACHABLE=0
PINNED=0
ERRORS=0

# ---- template parser ---------------------------------------------------------
# Parses .gitmodules.template into parallel arrays NAMES / PATHS / PLACEHOLDERS.
# Placeholder format: url = ${SUBMODULE_<NAME>_URL}
# We accept any placeholder name (no hardcoded list) so adding a 5th submodule
# is purely a template edit.
NAMES=()
PATHS=()
PLACEHOLDERS=()

parse_template() {
    local current_name="" current_path="" current_ph=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        # strip comments and leading whitespace
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" ]] && continue

        if [[ "$line" =~ ^\[submodule[[:space:]]+\"([^\"]+)\"\] ]]; then
            # flush prior
            if [[ -n "$current_name" ]]; then
                NAMES+=("$current_name")
                PATHS+=("${current_path:-$current_name}")
                PLACEHOLDERS+=("$current_ph")
            fi
            current_name="${BASH_REMATCH[1]}"
            current_path=""
            current_ph=""
            continue
        fi

        if [[ "$line" =~ ^path[[:space:]]*=[[:space:]]*(.+)$ ]]; then
            current_path="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^url[[:space:]]*=[[:space:]]*\$\{([A-Z0-9_]+)\} ]]; then
            current_ph="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^url[[:space:]]*=[[:space:]]*(.+)$ ]]; then
            # literal URL — wrap in a synthetic placeholder so resolver still runs
            current_ph="__LITERAL__:${BASH_REMATCH[1]}"
        fi
    done < "$TEMPLATE_FILE"

    if [[ -n "$current_name" ]]; then
        NAMES+=("$current_name")
        PATHS+=("${current_path:-$current_name}")
        PLACEHOLDERS+=("$current_ph")
    fi
}

parse_template

if [[ ${#NAMES[@]} -eq 0 ]]; then
    echo "ERROR: no [submodule ...] blocks parsed from $TEMPLATE_FILE" >&2
    exit 2
fi

# ---- lock file helpers -------------------------------------------------------
# Lock file format (line-oriented, easy to grep/diff):
#   <path> url=<url> sha=<sha>
# One submodule per line. Comments (#) and blank lines ignored.
lock_lookup_url() {
    local path="$1"
    [[ -f "$LOCK_FILE" ]] || return 1
    awk -v p="$path" '
        $0 ~ /^[[:space:]]*#/ { next }
        $1 == p {
            for (i = 2; i <= NF; i++) {
                if ($i ~ /^url=/) { sub(/^url=/, "", $i); print $i; exit }
            }
        }' "$LOCK_FILE"
}

lock_lookup_sha() {
    local path="$1"
    [[ -f "$LOCK_FILE" ]] || return 1
    awk -v p="$path" '
        $0 ~ /^[[:space:]]*#/ { next }
        $1 == p {
            for (i = 2; i <= NF; i++) {
                if ($i ~ /^sha=/) { sub(/^sha=/, "", $i); print $i; exit }
            }
        }' "$LOCK_FILE"
}

# Atomic upsert: rewrite lock line for <path> with new url= and sha=.
# Existing entries replaced; new entries appended. Idempotent.
lock_write() {
    local path="$1" url="$2" sha="$3"
    local tmp; tmp="$(mktemp)"
    local replaced=0
    if [[ -f "$LOCK_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            # passthrough comments/blanks
            if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "$line" ]]; then
                echo "$line" >> "$tmp"; continue
            fi
            local first; first="$(awk '{print $1}' <<< "$line")"
            if [[ "$first" == "$path" ]]; then
                echo "$path url=$url sha=$sha" >> "$tmp"
                replaced=1
            else
                echo "$line" >> "$tmp"
            fi
        done < "$LOCK_FILE"
    else
        {
            echo "# .gitmodules.lock — pinned submodule URLs + commit SHAs"
            echo "# format: <path> url=<url> sha=<sha>"
            echo "# auto-managed by wire-submodules.sh --pin-current"
        } >> "$tmp"
    fi
    if [[ "$replaced" -eq 0 ]]; then
        echo "$path url=$url sha=$sha" >> "$tmp"
    fi
    mv "$tmp" "$LOCK_FILE"
}

# ---- URL resolver (the heart of the dynamic replacement) --------------------
resolve_url() {
    local name="$1" path="$2" placeholder="$3"

    # Tier 0: template-embedded literal (escape hatch — discouraged)
    if [[ "$placeholder" == __LITERAL__:* ]]; then
        echo "${placeholder#__LITERAL__:}"
        return 0
    fi

    # Tier 1: CLI flag
    local cli_val
    if cli_val="$(cli_url_lookup "$name")"; then
        echo "$cli_val"; return 0
    fi

    # Tier 2: env var (the placeholder name itself, e.g. SUBMODULE_3_SURGEONS_URL)
    if [[ -n "$placeholder" ]]; then
        local envval="${!placeholder:-}"
        if [[ -n "$envval" ]]; then
            echo "$envval"; return 0
        fi
    fi

    # Tier 3: lock file (pinned URL)
    local lurl; lurl="$(lock_lookup_url "$path" 2>/dev/null || true)"
    if [[ -n "$lurl" ]]; then
        echo "$lurl"; return 0
    fi

    # Tier 4: interactive prompt (only if TTY)
    if [[ -t 0 ]] && [[ -t 1 ]]; then
        local answer=""
        # shellcheck disable=SC2162
        read -p "URL for submodule '$name' ($placeholder)? " answer < /dev/tty
        if [[ -n "$answer" ]]; then
            echo "$answer"; return 0
        fi
    fi

    # Tier 5: fail loudly (ZSF — no silent default)
    return 1
}

# ---- reachability probe (5s ceiling, cross-platform) ------------------------
reachable() {
    local url="$1"
    local tcmd=""
    if command -v timeout >/dev/null 2>&1; then
        tcmd="timeout 5"
    elif command -v gtimeout >/dev/null 2>&1; then
        tcmd="gtimeout 5"
    fi
    if [[ -n "$tcmd" ]]; then
        $tcmd git ls-remote "$url" HEAD >/dev/null 2>&1
    else
        GIT_HTTP_LOW_SPEED_LIMIT=1 GIT_HTTP_LOW_SPEED_TIME=5 \
            git ls-remote "$url" HEAD >/dev/null 2>&1
    fi
}

# ---- TODO sink (only created on first write) --------------------------------
note_todo() {
    local msg="$1"
    {
        if [[ ! -s "$TODO_FILE" ]]; then
            echo "# Submodule TODOs"
            echo ""
            echo "Generated by wire-submodules.sh (migrate2) on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
            echo ""
        fi
        echo "- $msg"
    } >> "$TODO_FILE"
}

# ---- per-submodule wiring ---------------------------------------------------
wire_one() {
    local name="$1" path="$2" placeholder="$3"

    echo ">> $name ($path)" >&2

    local url=""
    if ! url="$(resolve_url "$name" "$path" "$placeholder")"; then
        echo "   ERROR: could not resolve URL for '$name' (placeholder=$placeholder)" >&2
        echo "          set \$$placeholder, pass --url-$name=<url>, or add to $LOCK_FILE" >&2
        note_todo "**$name** — URL unresolved (placeholder \`$placeholder\`)"
        ERRORS=$((ERRORS + 1))
        return 0
    fi
    echo "   resolved: $url" >&2

    if ! reachable "$url"; then
        echo "   unreachable: $url — skipping" >&2
        note_todo "**$name** — remote unreachable: \`$url\`"
        SKIPPED_UNREACHABLE=$((SKIPPED_UNREACHABLE + 1))
        return 0
    fi

    if git -C "$TARGET" submodule add "$url" "$path" 2>&1 | sed 's/^/   /' >&2; then
        WIRED=$((WIRED + 1))
    else
        echo "   ERROR: submodule add failed for $name" >&2
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    # Pinning from lock (sha=) — applied even without --pin-current so a
    # checked-in lock file fully reproduces upstream state.
    local pinned_sha; pinned_sha="$(lock_lookup_sha "$path" 2>/dev/null || true)"
    if [[ -n "$pinned_sha" ]]; then
        echo "   pinning to $pinned_sha (from lock)" >&2
        if git -C "$TARGET/$path" fetch --depth 1 origin "$pinned_sha" 2>/dev/null \
           && git -C "$TARGET/$path" checkout --detach "$pinned_sha" 2>&1 | sed 's/^/   /' >&2; then
            git -C "$TARGET" add "$path" 2>/dev/null || true
            PINNED=$((PINNED + 1))
        else
            echo "   WARN: could not pin $name to $pinned_sha — leaving at HEAD" >&2
            note_todo "**$name** — failed to pin to \`$pinned_sha\`"
            ERRORS=$((ERRORS + 1))
        fi
    elif [[ "$PIN_CURRENT" -eq 1 ]]; then
        # Capture current HEAD into lock so future clones reproduce this state
        local current_sha
        if current_sha="$(git -C "$TARGET/$path" rev-parse HEAD 2>/dev/null)"; then
            lock_write "$path" "$url" "$current_sha"
            echo "   pinned current HEAD → $current_sha (written to $LOCK_FILE)" >&2
            PINNED=$((PINNED + 1))
        else
            echo "   WARN: could not read HEAD sha for $name; not pinning" >&2
            note_todo "**$name** — --pin-current requested but rev-parse failed"
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "   no pin (lock has no sha, --pin-current not set) — at HEAD" >&2
    fi
}

# ---- main loop --------------------------------------------------------------
for i in "${!NAMES[@]}"; do
    wire_one "${NAMES[$i]}" "${PATHS[$i]}" "${PLACEHOLDERS[$i]}"
done

cat >&2 <<SUMMARY

============================================================
wire-submodules.sh (migrate2) summary
============================================================
  WIRED                = $WIRED
  SKIPPED_UNREACHABLE  = $SKIPPED_UNREACHABLE
  PINNED               = $PINNED
  ERRORS               = $ERRORS

Target:   $TARGET
Template: $TEMPLATE_FILE
Lock:     $LOCK_FILE $([[ -f "$LOCK_FILE" ]] && echo "(exists)" || echo "(absent)")
TODO log: $TODO_FILE (only written if entries exist)

Changes are staged but NOT committed. Review with:
  git -C "$TARGET" status
  git -C "$TARGET" diff --cached
============================================================
SUMMARY

if [[ "$ERRORS" -gt 0 ]]; then
    exit 1
fi
exit 0
