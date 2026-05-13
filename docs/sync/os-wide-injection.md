# OS-Wide Context Injection — Design Sketch

> Phase 6 direction. The mothership stops being an IDE companion and becomes the operating-system-level memory layer that every AI prompt on the box benefits from. Design only — no implementation expected at the time of writing. The contract is what matters; the code follows when Phases 1-5 are stable.

---

## 1. The Vision

Today the subconscious feeds the IDE. Claude Code, Cursor, and VS Code each ask the mothership for a 9-section payload at prompt time, splice it in front of the user's question, and ship the combined prompt to a frontier model. That single integration already moves first-try success from ~60% to ~90%+ on the projects we've measured. But the integration is hard-wired to the IDE socket. The same payload — the same S0 safety rails, the same S2 professor wisdom, the same S6 holistic context — is invisible to every other AI surface on the same machine. Slack's AI replies hallucinate the codebase. Browser-based chatbots get nothing. The terminal AI assistants (continue, aider) reinvent context every session. Notion AI doesn't know what shipped yesterday. Voice assistants address you as a stranger.

Tomorrow the mothership exposes the same payload to **any AI-capable app on the operating system**. Slack AI replies subscribe and get the project context behind a thread. Browser extensions inject the payload into ChatGPT, Claude.ai, and Gemini before the user hits send. Terminal CLIs pull the payload before constructing an aider session. Email composition AI gets the recent-decisions snapshot for the recipient. Notion AI knows what the codebase actually looks like. Voice assistants stop being amnesiac. The mothership becomes the **operating-system-level memory layer** — one capture loop, one evidence ledger, one 9-section payload, many consumers. The local LLM bet gets stronger here: powerhouse cloud models stay good at reasoning, but the *contextual substrate* lives on your machine, owned by you, never leaving the box.

---

## 2. Three Transports

The mothership exposes the inject contract over three transports so any app on the OS can subscribe through whatever it already speaks. All three carry the same `ContextPayload` (see `MOTHERSHIP-ARCHITECTURE.md` §3 for the typed schema).

### 2.1 NATS subject — `injection.payload.v1.<client_id>`

For apps that can already speak NATS — Python services, Go tools, the local-LLM proxy on port 5044, fleet daemons, future first-party services. The mothership publishes payloads on demand to a subject scoped by client; clients subscribe via the existing JetStream cluster (mac1/mac2/mac3/cloud).

- **Subject convention**: `injection.payload.v1.<client_id>` — mirrors the existing `event.webhook.completed.<node>` pattern so JetStream replicas, retention, and observability work unchanged.
- **Version prefix `v1`**: lets us evolve the payload schema (S0-S10 sections, evidence taxonomy) without breaking existing clients. A `v2` subject runs in parallel during deprecation windows.
- **Request/reply**: clients publish a `request.payload.v1.<client_id>` message carrying `{topic, max_tokens, sections_wanted}`; mothership replies on `injection.payload.v1.<client_id>`. Standard NATS request/reply.
- **Use-cases**: local-LLM proxy enriches every cloud-bound prompt; fleet daemons inject context before remote build kicks off; Python data tools pull payload before generating SQL.

### 2.2 Local HTTP SSE — `http://127.0.0.1:8879/api/v1/inject`

For apps that prefer HTTP or can't speak NATS — browser extensions, Electron apps, anything that already has a `fetch()` call in its hand. The existing event-bridge daemon on `:8879` (see `contextdna-ide-oss/draft/profiles/lite.yaml`) already serves SSE for IDE events; we extend it with one inject endpoint.

- **Endpoint**: `GET http://127.0.0.1:8879/api/v1/inject?topic=<topic>&client_id=<id>&sections=s0,s1,s2`
- **Modes**:
  - **One-shot JSON**: `Accept: application/json` → single response, full payload, then close.
  - **SSE stream**: `Accept: text/event-stream` → mothership pushes payload + future deltas (S6 holistic context refreshes as new evidence promotes).
- **Auth**: `Authorization: Bearer <per-client-token>` header, issued at client registration (§4).
- **Use-cases**: browser extension splices payload into ChatGPT prompt; Electron menu-bar app shows current S6 snapshot; web-based admin panels.

### 2.3 Unix domain socket — `/tmp/contextdna-inject.sock`

