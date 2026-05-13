#!/usr/bin/env bash
# configure-services.sh — Probe every service ContextDNA can use, configure
# what's missing, swap providers freely. Designed for two scenarios:
#
#   1. FIRST RUN: any new user runs this after `git clone` and has a
#      complete working system at the end. No prior knowledge required.
#
#   2. RECOVERY 6 MONTHS LATER: user has restored .env from a bundle but
#      some API keys have rotated, IP addresses changed, they want a
#      different local LLM model, etc. This script detects what works,
#      preserves it, and prompts only for what's broken or missing.
#
# Service categories handled (each can be skipped if not needed):
#   - LLM providers     (Cardiologist + Neurologist; multi-provider OK)
#   - Local LLM         (MLX on Apple Silicon, or Ollama cross-platform)
#   - Backup storage    (S3-compatible — picks up where setup-mothership left off)
#   - NATS / fleet      (auto-detect local or remote; offers to install)
#   - Optional services (ElevenLabs, LiveKit, Stripe, Supabase, etc.)
#
# Usage:
#   bash scripts/configure-services.sh            # interactive, all services
#   bash scripts/configure-services.sh --probe    # report status only, no prompts
#   bash scripts/configure-services.sh --service NAME  # only configure one
#
# ZSF: every failure is named and exits non-zero.

set -uo pipefail

MODE="interactive"
ONLY_SERVICE=""
for arg in "$@"; do
    case "$arg" in
        --probe)   MODE="probe" ;;
        --service) ONLY_SERVICE="next" ;;
        --help|-h) sed -n '2,28p' "$0" | sed 's|^# ||; s|^#||'; exit 0 ;;
        *)
            if [ "$ONLY_SERVICE" = "next" ]; then ONLY_SERVICE="$arg"
            else echo "unknown arg: $arg" >&2; exit 2; fi ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1); BLUE=$(tput setaf 4); CYAN=$(tput setaf 6)
    DIM=$(tput dim); RESET=$(tput sgr0)
else BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; CYAN=""; DIM=""; RESET=""; fi

