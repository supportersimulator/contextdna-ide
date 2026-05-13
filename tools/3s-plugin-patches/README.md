# 3-Surgeons CLI Patches — DeepSeek Primary + OpenAI Optional

Restores `3s consult` / `3s probe` capability without requiring an OpenAI API key.

## What's broken upstream

The shipped 3-Surgeons plugin (v1.0.0 at
`~/.claude/plugins/cache/3-surgeons-marketplace/3-surgeons/1.0.0/`) only reads API
keys from environment variables. If `Context_DNA_OPENAI` isn't exported in the
shell, `3s probe` fails immediately:

```
Cardiologist: FAIL -- API key missing. Set Context_DNA_OPENAI env var.
Neurologist:  FAIL -- API key missing. Set Context_DNA_OPENAI env var.
```

But this fleet stores all API keys in macOS Keychain
(`service=fleet-nerve, account=<KEY_NAME>`), so the env var is rarely set.
Result: 3s is unusable in practice, blocking the 3-Surgeon protocol.

## What this patch does

Two minimal changes to the plugin source — everything else is untouched.

### Patch 1: `three_surgeons/core/config.py` — Keychain fallback

`SurgeonConfig.get_api_key()` now falls back to macOS Keychain if the env var
is unset or too short. The Keychain service defaults to `fleet-nerve` and can
be overridden via `THREE_SURGEONS_KEYCHAIN_SERVICE`. Lookups are cached per
process. Values are never logged.

### Patch 2: `three_surgeons/cli/main.py` — Probe shows provider, never blocks

`3s probe` now reports which provider succeeded:

```
Cardiologist: OK (provider=deepseek, model=deepseek-chat, 412ms)
Neurologist:  FAIL (no local LLM on :5044/:11434, fallback=openai key-missing)
```

Probe exits 0 if at least the cardiologist passes (degraded-but-operational
mode — Atlas + Cardiologist still gives 2-of-3 consensus).

### Patch 3: `~/.3surgeons/config.yaml` — DeepSeek primary

Cardiologist: DeepSeek (`api.deepseek.com/v1`, `deepseek-chat`) primary, OpenAI
secondary fallback.

Neurologist: MLX local (:5044) primary, Ollama (:11434) fallback, then
DeepSeek/OpenAI cloud as last-resort fallbacks so consult always has 2 surgeons.

## Install

```bash
bash tools/3s-plugin-patches/apply.sh
```

This copies the two patched files into the plugin tree and verifies via
`3s probe`. To roll back:

```bash
bash tools/3s-plugin-patches/apply.sh --revert
```

## Verify

```bash
3s probe
3s consult "what is 2+2?"
```

## Optional: route Cardiologist via ContextDNA Claude Bridge (B3 #2)

By **default**, the Cardiologist talks **direct** to `api.deepseek.com` — most
resilient (no daemon dependency) and lowest latency. **Opt-in** to route it
through the local ContextDNA Claude Bridge (`localhost:8855/v1`) when you want:

- **Unified observability** — `/metrics` shows 3-Surgeons consult traffic
  alongside Aaron's Claude Code traffic on the same `bridge_*` counters.
- **Auto Anthropic→DeepSeek fallback** — the bridge tries Anthropic upstream
  first when an `ANTHROPIC_API_KEY` (or OAuth Bearer) is configured, falling
  to DeepSeek on 429/5xx. Cardiologist gets full Claude quality when quota
  allows; same DeepSeek path otherwise.
- **Single counter rotation** — one place to scrape for SLO dashboards.

### Opt-in (one of three ways)

```bash
# 1) Env-driven (preferred — survives plugin re-patches via apply.sh)
export THREE_SURGEONS_VIA_BRIDGE=1   # add to ~/.zshrc / ~/.bashrc
bash tools/3s-plugin-patches/apply.sh

# 2) Explicit flag (no env var needed)
python3 tools/3s-plugin-patches/apply.py --route-via-bridge

# 3) Revert to direct
python3 tools/3s-plugin-patches/apply.py --route-direct
```

The patcher rewrites `~/.3surgeons/config.yaml` so the cardiologist endpoint
points at `http://localhost:8855/v1` and **inserts direct DeepSeek as the
first 3s-level fallback**. If the bridge goes down mid-session, 3s falls
through to `api.deepseek.com` automatically — the routing is best-effort,
never a hard dependency.

### Verify the route

```bash
# Counters should increment after each `3s consult` / `3s probe` when opted in:
curl -s http://localhost:8855/metrics | grep bridge_chat_completions
#   bridge_chat_completions_requests          → all calls
#   bridge_chat_completions_anthropic_ok      → Anthropic upstream answered
#   bridge_chat_completions_fallback_to_deepseek → Anthropic 429/5xx → DeepSeek
#   bridge_chat_completions_skipped           → no Anthropic key configured
#   bridge_chat_completions_failures          → both upstreams failed
```

### When to keep direct (default)

- Bridge daemon (`io.contextdna.fleet-nats`) is unstable on this node.
- You need lowest possible cardio latency (bridge adds ~600-800ms hop).
- You're running `3s consult` from a node that doesn't run the bridge.

## Caveat

The plugin lives under `~/.claude/plugins/cache/.../1.0.0/`, which Claude Code
may overwrite when it refreshes the plugin marketplace. Re-run `apply.sh` after
plugin updates. A long-term fix is to upstream this into the
`3-surgeons-marketplace` repo.