For low-latency in-process apps — terminal CLIs, system-level integrations, the inject daemon's own subprocess children. Mirrors the existing `/tmp/contextdna-event-bridge.sock` convention so apps that already speak the event-bridge protocol pick up inject for free.

- **Wire format**: length-prefixed JSON frames (`<4-byte big-endian length><JSON>`).
- **Latency**: sub-millisecond round-trip; no TCP overhead, no auth handshake beyond the socket's filesystem permissions (`0600`, owner-only).
- **Use-cases**: shell wrappers (`contextdna-inject` CLI), tmux preprocessor hooks, system-wide keyboard-shortcut daemons, any tool that runs in the user's session and inherits the socket's permission scope.

---

## 3. Client SDK Sketches

Pseudocode. The interfaces are the contract; the implementations are Phase 6 work.

### 3.1 Python

```python
from contextdna_inject import Subscriber

s = Subscriber(client_id="my-slack-bot")          # reads token from ~/.contextdna/clients/my-slack-bot.toml
payload = s.request(topic="auth refactor", max_tokens=4000, sections=["s0", "s1", "s2", "s6"])

print(payload.s2_professor_wisdom)                # ProfessorWisdom model from MOTHERSHIP-ARCHITECTURE §3
print(payload.s0_safety_rails.forbidden_patterns) # e.g. ["except Exception: pass"]

# Streaming variant — refreshes when S6 updates
for delta in s.subscribe(topic="auth refactor"):
    update_slack_thread_context(delta)
```

### 3.2 TypeScript / Node

```typescript
import { ContextDNAClient } from '@contextdna/inject';

const c = new ContextDNAClient({ clientId: 'browser-ext' });
const p = await c.request({ topic: 'fix bug X', sections: ['s0', 's2'] });

console.log(p.s2_professor_wisdom.patterns);

// Streaming
const stream = c.subscribe({ topic: 'fix bug X' });
for await (const delta of stream) {
  injectIntoActivePromptBox(delta);
}
```

### 3.3 Shell

```sh
# One-shot — pipe straight into jq
contextdna-inject --topic "$PROMPT" --json | jq '.s2_professor_wisdom'

# Splice into an aider session
PAYLOAD=$(contextdna-inject --topic "$1" --format markdown)
aider --read-prompt <(echo "$PAYLOAD"; echo "$1")

# Streaming, line-delimited JSON
contextdna-inject --topic "$PROMPT" --stream | while read -r delta; do
  echo "$delta" | jq '.s6_holistic'
done
```

### 3.4 Browser

```javascript
// Browser extension — splice into ChatGPT prompt box before send
const res = await fetch(
  `http://127.0.0.1:8879/api/v1/inject?topic=${encodeURIComponent(userPrompt)}&client_id=browser-ext&sections=s0,s1,s2`,
  { headers: { 'Authorization': `Bearer ${EXT_TOKEN}` } }
);
const payload = await res.json();

const promptBox = document.querySelector('#prompt-textarea');
promptBox.value = renderPayload(payload) + '\n\n' + promptBox.value;
```

---

## 4. Permissions & Sandboxing

Not every client needs every section. A Slack bot shouldn't see S0 safety rails for your medical-records project. A browser extension probably doesn't need S8 8th-intelligence reflection. A terminal CLI in `~/work/erslp` should never see evidence categorised as `personal` or `medical` or `financial`. The mothership treats every client as untrusted until the user approves it, and even then only grants the narrowest possible slice.

### 4.1 Client registration — `~/.contextdna/clients/<client_id>.toml`

```toml
# Example: ~/.contextdna/clients/my-slack-bot.toml
client_id = "my-slack-bot"
display_name = "Slack AI Reply Helper"
approved_at = "2026-05-13T14:22:00Z"
approved_by = "aaron"                  # which OS user approved
token = "ctxdna_live_a1b2c3..."        # opaque, presented on every request

# Which payload sections this client may receive
subscribed_sections = ["s0", "s1", "s2", "s6"]

# Which evidence categories the mothership filters in
authorized_categories = ["code", "marketing"]
# Implicitly forbidden: ["personal", "medical", "financial", "private-keys", ...]

# Optional per-client rate limit (requests / minute)
rate_limit = 30