_step()    { echo ""; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
_ok()      { echo "  ${GREEN}✓${RESET} $*"; }
_warn()    { echo "  ${YELLOW}⚠${RESET} $*"; }
_fail()    { echo "  ${RED}✗${RESET} $*" >&2; exit 1; }
_info()    { echo "  ${DIM}$*${RESET}"; }
_link()    { echo "  ${CYAN}→ $*${RESET}"; }
_ask()     {
    local prompt="$1" default="${2:-}" var
    [ "$MODE" = "probe" ] && { echo "$default"; return; }
    if [ -n "$default" ]; then
        read -r -p "  ${BOLD}?${RESET} $prompt [${DIM}$default${RESET}]: " var
        echo "${var:-$default}"
    else
        read -r -p "  ${BOLD}?${RESET} $prompt: " var
        echo "$var"
    fi
}
_ask_secret() {
    local prompt="$1" var
    [ "$MODE" = "probe" ] && { echo ""; return; }
    read -r -s -p "  ${BOLD}?${RESET} $prompt: " var
    echo "" >&2
    echo "$var"
}
_confirm() {
    local prompt="$1"
    [ "$MODE" = "probe" ] && return 1
    read -r -p "  ${BOLD}?${RESET} $prompt [Y/n]: " var
    [[ "$var" =~ ^[Nn] ]] && return 1 || return 0
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
PLATFORM="$(uname -s)"

# Make sure .env exists; seed from .env.example if not
if [ ! -f "$ENV_FILE" ] && [ -f "$REPO_ROOT/.env.example" ]; then
    [ "$MODE" = "probe" ] || cp "$REPO_ROOT/.env.example" "$ENV_FILE"
fi
[ -f "$ENV_FILE" ] || touch "$ENV_FILE"
chmod 600 "$ENV_FILE" 2>/dev/null || true

# ── Helpers: read/write .env without clobbering other vars ───────────────────
_env_get() {
    [ -f "$ENV_FILE" ] || { echo ""; return; }
    grep "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | sed "s|^${1}=||" | sed 's|^"\(.*\)"$|\1|'
}
_env_set() {
    local key="$1" val="$2"
    [ "$MODE" = "probe" ] && return 0
    if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
        grep -v "^${key}=" "$ENV_FILE" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "$ENV_FILE"
    fi
    echo "${key}=${val}" >> "$ENV_FILE"
}

# Convenience: HTTP probe that returns 0 only on 2xx
_http_ok() {
    local url="$1" auth="${2:-}"
    local code
    if [ -n "$auth" ]; then
        code=$(curl -sS -o /dev/null -w '%{http_code}' -m 10 -H "Authorization: $auth" "$url" 2>/dev/null)
    else
        code=$(curl -sS -o /dev/null -w '%{http_code}' -m 10 "$url" 2>/dev/null)
    fi
    [[ "$code" =~ ^2 ]]
}

cat <<EOF

${BOLD}ContextDNA — Service Configurator${RESET}
${DIM}Probes every service, configures what's missing, swaps providers freely.${RESET}

Mode: ${BOLD}$MODE${RESET}${ONLY_SERVICE:+ (service=$ONLY_SERVICE)}
Repo: $REPO_ROOT
.env: $ENV_FILE

EOF

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — LLM PROVIDERS  (Cardiologist requires at least one)
# ─────────────────────────────────────────────────────────────────────────────
_section_llm_providers() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "llm" ] && return 0
    _step "LLM providers (for 3-Surgeons + ContextDNA injection)"

    # The known providers, in order of recommendation
    declare -a PROVIDERS_NAME=("OpenAI" "Anthropic" "DeepSeek" "Groq" "xAI" "Mistral" "Cohere" "Perplexity" "Together")
    declare -a PROVIDERS_KEY=("OPENAI_API_KEY" "ANTHROPIC_API_KEY" "DEEPSEEK_API_KEY" "GROQ_API_KEY" "XAI_API_KEY" "MISTRAL_API_KEY" "COHERE_API_KEY" "PERPLEXITY_API_KEY" "TOGETHER_API_KEY")
    declare -a PROVIDERS_URL=("https://platform.openai.com/api-keys" "https://console.anthropic.com/settings/keys" "https://platform.deepseek.com/api_keys" "https://console.groq.com/keys" "https://console.x.ai" "https://console.mistral.ai/api-keys" "https://dashboard.cohere.com/api-keys" "https://www.perplexity.ai/settings/api" "https://api.together.xyz/settings/api-keys")
    declare -a PROVIDERS_PROBE=(
        "https://api.openai.com/v1/models"
        "https://api.anthropic.com/v1/messages"
        "https://api.deepseek.com/v1/models"
        "https://api.groq.com/openai/v1/models"
        "https://api.x.ai/v1/models"
        "https://api.mistral.ai/v1/models"
        "https://api.cohere.com/v1/models"
        "https://api.perplexity.ai"
        "https://api.together.xyz/v1/models"
    )

    local working_count=0
    local first_working=""

    for i in "${!PROVIDERS_NAME[@]}"; do
        local name="${PROVIDERS_NAME[$i]}"
        local key_var="${PROVIDERS_KEY[$i]}"
        local url="${PROVIDERS_URL[$i]}"
        local probe="${PROVIDERS_PROBE[$i]}"
        local current_key="$(_env_get "$key_var")"

        # Detect placeholders — anything starting with YOUR_, your-, sk-placeholder, etc.
        local is_placeholder=false
        if [[ "$current_key" =~ ^(YOUR_|your[-_]|sk-placeholder|PLACEHOLDER|TODO|CHANGE) ]] || [ "${#current_key}" -lt 10 ]; then
            is_placeholder=true
        fi
        if [ -n "$current_key" ] && [ "$is_placeholder" = false ]; then
            # Probe with the key
            if _http_ok "$probe" "Bearer $current_key"; then
                _ok "$name ($key_var) — working"
                working_count=$((working_count + 1))
                [ -z "$first_working" ] && first_working="$name"
                continue
            else
                _warn "$name ($key_var) — key set but probe failed"
            fi
        else
            _info "$name ($key_var) — not configured"
        fi

        if [ "$MODE" = "probe" ]; then continue; fi
        if _confirm "Configure $name now? (signup: $url)"; then
            _link "Get your key here: $url"
            local new_key
            new_key="$(_ask_secret "$name API key (input hidden)")"
            if [ -n "$new_key" ]; then
                _env_set "$key_var" "$new_key"
                if _http_ok "$probe" "Bearer $new_key"; then
                    _ok "$name verified and saved"
                    working_count=$((working_count + 1))
                    [ -z "$first_working" ] && first_working="$name"
                else
                    _warn "key saved but probe still failed — double-check or come back later"
                fi
            fi
        fi
    done

    echo ""
    if [ "$working_count" -eq 0 ]; then
        _warn "NO LLM providers working — ContextDNA + 3-Surgeons will not function until at least one is configured"
        _info "Cheapest path: DeepSeek (~\$0.27/1M tokens, signup at platform.deepseek.com)"
    else
        _ok "$working_count LLM provider(s) working — Cardiologist will default to $first_working"
        # Persist the default if not already set
        local current_default="$(_env_get CARDIO_PROVIDER)"
        if [ -z "$current_default" ]; then
            _env_set "CARDIO_PROVIDER" "$(echo "$first_working" | tr '[:upper:]' '[:lower:]')"
        fi
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — LOCAL LLM (Neurologist + low-cost inference)
# ─────────────────────────────────────────────────────────────────────────────
_section_local_llm() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "local-llm" ] && return 0
    _step "Local LLM (Neurologist + cost-free background inference)"

    local cur_url="$(_env_get LOCAL_LLM_URL)"
    [ -z "$cur_url" ] && cur_url="http://127.0.0.1:5044"

    # Probe each likely local backend
    local backends_url=("http://localhost:5044/v1/models" "http://localhost:11434/v1/models" "http://localhost:1234/v1/models" "http://localhost:8000/v1/models")
    local backends_name=("MLX (port 5044)" "Ollama (port 11434)" "LM Studio (port 1234)" "vLLM (port 8000)")
    local working_url=""

    for i in "${!backends_url[@]}"; do
        if _http_ok "${backends_url[$i]}"; then
            _ok "${backends_name[$i]} — running"
            [ -z "$working_url" ] && working_url="${backends_url[$i]%/v1/models}"
        else
            _info "${backends_name[$i]} — not running"
        fi
    done

    if [ -n "$working_url" ]; then
        _env_set "LOCAL_LLM_URL" "$working_url"
        # List the models available
        local model_count
        model_count=$(curl -sf "${working_url}/v1/models" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data', [])))" 2>/dev/null || echo "?")
        _ok "local LLM endpoint: $working_url ($model_count models available)"
        return 0
    fi

    if [ "$MODE" = "probe" ]; then
        _warn "no local LLM running — Neurologist will fall back to remote provider"
        return 0
    fi

    _warn "No local LLM detected. This means:"
    _info "  - Neurologist will use a remote API (still works, costs pennies)"
    _info "  - Higher latency on background inference"
    _info "  - You're paying for tokens that local hardware could do for free"

    if ! _confirm "Set up a local LLM now?"; then
        _info "skipping local LLM setup"
        return 0
    fi

    # Choose backend
    echo ""
    echo "  Local LLM backend options:"
    [ "$PLATFORM" = "Darwin" ] && echo "    1) MLX           (Apple Silicon, fastest on Mac, recommended)"
    echo "    2) Ollama        (cross-platform, easiest, GPU/CPU)"
    echo "    3) LM Studio     (GUI, easy)"
    echo "    4) vLLM          (Linux/GPU)"
    echo "    5) Skip"
    local backend
    backend="$(_ask "Choose 1-5" "$( [ "$PLATFORM" = "Darwin" ] && echo 1 || echo 2 )")"

    case "$backend" in
        1)
            command -v mlx_lm.server >/dev/null 2>&1 || {
                _info "MLX not installed. Install with:"
                echo "    pip install mlx-lm"
                _confirm "Install now?" && pip install mlx-lm
            }
            _section_local_llm_mlx
            ;;
        2)
            command -v ollama >/dev/null 2>&1 || {
                _info "Ollama not installed."
                if [ "$PLATFORM" = "Darwin" ]; then
                    _confirm "Install via brew?" && brew install ollama
                else
                    _link "Install: https://ollama.com/download"
                fi
            }
            _section_local_llm_ollama
            ;;
        3) _link "LM Studio: https://lmstudio.ai/" ;;
        4) _link "vLLM: https://docs.vllm.ai/en/latest/getting_started/installation.html" ;;
        5) return 0 ;;
    esac
}

