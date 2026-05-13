#!/usr/bin/env bash
# sanitize-rules.sh — MODERATE sanitization for ContextDNA IDE mothership extraction.
#
# Drops obvious secrets (API keys, bearer tokens, DB passwords, hardcoded LAN
# NATS URLs, AWS account IDs). Keeps Aaron-specific paths intact — repo is
# private. See docs/plans/2026-05-13-contextdna-ide-mothership-plan.md §4 D5.
#
# Usage: sanitize-rules.sh <target_dir>
# Output: <target_dir>/sanitize-report.txt   (TSV: rule | file | line | replacement)
# Exit:   0 on success, 1 on bad args, 2 if unhandled secret patterns detected.
#
# ZSF: every rule has its own counter; failures are observable, never silent.
# Cross-platform: prefers `perl -i -pe` over sed (BSD vs GNU sed avoidance).
# Bash 3.2 compatible (macOS default) — no `mapfile`, no associative arrays.

set -euo pipefail

# -------- arg parsing --------
if [[ $# -ne 1 ]]; then
  echo "usage: $(basename "$0") <target_dir>" >&2
  exit 1
fi

TARGET="$1"
if [[ ! -d "$TARGET" ]]; then
  echo "error: '$TARGET' is not a directory" >&2
  exit 1
fi

TARGET="$(cd "$TARGET" && pwd -P)"
REPORT="$TARGET/sanitize-report.txt"

# -------- portability probe (kept for documentation; perl handles it) --------
SED_FLAVOR="bsd"
if sed --version >/dev/null 2>&1; then
  SED_FLAVOR="gnu"
fi
export SED_FLAVOR  # unused by current rule set but available for future rules

# -------- dependency check --------
if ! command -v perl >/dev/null 2>&1; then
  echo "error: perl required (used for portable in-place regex edits)" >&2
  exit 1
fi

# -------- counter sentinels (avoid subshell variable loss in bash 3.2) --------
# Each rule increments a sentinel file with one byte per hit; we count bytes
# at the end. Sentinels live in a dedicated tmp dir, removed on exit.
SENT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sanitize-counters-XXXXXX")"
trap 'rm -rf "$SENT_DIR"' EXIT

# Pre-create empty sentinels so wc -c is always safe.
for rule in RULE1_API_KEY RULE2_JWT_TOKEN RULE3_DB_URL RULE4_EMAIL \
            RULE5_NATS_URL RULE6_AWS_ARN UNHANDLED; do
  : > "$SENT_DIR/$rule"
done

count_file() { wc -c < "$1" | tr -d ' '; }

# -------- report header --------
{
  printf '# sanitize-report.txt\n'
  printf '# generated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '# target:    %s\n' "$TARGET"
  printf '# format:    rule\tfile\tline\treplacement-applied\n'
  printf '# ---------------------------------------------------------------\n'
} > "$REPORT"

# -------- file discovery (bash 3.2 portable, NUL-delimited) --------
FILE_LIST="$SENT_DIR/files.lst"
find "$TARGET" \
  \( -path '*/.git' -o -path '*/node_modules' -o -path '*/.venv' \
     -o -path '*/__pycache__' -o -path '*/dist' -o -path '*/build' \) -prune \
  -o -type f \( \
       -name '*.py' -o -name '*.sh' -o -name '*.md' \
       -o -name '*.yaml' -o -name '*.yml' \
       -o -name '*.json' -o -name '*.toml' \
       -o -name '*.txt' -o -name '*.example' \
       -o -name '.env' -o -name '.env.*' -o -name '*.env' \
     \) -print0 > "$FILE_LIST"

FILE_COUNT=0
if [[ -s "$FILE_LIST" ]]; then
  # Count NUL terminators
  FILE_COUNT=$(tr -cd '\0' < "$FILE_LIST" | wc -c | tr -d ' ')
fi

if [[ "$FILE_COUNT" -eq 0 ]]; then
  printf '# (no matching files found)\n' >> "$REPORT"
  printf 'sanitize-rules.sh: no matching files under %s\n' "$TARGET" >&2
  exit 0
fi

# -------- helper: run perl with stderr capture; aggregate hits into report + sentinel --------
# args: rule_tag, file, perl_program
# perl_program MUST emit one line per hit on STDERR in form:
#   <rule_tag>\t<file_path>\t<line_no>\t<replacement_text>
apply_rule() {
  local rule_tag="$1" file="$2" program="$3"
  local err_log="$SENT_DIR/last.err"
  : > "$err_log"

  # Run perl with stderr redirected to a file we can post-process synchronously
  # (avoids the bash 3.2 subshell variable trap).
  perl -i -pe "$program" "$file" 2> "$err_log" || true

  if [[ -s "$err_log" ]]; then
    # Append each hit to the report and bump the sentinel counter.
    while IFS=$'\t' read -r tag fpath lineno repl; do
      [[ -z "$tag" ]] && continue
      [[ "$tag" != "$rule_tag" ]] && continue
      printf '%s\t%s\t%s\t%s\n' "$tag" "${fpath#$TARGET/}" "$lineno" "$repl" >> "$REPORT"
      printf '.' >> "$SENT_DIR/$rule_tag"
    done < "$err_log"
  fi
}

# -------- binary sniff: grep -I treats any file with a NUL byte as binary --------
is_text() {
  local f="$1"
  [[ ! -s "$f" ]] && return 0
  LC_ALL=C grep -Iq . "$f"
}

# -------- per-file processing --------
process_file() {
  local file="$1"
  [[ "$file" == "$REPORT" ]] && return 0
  is_text "$file" || return 0

  # -- Rule 1: real API keys (40+ alnum chars following a known KEY= name) --
  apply_rule "RULE1_API_KEY" "$file" '
    s{(?<name>OPENAI_API_KEY|ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|Context_DNA_Deep_Seek|Context_DNA_Deepseek|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|STRIPE_[A-Z_]*KEY|STRIPE_SECRET_KEY|GITHUB_TOKEN|SLACK_BOT_TOKEN|HF_TOKEN)(?<sep>\s*[:=]\s*"?)(?<val>[A-Za-z0-9_\-]{40,})}
     {
       my $name = $+{name};
       my $sep  = $+{sep};
       print STDERR "RULE1_API_KEY\t$ARGV\t$.\tYOUR_KEY_HERE_$name\n";
       "${name}${sep}YOUR_KEY_HERE_${name}";
     }gex;
  '

  # -- Rule 2: JWT-style bearer tokens (eyJ... base64 segments) --
  apply_rule "RULE2_JWT_TOKEN" "$file" '
    s{(eyJ[A-Za-z0-9._\-]{40,})}
     {
       print STDERR "RULE2_JWT_TOKEN\t$ARGV\t$.\tYOUR_TOKEN_HERE\n";
       "YOUR_TOKEN_HERE";
     }gex;
  '

  # -- Rule 3: DB / queue URLs with embedded passwords --
  apply_rule "RULE3_DB_URL" "$file" '
    s{\b(?<scheme>postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://(?<user>[^:@/\s]+):(?<pw>[^@/\s"]+)\@(?<host>[^/\s"]+)}
     {
       my $pw = $+{pw};
       if ($pw =~ /^(YOUR_PASSWORD|pass(?:word)?|xxx+|\*+)$/i) {
         "$+{scheme}://$+{user}:$pw\@$+{host}";
       } else {
         print STDERR "RULE3_DB_URL\t$ARGV\t$.\t$+{scheme}://$+{user}:YOUR_PASSWORD\@$+{host}\n";
         "$+{scheme}://$+{user}:YOUR_PASSWORD\@$+{host}";
       }
     }gex;
  '

  # -- Rule 4: real email — skip .env.example (template only per spec) --
  local base
  base="$(basename "$file")"
  if [[ "$base" != ".env.example" && "$base" != "env.example" ]]; then
    apply_rule "RULE4_EMAIL" "$file" '
      s{\bsupport\@ersimulator\.com\b}
       {
         print STDERR "RULE4_EMAIL\t$ARGV\t$.\tyour\@email.com\n";
         "your\@email.com";
       }gex;
    '
  fi

  # -- Rule 5: hardcoded private-LAN NATS URLs (192.168.x.y) --
  apply_rule "RULE5_NATS_URL" "$file" '
    s{nats://192\.168\.\d{1,3}\.\d{1,3}:4222}
     {
       print STDERR "RULE5_NATS_URL\t$ARGV\t$.\tnats://localhost:4222\n";
       "nats://localhost:4222";
     }gex;
  '

  # -- Rule 6: AWS account IDs inside ARNs --
  apply_rule "RULE6_AWS_ARN" "$file" '
    s{arn:aws:([^:]+):([^:]*):(\d{12}):}
     {
       print STDERR "RULE6_AWS_ARN\t$ARGV\t$.\tarn:aws:SERVICE:REGION:YOUR_AWS_ACCOUNT:\n";
       "arn:aws:SERVICE:REGION:YOUR_AWS_ACCOUNT:";
     }gex;
  '

  # -- Catch-all: unhandled long alnum tokens near KEY=/TOKEN=/SECRET= --
  # No modification; flag to stderr + report. Pre-sanitized placeholders skipped.
  perl -ne '
    next if /YOUR_KEY_HERE_|YOUR_TOKEN_HERE|YOUR_PASSWORD|YOUR_AWS_ACCOUNT/;
    next if /^\s*#/;
    if (/\b(?:[A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET))\s*[:=]\s*"?([A-Za-z0-9_\-]{40,})/) {
      my $hit = $1;
      next if $hit =~ /^(?:placeholder|example|changeme|todo|fixme|your_)/i;
      print "$.\t$hit\n";
    }
  ' "$file" > "$SENT_DIR/last.unhandled" || true

  if [[ -s "$SENT_DIR/last.unhandled" ]]; then
    while IFS=$'\t' read -r lineno hit; do
      [[ -z "$lineno" ]] && continue
      echo "UNHANDLED_SECRET	${file#$TARGET/}	$lineno	$hit" >&2
      printf 'UNHANDLED_SECRET\t%s\t%s\t%s\n' "${file#$TARGET/}" "$lineno" "$hit" >> "$REPORT"
      printf '.' >> "$SENT_DIR/UNHANDLED"
    done < "$SENT_DIR/last.unhandled"
  fi
}

# -------- main loop (bash 3.2 NUL-safe) --------
while IFS= read -r -d '' f; do
  process_file "$f"
done < "$FILE_LIST"

# -------- collect counts --------
SANITIZED_API_KEYS=$(count_file "$SENT_DIR/RULE1_API_KEY")
SANITIZED_TOKENS=$(count_file "$SENT_DIR/RULE2_JWT_TOKEN")
SANITIZED_DB_URLS=$(count_file "$SENT_DIR/RULE3_DB_URL")
SANITIZED_EMAILS=$(count_file "$SENT_DIR/RULE4_EMAIL")
SANITIZED_NATS_URLS=$(count_file "$SENT_DIR/RULE5_NATS_URL")
SANITIZED_AWS_ARNS=$(count_file "$SENT_DIR/RULE6_AWS_ARN")
UNHANDLED_PATTERNS=$(count_file "$SENT_DIR/UNHANDLED")

# -------- summary on stderr --------
{
  echo "----------------------------------------------"
  echo "sanitize-rules.sh summary"
  echo "  target dir:              $TARGET"
  echo "  files scanned:           $FILE_COUNT"
  echo "  SANITIZED_API_KEYS:      $SANITIZED_API_KEYS"
  echo "  SANITIZED_TOKENS:        $SANITIZED_TOKENS"
  echo "  SANITIZED_DB_URLS:       $SANITIZED_DB_URLS"
  echo "  SANITIZED_EMAILS:        $SANITIZED_EMAILS"
  echo "  SANITIZED_NATS_URLS:     $SANITIZED_NATS_URLS"
  echo "  SANITIZED_AWS_ARNS:      $SANITIZED_AWS_ARNS"
  echo "  UNHANDLED_PATTERNS:      $UNHANDLED_PATTERNS"
  echo "  report:                  $REPORT"
  echo "----------------------------------------------"
} >&2

# -------- append summary to report tail --------
{
  echo
  echo "# ---- summary ----"
  echo "# files_scanned=$FILE_COUNT"
  echo "# SANITIZED_API_KEYS=$SANITIZED_API_KEYS"
  echo "# SANITIZED_TOKENS=$SANITIZED_TOKENS"
  echo "# SANITIZED_DB_URLS=$SANITIZED_DB_URLS"
  echo "# SANITIZED_EMAILS=$SANITIZED_EMAILS"
  echo "# SANITIZED_NATS_URLS=$SANITIZED_NATS_URLS"
  echo "# SANITIZED_AWS_ARNS=$SANITIZED_AWS_ARNS"
  echo "# UNHANDLED_PATTERNS=$UNHANDLED_PATTERNS"
} >> "$REPORT"

if [[ "$UNHANDLED_PATTERNS" -gt 0 ]]; then
  echo "sanitize-rules.sh: $UNHANDLED_PATTERNS unhandled secret pattern(s) detected — review report and stderr." >&2
  exit 2
fi

exit 0