# Optional topic allowlist — empty means "any topic"
topic_allowlist = []
```

### 4.2 Per-section opt-in

Clients declare which sections they want at registration. The mothership returns `null` (not stub data, not silent omission — explicit `null` with a `denied_reason` field in `SectionMeta`) for any section the client didn't subscribe to. ZSF: every refusal increments a `denied_section_requests{client_id, section}` counter on `/health`. Silent skipping is forbidden.

### 4.3 Evidence-category authorization

Even within a section, evidence carries a category tag (see "stable evidence taxonomy" in §7). The mothership filters the source list inside each section to only the categories the client is authorized for. A Slack bot subscribed to S2 only sees professor wisdom that derived from `code` and `marketing` evidence — never wisdom that distilled from a `medical` or `financial` source.

### 4.4 Approval CLI — no auto-trust

```sh
# A new client appears (it tried to register, got 401, mothership recorded the attempt)
context-dna-ide client list --pending
# → my-slack-bot   (requested 2026-05-13 14:18 from 127.0.0.1, 3 attempts)

context-dna-ide client approve my-slack-bot \
    --sections s0,s1,s2,s6 \
    --categories code,marketing \
    --rate-limit 30
# → Approved. Token written to ~/.contextdna/clients/my-slack-bot.toml
# → Token shown ONCE; copy it into the client's config now.