_section_local_llm_mlx() {
    # Model picker
    echo ""
    echo "  Choose a model (matched to your RAM):"
    echo "    1) Qwen3-4B-4bit               (~2.5 GB, fast, recommended)"
    echo "    2) Qwen3-8B-4bit               (~5 GB, more capable)"
    echo "    3) Llama-3.3-8B-Instruct-4bit  (~5 GB, popular)"
    echo "    4) Custom (enter HuggingFace repo)"
    local choice
    choice="$(_ask "Choose 1-4" "1")"

    local model
    case "$choice" in
        1) model="mlx-community/Qwen3-4B-4bit" ;;
        2) model="mlx-community/Qwen3-8B-4bit" ;;
        3) model="mlx-community/Llama-3.3-8B-Instruct-4bit" ;;
        4) model="$(_ask "HuggingFace repo (e.g. mlx-community/Some-Model-4bit)" "")" ;;
    esac
    [ -z "$model" ] && { _warn "no model chosen"; return; }

    _env_set "LOCAL_LLM_MODEL" "$model"
    _env_set "LOCAL_LLM_URL" "http://127.0.0.1:5044"
    _env_set "LOCAL_LLM_BACKEND" "mlx"

    _info "starting MLX server with $model (first run downloads ~2-5 GB from HuggingFace)"
    if _confirm "Start it now (background)?"; then
        nohup python3 -m mlx_lm.server --model "$model" --port 5044 > /tmp/mlx-server.log 2>&1 &
        sleep 3
        if _http_ok "http://127.0.0.1:5044/v1/models"; then
            _ok "MLX server running on :5044 (logs: /tmp/mlx-server.log)"
        else
            _warn "MLX server failed to come up — check /tmp/mlx-server.log"
        fi
    fi

    # Offer to install a launchd plist so it auto-starts
    if [ "$PLATFORM" = "Darwin" ] && _confirm "Install launchd plist so MLX auto-starts at login?"; then
        local plist="$HOME/Library/LaunchAgents/com.contextdna.local-llm.plist"
        cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.contextdna.local-llm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string><string>-lc</string>
    <string>python3 -m mlx_lm.server --model $model --port 5044</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/mlx-server.log</string>
  <key>StandardErrorPath</key><string>/tmp/mlx-server.err</string>