context-dna-ide client revoke my-slack-bot   # rotate / disable any time
context-dna-ide client audit my-slack-bot    # show every request in the last 30d
```

The user is always the gate. The mothership never auto-trusts an inbound `client_id`. Unknown clients get 401 + an entry in the pending-approvals queue. Aaron approves explicitly — never the daemon, never a heuristic.

### 4.5 Token model

Tokens are opaque, per-client, revocable. Format: `ctxdna_live_<32 hex>` (mirrors the `sk_live_` / `xoxb-` convention so they're recognisable in stray logs). Stored hashed in the mothership; presented in cleartext on every request. Compromise of a token reveals only what the client was already authorized to see; revocation is one CLI call.

---

## 5. OS Integration Patterns

The inject daemon needs to start at user login, expose its sockets in OS-discoverable locations, and behave like a native service on each platform. Concrete patterns:

### 5.1 macOS — launchd + XPC

- Ship a launchd plist: `~/Library/LaunchAgents/io.contextdna.inject.plist`. RunAtLoad + KeepAlive. Already half-built — the event-bridge plist (`io.contextdna.event-bridge.plist`) is the template.
- **Service Management framework**: register the daemon with `SMAppService` for "Allow in Background" UX in System Settings.
- **Discovery**: apps connect via `xpc_connection_create_mach_service("io.contextdna.inject", ...)` for the in-process XPC variant, or fall back to the unix socket at `/tmp/contextdna-inject.sock`.
- **Permissions**: macOS's per-app TCC prompts inherit — the daemon is the gate, not the OS. The OS just runs it.

### 5.2 Linux — systemd + D-Bus

- Systemd user unit: `~/.config/systemd/user/contextdna-inject.service`. `Type=notify`, `WantedBy=default.target`. `systemctl --user enable contextdna-inject`.
- **D-Bus service**: register `io.contextdna.inject` on the user session bus for native D-Bus consumers (GNOME extensions, KDE Plasma, system trays).
- **Unix socket**: `$XDG_RUNTIME_DIR/contextdna-inject.sock` — preferred over `/tmp` on Linux because `XDG_RUNTIME_DIR` is owner-only by spec and tied to the login session lifecycle.
- **AppArmor / SELinux**: ship a profile that confines the daemon to `~/.contextdna/`, the NATS port, and `:8879`. Distros that don't enforce MAC fall back to standard UNIX perms.

### 5.3 Windows — Scheduled Task + Named Pipe

- **Auto-start**: scheduled task triggered `At log on of any user`, configured for the highest reasonable privilege (interactive user, not SYSTEM — the daemon must run as the user whose evidence it serves).
- **IPC**: named pipe `\\.\pipe\contextdna-inject` for the local equivalent of the unix socket. Frame format identical to the unix socket transport so SDKs share serialisation code.
- **HTTP / NATS**: same as macOS / Linux — `127.0.0.1:8879` and the NATS cluster work unchanged.
- **Service vs Task**: Windows Service is overkill (and complicates the per-user evidence model). Scheduled Task at logon is sufficient and keeps the daemon scoped to the right session.

---

## 6. Future — Payload-Aware Models

Speculative — for the back half of the decade, not Phase 6 launch.

Today the payload is rendered to text and prepended to the user's prompt. The cloud model treats it as ordinary prefix tokens, parses it heuristically, and sometimes ignores sections under load. That works, but it leaves leverage on the table — every model spends tokens re-parsing structured data it could have consumed natively.

Powerhouse models that understand the S0-S8 schema structurally would change the economics. Instead of "here are 4000 tokens of preamble, please read it," the API call carries `{prompt, context: ContextPayload}` as a typed second argument; the model's attention masks treat each section with the right priority (S0 forbidden patterns get hard constraint weight; S2 professor wisdom gets retrieval-augmentation weight; S6 holistic context gets working-memory weight). Section confidence becomes a routing signal — low-confidence sections get less attention; high-confidence sections get more.

Two paths to get there:

1. **Upstream provider cooperation** — convince Anthropic / OpenAI / Google to accept a typed payload argument in their APIs. Long timeline. Requires a critical mass of clients sending the same schema (i.e., the OSS mothership has to ship and spread first).
2. **Fine-tuned open-weight models** — ship them inside the mothership. A local 70B or MoE fine-tuned on `{ContextPayload, user_prompt} → response` triples. Aaron's local-LLM bet gets stronger here: the model that natively understands your payload schema is the model you trained on your own evidence ledger. The cloud frontier models become a fallback for "this prompt needs raw horsepower and no project memory"; the local payload-aware model handles everything else.

Either path is downstream of Phase 6 shipping. The contract has to exist first.

---

## 7. What's Needed Before Phase 6 Ships

Prerequisites — Phase 6 cannot start until all of these are stable.

1. **Stable S0-S8 (and S10) payload schema** — `ContextPayload(BaseModel)` is already drafted in `MOTHERSHIP-ARCHITECTURE.md §3`. Lock it. A `v1` subject means we have to honour `v1` forever (or run parallel `v1` + `v2` during deprecation), so the schema can't churn after launch.
2. **Stable evidence taxonomy** — the categories in `authorized_categories` (code / marketing / personal / medical / financial / ...) need to be canonical. Today they're implicit in how evidence is tagged at capture time; for OS-wide injection they become an enforced authorization boundary.
3. **The inject daemon itself** — extend the existing event-bridge on `:8879` with the `/api/v1/inject` endpoint, NATS subject publisher (`injection.payload.v1.*`), and unix-socket frame handler (`/tmp/contextdna-inject.sock`). Reuse the event-bridge's process lifecycle, plist, and health endpoint — don't ship a second long-running process.
4. **Client SDK packaging** — Python (`contextdna-inject` on PyPI), Node (`@contextdna/inject` on npm), shell (`contextdna-inject` Homebrew formula + apt package). Each SDK is thin; the contract is the work.
5. **Approval workflow CLI** — `context-dna-ide client list/approve/revoke/audit` subcommands. Hooks into the existing `~/.contextdna/` config tree. Pending-approval queue surfaces in the `fleet-check.sh` dashboard.
6. **Security review** — explicit sandbox model + threat model, signed off by the 3-surgeon protocol (cardio reviews secrets handling, neurologist challenges the auth model, Atlas synthesizes). Concrete threats to model: untrusted local processes scraping `/tmp` for the socket; compromised browser extensions exfiltrating tokens; malicious clients enumerating evidence categories by probing for 403s; token-rotation race conditions during revocation.

When all six exist and have been steady for at least one shipping cycle, Phase 6 starts. Not before.

---

## 8. What This Document Is Not

This is not an implementation plan. No code is required by anything written above. The transports, SDK sketches, registration format, and OS integration patterns are the **contract** — the externally observable shape Phase 6 must conform to. The implementation lives downstream of Phases 1-5 and starts when the mothership has shipped and stabilised.

No timeline commitment. No engineering estimate. No file-paths-to-create. This is direction, not destination.

If a future session is tempted to start building from this document: stop. Read `docs/plans/2026-05-13-contextdna-ide-mothership-plan.md` first. Phase 6 is the *last* phase for a reason.