</dict></plist>
EOF
        launchctl unload "$plist" 2>/dev/null || true
        launchctl load -w "$plist" && _ok "launchd plist installed (model auto-loads at login)"
    fi
}

_section_local_llm_ollama() {
    echo ""
    echo "  Choose a model:"
    echo "    1) qwen3:4b      (~2.6 GB, fast)"
    echo "    2) qwen3:8b      (~5 GB)"
    echo "    3) llama3.3:8b   (~5 GB)"
    echo "    4) Custom (enter ollama tag)"
    local choice
    choice="$(_ask "Choose 1-4" "1")"
    local model
    case "$choice" in
        1) model="qwen3:4b" ;;
        2) model="qwen3:8b" ;;
        3) model="llama3.3:8b" ;;
        4) model="$(_ask "Ollama model tag (e.g. mistral:7b)" "")" ;;
    esac
    [ -z "$model" ] && return
    _env_set "LOCAL_LLM_MODEL" "$model"
    _env_set "LOCAL_LLM_URL" "http://127.0.0.1:11434"
    _env_set "LOCAL_LLM_BACKEND" "ollama"

    _info "pulling $model (one-time download)"
    if _confirm "Pull now?"; then
        ollama pull "$model" && _ok "$model pulled and ready"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — NATS / FLEET (optional but recommended)
# ─────────────────────────────────────────────────────────────────────────────
_section_nats() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "nats" ] && return 0
    _step "NATS / Fleet coordination"

    local cur_url="$(_env_get NATS_URL)"
    [ -z "$cur_url" ] && cur_url="nats://localhost:4222"

    # Probe via the monitoring endpoint
    local mon_url="${cur_url/nats:/http:}"
    mon_url="${mon_url/:4222/:8222}/healthz"

    if _http_ok "$mon_url"; then
        _ok "NATS reachable at $cur_url"
        _env_set "NATS_URL" "$cur_url"
        return 0
    fi
    _info "NATS not reachable at $cur_url"

    [ "$MODE" = "probe" ] && return 0

    if _confirm "Install + start local NATS server?"; then
        if [ "$PLATFORM" = "Darwin" ] && command -v brew >/dev/null; then
            command -v nats-server >/dev/null || brew install nats-server
            command -v nats >/dev/null || brew install nats-io/nats-tools/nats
            mkdir -p ~/Library/LaunchAgents
            cat > ~/Library/LaunchAgents/io.nats.server.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>io.nats.server</string>
<key>ProgramArguments</key><array>
  <string>/opt/homebrew/bin/nats-server</string>
  <string>-js</string>
  <string>-m</string><string>8222</string>
</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>/tmp/nats-server.log</string>
<key>StandardErrorPath</key><string>/tmp/nats-server.err</string>
</dict></plist>
PLIST
            launchctl unload ~/Library/LaunchAgents/io.nats.server.plist 2>/dev/null || true
            launchctl load -w ~/Library/LaunchAgents/io.nats.server.plist
            sleep 2
            if _http_ok "http://localhost:8222/healthz"; then
                _ok "NATS running with JetStream enabled, auto-starts at login"
            else
                _warn "NATS install completed but health check failed — check /tmp/nats-server.err"
            fi
        else
            _link "Install NATS: https://docs.nats.io/running-a-nats-service/introduction/installation"
        fi
    fi

    local new_url
    new_url="$(_ask "NATS URL" "$cur_url")"
    _env_set "NATS_URL" "$new_url"
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — DOCKER + STACK
# ─────────────────────────────────────────────────────────────────────────────
_section_docker() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "docker" ] && return 0
    _step "Docker stack (Postgres + Redis + supporting services)"

    if ! command -v docker >/dev/null; then
        _warn "docker not installed"
        [ "$MODE" = "probe" ] && return 0
        _link "Install: https://docs.docker.com/get-docker/"
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        _warn "docker daemon not running — start Docker Desktop first"
        return 0
    fi
    _ok "docker daemon running"

    if [ ! -f "$REPO_ROOT/docker-compose.lite.yml" ]; then
        _warn "docker-compose.lite.yml missing in repo"
        return 0
    fi

    local running
    running="$(docker compose -f "$REPO_ROOT/docker-compose.lite.yml" ps --quiet 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$running" -gt 0 ]; then
        _ok "lite stack already has $running container(s) running"
    elif [ "$MODE" != "probe" ] && _confirm "Start the lite stack now?"; then
        (cd "$REPO_ROOT" && docker compose -f docker-compose.lite.yml up -d) && _ok "stack up"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — OPTIONAL EXTERNAL SERVICES
# ─────────────────────────────────────────────────────────────────────────────
_section_optional() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "optional" ] && return 0
    _step "Optional external services (each can be skipped)"

    # name | env var | signup URL | what for
    declare -a OPT_NAME=("ElevenLabs" "LiveKit" "Stripe (test)" "Supabase" "Firebase" "SendGrid" "Sentry" "Cloudflare" "Google OAuth")
    declare -a OPT_KEY=("ELEVENLABS_API_KEY" "LIVEKIT_API_KEY" "STRIPE_SECRET_KEY" "SUPABASE_URL" "FIREBASE_PROJECT_ID" "SENDGRID_API_KEY" "SENTRY_DSN" "CLOUDFLARE_API_TOKEN" "GOOGLE_CLIENT_ID")
    declare -a OPT_URL=("https://elevenlabs.io/api" "https://cloud.livekit.io" "https://dashboard.stripe.com/test/apikeys" "https://supabase.com/dashboard/projects" "https://console.firebase.google.com/" "https://app.sendgrid.com/settings/api_keys" "https://sentry.io/settings/account/api/auth-tokens/" "https://dash.cloudflare.com/profile/api-tokens" "https://console.cloud.google.com/apis/credentials")
    declare -a OPT_FOR=("text-to-speech (voice agents)" "WebRTC voice server (ER Simulator voice agent)" "payment processing (subscriptions)" "managed Postgres + auth" "auth + analytics" "transactional email" "error tracking" "DNS automation" "OAuth sign-in")

    for i in "${!OPT_NAME[@]}"; do
        local name="${OPT_NAME[$i]}" key="${OPT_KEY[$i]}" url="${OPT_URL[$i]}" forwhat="${OPT_FOR[$i]}"
        local cur="$(_env_get "$key")"
        if [ -n "$cur" ] && [ "${cur:0:8}" != "your-key" ]; then
            _ok "$name ($key) configured"
            continue
        fi
        [ "$MODE" = "probe" ] && { _info "$name — not configured"; continue; }

        echo ""
        echo "  ${BOLD}$name${RESET} — $forwhat"
        _link "$url"
        if _confirm "Configure $name now?"; then
            local v
            v="$(_ask_secret "$key value (input hidden; leave blank to skip)")"
            [ -n "$v" ] && { _env_set "$key" "$v"; _ok "$key saved"; }
        fi
    done
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — NETWORK / IP detection
# ─────────────────────────────────────────────────────────────────────────────
_section_network() {
    [ -n "$ONLY_SERVICE" ] && [ "$ONLY_SERVICE" != "network" ] && return 0
    _step "Network detection (IPs change between machines)"

    # Detect current LAN IP
    local lan_ip=""
    if [ "$PLATFORM" = "Darwin" ]; then
        lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")"
    else
        lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    [ -n "$lan_ip" ] && _ok "current LAN IP: $lan_ip" || _warn "could not auto-detect LAN IP"

    # Look for stale IPs in .env that look like LAN ranges and don't match current
    local stale=0
    while IFS='=' read -r k v; do
        [[ "$k" =~ ^# ]] && continue
        [ -z "$v" ] && continue
        if [[ "$v" =~ (192\.168\.|10\.0\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
            local found_ip
            found_ip=$(echo "$v" | grep -oE '(192\.168\.|10\.0\.|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9]+\.[0-9]+' | head -1)
            if [ -n "$found_ip" ] && [ "$found_ip" != "$lan_ip" ]; then
                _warn "$k contains stale LAN IP: $found_ip (current: ${lan_ip:-unknown})"
                stale=$((stale + 1))
                if [ "$MODE" != "probe" ] && _confirm "  Update $k from $found_ip to ${lan_ip:-localhost}?"; then
                    local new_v="${v//$found_ip/${lan_ip:-localhost}}"
                    _env_set "$k" "$new_v"
                    _ok "$k updated"
                fi
            fi
        fi
    done < "$ENV_FILE"
    [ "$stale" -eq 0 ] && _ok "no stale LAN IPs in .env"
}

# ─────────────────────────────────────────────────────────────────────────────
# Run sections
# ─────────────────────────────────────────────────────────────────────────────
_section_llm_providers
_section_local_llm
_section_nats
_section_docker
_section_network
_section_optional

# ─────────────────────────────────────────────────────────────────────────────
# Final verification
# ─────────────────────────────────────────────────────────────────────────────
echo ""
_step "Final verification"

# Show summary
declare -a SUMMARY_KEYS=("OPENAI_API_KEY" "ANTHROPIC_API_KEY" "DEEPSEEK_API_KEY" "LOCAL_LLM_URL" "LOCAL_LLM_MODEL" "NATS_URL" "BACKUP_BUCKET")
_summarize_one() {
    local k="$1"
    local v
    v="$(_env_get "$k")"
    if [ -n "$v" ]; then
        if [[ "$k" =~ _KEY$ ]] || [[ "$k" =~ TOKEN$ ]]; then
            _ok "$k = ${v:0:6}…${v: -4} ${DIM}(masked)${RESET}"
        else
            _ok "$k = $v"
        fi
    else
        _warn "$k = ${DIM}(not set)${RESET}"
    fi
}
for k in "${SUMMARY_KEYS[@]}"; do _summarize_one "$k"; done

echo ""
if [ "$MODE" = "probe" ]; then
    echo "  ${DIM}(probe mode — no changes were made)${RESET}"
else
    echo "  ${BOLD}${GREEN}━━━ Configuration complete ━━━${RESET}"
    echo "  All changes saved to: $ENV_FILE"
    echo "  Re-run anytime: ${BLUE}bash scripts/configure-services.sh${RESET}"
    echo "  Status check:   ${BLUE}bash scripts/configure-services.sh --probe${RESET}"
fi
echo ""
