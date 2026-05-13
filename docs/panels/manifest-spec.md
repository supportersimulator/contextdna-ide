# Universal Panel Manifest — Specification

**Status:** Draft v1
**Audience:** Third-party panel authors + mothership runtime engineers
**Companion docs:** [MOTHERSHIP-ARCHITECTURE.md §5](../../draft/MOTHERSHIP-ARCHITECTURE.md), [contextdna-ide-mothership-plan.md](../../../docs/plans/2026-05-13-contextdna-ide-mothership-plan.md)

---

## 1. Overview

A **panel** is a self-contained coding-domain plug-in that mounts into the ContextDNA IDE shell (DockView substrate) and contributes events to the mothership's evidence ledger. Panels are how the mothership grows beyond a fixed application into a universal coding cockpit — every domain (HuggingFace, mobile, robotics, marketing automation, embedded firmware, third-party brains, full repos) is a panel.

The `panel.manifest.json` is the single source of truth that lets the substrate decide:

- whether a panel is installable on this machine
- which env vars / MCP servers / credentials it depends on
- which evidence events it produces and consumes
- where its UI gets mounted and with what permissions
- which subconscious distillations should be routed to it

The manifest is the contract — and the contract is bilateral:

| The panel author guarantees | The mothership runtime guarantees |
|---|---|
| The manifest accurately declares **all** env, MCP, credentials, and filesystem/network reach. | Missing dependencies surface as a typed install error — never a silent half-mount. |
| Events emitted match the declared `evidence_emits` types and schemas. | Events from other panels arrive only on declared `evidence_subscribes` types — no leakage. |
| The panel never writes to the evidence ledger directly. | Writes flow through validated `RawEvent` contributors; provenance is auto-stamped. |
| The panel respects sandboxing (read/write only declared paths, network only declared hosts). | Panels are sandboxed accordingly — undeclared reach raises a runtime permission error. |
| Semver discipline (breaking changes → major bump). | The substrate honors `mothership_min_version` — incompatible panels refuse to mount, not crash. |

If any guarantee on either side fails, the install / mount fails loudly. **There are no silent half-mounts.** (This is the Zero Silent Failures invariant applied to the panel system.)

---

## 2. `panel.manifest.json` Schema

Every panel ships a `panel.manifest.json` at its repository root. The full schema:

```json
{
  "id": "huggingface-hub-v2",
  "name": "HuggingFace Hub",
  "version": "0.3.1",
  "mothership_min_version": "0.5.0",

  "author": {
    "name": "Jane Author",
    "email": "jane@example.com",
    "github": "janeauthor"
  },

  "description": "Browse, pull, and run inference on HuggingFace models and datasets.",

  "domains": ["ml.training", "ml.inference", "huggingface"],
  "language_affinity": ["python", "rust", "shell"],

  "evidence_emits": [
    "pattern.discovered",
    "landmine.confirmed",
    "outcome.observed",
    "panel.huggingface.model_pulled"
  ],
  "evidence_subscribes": [
    "wisdom.requested",
    "evidence.claim.proposed",
    "evidence.wisdom.promoted"
  ],

  "mount": {
    "type": "react",
    "entry": "dist/panel.js",
    "props": {
      "defaultTab": "models",
      "telemetry": false
    }
  },

  "requires_env": {
    "HF_TOKEN": {
      "required": true,
      "description": "HuggingFace API token (read scope minimum).",
      "secret": true
    },
    "HF_CACHE_DIR": {
      "required": false,
      "description": "Local cache for pulled artifacts.",
      "secret": false
    }
  },

  "requires_mcp_servers": ["huggingface-mcp"],
  "requires_credentials": ["keychain:huggingface.com"],

  "permissions": {
    "evidence_ledger": "read-write",
    "filesystem": [
      "read:~/.cache/huggingface",
      "write:~/.cache/huggingface"
    ],
    "network": [
      "huggingface.co",
      "*.huggingface.co"
    ]
  },

  "homepage": "https://example.com/huggingface-hub-panel",
  "repository": "https://github.com/janeauthor/contextdna-panel-huggingface",
  "license": "MIT"
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique per installation. Reverse-DNS or kebab-case (e.g. `huggingface-hub-v2`, `com.acme.robotics`). Used as the install directory name and the namespace prefix for panel-scoped events. |
| `name` | string | yes | Human-readable label rendered in the DockView tab. |
| `version` | string (semver) | yes | The panel's own semver. Breaking schema/event changes require a major bump. |
| `mothership_min_version` | string (semver) | yes | Minimum mothership runtime this panel was tested against. The substrate refuses to mount if the installed mothership is older. |
| `author` | object | yes | `{ name, email?, github? }`. `name` is the only mandatory subfield. |
| `description` | string | yes | One-line elevator pitch, ≤140 chars. Surfaced in `context-dna-ide panel list`. |
| `domains` | string[] | yes | Dotted domain tags (e.g. `ml.training`, `mobile.ios`, `marketing.email`). Drives domain-aware injection: when a prompt's classified domain overlaps a panel's `domains`, that panel's recent evidence is preferred in S2/S4 assembly. **Never use `*` or `all`** (see §9). |
| `language_affinity` | string[] | no | Programming languages this panel cares about. Used as a tiebreaker when multiple panels match the same domain. |
| `evidence_emits` | string[] | yes | All event types the panel may emit. The substrate rejects any event whose type is not in this list — every emit is declared. |
| `evidence_subscribes` | string[] | yes | All event types the panel wants delivered. The subconscious will route matching events to the panel's `events` channel. |
| `mount` | object | yes | See §4. Tells the substrate how to instantiate the UI. |
| `requires_env` | object | no | Map of env var name → `{ required: bool, description: string, secret: bool }`. The shell prompts the user for any missing required vars before install completes. Secrets are written to the keychain, never to disk. |
| `requires_mcp_servers` | string[] | no | MCP server names that must be running for the panel to function. The substrate auto-boots them on mount. |
| `requires_credentials` | string[] | no | Keychain/vault entries the panel needs (form: `keychain:<service>` or `vault:<path>`). The mothership prompts the user if any are missing — the panel never touches the keychain directly. |
| `permissions` | object | yes | Least-privilege declaration. See §5. |
| `homepage` | string (URL) | no | Marketing or docs page. |
| `repository` | string (URL) | no | Source repo. Populated automatically when installed via `panel install <git-url>`. |
| `license` | string (SPDX) | yes | E.g. `MIT`, `Apache-2.0`, `BUSL-1.1`. The shell displays this on install. |

### Validation

The schema is enforced by `apps/ide-shell/src/panel/manifest-validator.ts` (JSON Schema draft 2020-12). The shell runs validation at three checkpoints:

1. **`panel install`** — before the clone is registered in `panels.lock.json`.
2. **Cold boot** — every panel listed in `panels.lock.json` is re-validated on shell start.
3. **Hot reload** — when a panel's manifest changes on disk, the substrate re-validates before re-mounting.

Validation failures are typed (`PanelManifestError`) and bump `panel_manifest_errors_total{panel_id, reason}` — never silently dropped.

---

## 3. Evidence Event Contract

Panels communicate with the mothership through **typed events**, never through direct ledger writes. The subconscious (local LLM) consumes the emit firehose, distills it into Claims, and broadcasts promoted Wisdom back to panels via subscribe channels.

Every event carries a common envelope:

```ts
interface EventEnvelope<Payload> {
  type: string;                  // dotted event type, e.g. "pattern.discovered"
  panel_id: string;              // emitter — auto-stamped by substrate
  emitted_at: string;            // ISO 8601 UTC
  trace_id: string;              // correlation across panels + subconscious + speaker
  payload: Payload;              // type-specific (see below)
}
```

`panel_id` and `trace_id` are **auto-stamped by the substrate** — panel code MUST NOT set them. Attempts to override are rejected with `PanelProvenanceError`.

### Core event types

#### `pattern.discovered`

The panel has observed a repeating signal worth promoting into the ledger.

```ts
{
  type: "pattern.discovered",
  payload: {
    claim_text: string;            // human-readable claim, ≤280 chars
    evidence_refs: string[];       // pointers — file paths, log line ranges, URLs, prior event IDs
    confidence: number;            // 0.0–1.0; the panel's self-assessed confidence
    domain_hint?: string;          // optional dotted domain tag if narrower than the panel's declared domains
  }
}
```

#### `landmine.confirmed`

A known trap, footgun, or anti-pattern is verified present in the current workspace.

```ts
{
  type: "landmine.confirmed",
  payload: {
    landmine_text: string;         // what the landmine is
    file_path?: string;            // workspace-relative path if file-bound
    severity: "low" | "medium" | "high" | "critical";
    evidence_refs: string[];       // supporting pointers
  }
}
```

`severity: "critical"` events bypass the normal distillation backlog and flow directly into the S0 safety section of the next payload.

#### `outcome.observed`

An action completed; the panel records what happened. This is the feedback loop that closes the sacred capture → index → promote → inject loop.

```ts
{
  type: "outcome.observed",
  payload: {
    action_taken: string;          // what was attempted, ≤280 chars
    result: "success" | "failure" | "partial";
    details: string;               // ≤2000 chars; failure mode, error message, partial output
    related_claim_id?: string;     // if the action was the test of a specific promoted claim
  }
}
```

#### `wisdom.requested`

Broadcast **from subconscious to panels**: "what do you know about `<topic>`?" Panels with matching `domains` receive this and may reply with `wisdom.responded`. This is how the subconscious pulls fresh, panel-specific knowledge into a payload at prompt time without polling.

```ts
{
  type: "wisdom.requested",
  payload: {
    request_id: string;            // correlate the responses
    topic: string;                 // e.g. "huggingface inference latency"
    domain_filter?: string[];      // narrow to specific dotted domains
    deadline_ms: number;           // panels that miss the deadline are silently skipped (with a counter)
  }
}
```

#### `wisdom.responded`

Panel reply to a `wisdom.requested`.

```ts
{
  type: "wisdom.responded",
  payload: {
    request_id: string;            // matches the request
    claims: Array<{
      claim_text: string;
      confidence: number;          // 0.0–1.0
      evidence_refs: string[];
    }>;
    notes?: string;                // optional freeform context
  }
}
```

### Panel-scoped event types

Panels MAY emit additional event types prefixed with `panel.<id>.<event>`, e.g. `panel.huggingface-hub-v2.model_pulled`. These must still be declared in `evidence_emits`. Panel-scoped events are not promoted to the ledger but are visible to the subconscious for cross-panel correlation.

### Schema validation

All event payloads are validated against typed schemas at the substrate boundary. Invalid events bump `panel_event_schema_errors_total{panel_id, event_type}` and are dropped — they never reach the ledger.

---

## 4. Mount Types

The `mount` field tells the substrate how to instantiate the panel UI.

### `react` (preferred)

The panel ships a compiled React bundle. The substrate dynamically imports it, hands it a `PanelHost`, and renders it into a DockView slot.

```json
"mount": {
  "type": "react",
  "entry": "dist/panel.js",
  "props": { "defaultTab": "models", "telemetry": false }
}
```

`entry` is repo-root-relative. The exported default must implement the `MothershipPanel` interface (see [MOTHERSHIP-ARCHITECTURE.md §5.1](../../draft/MOTHERSHIP-ARCHITECTURE.md)). `props` is passed verbatim — the substrate does not interpret it.

### `iframe`

The panel ships an HTML entry point that the substrate loads inside a sandboxed iframe. Used for legacy or non-React panels.

```json
"mount": {
  "type": "iframe",
  "entry": "ui/index.html",
  "props": { "sandbox": "allow-scripts allow-same-origin" }
}
```

The iframe communicates with the substrate via `postMessage` using the same `EventEnvelope` schema as React panels. The sandbox attribute defaults to `allow-scripts` only.

### `external-url`

The panel renders a remote URL inside an iframe. Used for hosted dashboards (e.g. Vercel, Grafana, third-party brains).

```json
"mount": {
  "type": "external-url",
  "entry": "https://app.example.com/embed?theme=dark",
  "props": { "auth": "bearer", "tokenEnv": "EXAMPLE_TOKEN" }
}
```

`external-url` panels can still emit and subscribe via a thin embed bridge (`mothership-embed.js`) the substrate injects. They have stricter network permissions by default — only the URL's origin is reachable unless explicitly declared in `permissions.network`.

---

## 5. Permissions Model

Panels are sandboxed by least-privilege defaults. Every read, write, and outbound network call must trace back to a declared permission. Undeclared reach raises a runtime `PanelPermissionError` and is counter-tracked.

```json
"permissions": {
  "evidence_ledger": "read-write",
  "filesystem": [
    "read:~/.cache/huggingface",
    "write:~/.cache/huggingface",
    "read:./workspace"
  ],
  "network": [
    "huggingface.co",
    "*.huggingface.co"
  ]
}
```

### `evidence_ledger`

- `read` — panel may subscribe to events and query the ledger via `PanelHost.ledger.query()`.
- `read-write` — panel may also emit events that result in `RawEvent` rows.

**Important:** even `read-write` panels do not write to the ledger directly. They emit events; the substrate's contributor pipeline validates provenance, deduplicates, and persists. There is no API for raw row insertion.

### `filesystem`

Array of `<mode>:<path>` entries. Modes: `read`, `write`, `read-write`. Paths may use `~` (user home) or workspace-relative (`./…`). Globs are not supported — declare directories, not patterns. The substrate enforces via OS-level sandboxing (App Sandbox on macOS, AppArmor profile on Linux, where available; userland validator otherwise).

### `network`

Array of hostnames. Bare hostnames are exact-match; `*.example.com` is a single-level wildcard (does not match `example.com` itself). IPs are allowed. **No `*` and no full-URL entries** — declare hosts, not protocols or paths.

### Defaults (when `permissions` is omitted)

- `evidence_ledger: "read"`
- `filesystem: []` — no filesystem access
- `network: []` — no outbound network

A panel with default permissions can still subscribe to events and render UI, but cannot pull artifacts, write caches, or call external services. This is the safe baseline.

### Evidence-ledger writes pass through validation

Even with `evidence_ledger: "read-write"`, every emit is validated:

1. Event type must be in `evidence_emits`.
2. Payload must match the typed schema for that event type.
3. `panel_id` and `trace_id` are auto-stamped (panel cannot override).
4. Rate limits apply per `(panel_id, event_type)` — overflows are dropped and counted.

---

## 6. Versioning + Compatibility

### Semver discipline

- **Major** — breaking changes to emitted event payload schemas, removed event types, dropped `domains`, or incompatible `mount` changes.
- **Minor** — new event types in `evidence_emits`, new optional manifest fields, additive UI features.
- **Patch** — bug fixes, internal refactors, doc updates.

### `mothership_min_version`

Required. The substrate refuses to mount panels whose `mothership_min_version` exceeds the running mothership's version. The error is typed (`PanelVersionIncompatible`) and surfaces in the shell with a one-line fix ("upgrade mothership to ≥X.Y.Z").

### Deprecation policy

When the mothership deprecates an event type or field:

1. The change is announced in the mothership's `CHANGELOG.md` at least **one minor version** before removal.
2. The substrate emits a `panel.deprecation_warning` event to any panel still emitting/subscribing to the deprecated type.
3. Removal happens only on a mothership major-version boundary.

Panels that go silent (no commits, no manifest updates) for >12 months are flagged as `unmaintained` in `context-dna-ide panel list` but continue to function until the next breaking mothership major.

---

## 7. Distribution + Install

Three installation modes, all converging on the same on-disk layout (`apps/ide-shell/panels/<id>/`):

### Mode 1 — Built-in

Panels in the mothership monorepo at clone time. Live in `apps/ide-shell/panels/<id>/` and are registered in `panels.lock.json` on first boot. Examples: `surgeon-theater`, `evidence-ledger`, `truth-ladder`.

No install command needed — they boot with the shell.

### Mode 2 — Git URL

```bash
context-dna-ide panel install https://github.com/janeauthor/contextdna-panel-huggingface
```

The shell:

1. Clones the repo into `apps/ide-shell/panels/<id>/` (the `<id>` comes from the manifest).
2. Validates `panel.manifest.json`.
3. Prompts the user for any missing `requires_env` values, writing secrets to the keychain.
4. Boots `requires_mcp_servers`.
5. Verifies `requires_credentials` (prompts user if missing — never silently fails).
6. Registers the panel in `panels.lock.json` with the resolved commit hash.
7. Hot-mounts in DockView without a shell restart.

Uninstall is `context-dna-ide panel remove <id>` — symmetric, idempotent, and counter-tracked.

### Mode 3 — npm package (future)

```bash
context-dna-ide panel install @org/panel-name
```

Same flow as git URL, but the source is an npm package containing the manifest plus compiled bundle. Reserved for the post-1.0 panel marketplace.

### `panels.lock.json`

Pinned manifest of installed panels. Equivalent of `package-lock.json`. Committed to the user's mothership repo to make panel sets reproducible across machines.

---

## 8. Example — Minimal "Hello, Ledger" Panel

A reference implementation that emits one event and subscribes to one. Drop this into `apps/ide-shell/panels/hello-ledger/panel.manifest.json`:

```json
{
  "id": "hello-ledger",
  "name": "Hello, Ledger",
  "version": "0.1.0",
  "mothership_min_version": "0.5.0",

  "author": {
    "name": "Example Author",
    "github": "exampleauthor"
  },

  "description": "Minimal reference panel — emits one greeting event, subscribes to wisdom requests.",

  "domains": ["examples.hello-world"],
  "language_affinity": ["typescript"],

  "evidence_emits": [
    "pattern.discovered"
  ],
  "evidence_subscribes": [
    "wisdom.requested"
  ],

  "mount": {
    "type": "react",
    "entry": "dist/panel.js"
  },

  "permissions": {
    "evidence_ledger": "read-write",
    "filesystem": [],
    "network": []
  },

  "license": "MIT"
}
```

Pair it with a 20-line React panel that emits a `pattern.discovered` event on mount and logs every `wisdom.requested` it receives. The shell will hot-mount it, validate the manifest, route the event, and update `/health` counters — no other plumbing required.

---

## 9. Anti-Patterns

What NOT to do. The substrate will reject these at install or runtime, but the responsibility starts with the author.

| Anti-pattern | Why it's wrong | How the substrate catches it |
|---|---|---|
| **Writing secrets to the evidence ledger** | Ledger rows are queryable by all panels (per permissions) and may end up in S7 payload assembly. A leaked API key becomes a model-visible secret. | Pre-write redaction scanner; rejected events bump `panel_secret_redaction_blocks_total`. Authors must use `requires_credentials` (keychain) for secrets. |
| **Claiming `*` or `all` in `domains`** | Defeats domain-aware injection — every panel claims to be relevant, so the subconscious can't pick. | Manifest validator rejects `*` and `all`. Use specific dotted domains. |
| **Emitting events without declaring them in `evidence_emits`** | Undermines the predictability the substrate guarantees to other panels and to the subconscious. | Events whose `type` is missing from `evidence_emits` are dropped and counted as `panel_event_undeclared_total`. |
| **Setting `panel_id` or `trace_id` in payload** | Defeats provenance. A panel could impersonate another panel or break correlation. | Substrate strips these fields on emit and bumps `panel_provenance_violations_total`. Repeat offenders are auto-quarantined. |
| **Direct ledger writes via SQL or HTTP** | Bypasses validation, rate limits, redaction, and dedup. Silent corruption guaranteed. | The ledger has no public write API. Attempts via raw Postgres/HTTP are blocked by sandboxing. |
| **Touching the keychain directly** | Same secret-leak risk as ledger writes, plus OS-level prompts that confuse the user. | Filesystem sandbox blocks keychain paths. Use `requires_credentials` and read via `PanelHost.credentials.get()`. |
| **Subscribing to events you don't handle** | Wastes substrate routing capacity and the panel's own event loop. | Subscriptions with no handler for >5 min raise `panel_subscribe_unused_warning`. |
| **Skipping `mothership_min_version`** | Panel mounts on incompatible runtimes, crashes mid-session, evidence ledger gets partial writes. | Manifest validator rejects missing field. There is no implicit default. |
| **Wildcards in `permissions.network`** (e.g. `*`) | Defeats the sandbox. Equivalent to "I'm a panel that calls anything." | Validator rejects `*`. `*.example.com` is permitted (single-level wildcard); bare `*` is not. |
| **Emitting `outcome.observed` for the wrong action** | Poisons the promotion logic — claims promoted on bad evidence. | The substrate cannot detect this directly. Authors are responsible; the 3-Surgeons review layer catches systemic poisoning over time and quarantines panels with anomalous outcome distributions. |

---

## See also

- [MOTHERSHIP-ARCHITECTURE.md §5 — The Panel Substrate](../../draft/MOTHERSHIP-ARCHITECTURE.md)
- [contextdna-ide-mothership-plan.md](../../../docs/plans/2026-05-13-contextdna-ide-mothership-plan.md) — overall mothership plan
- `apps/ide-shell/src/panel/contract.ts` — the runtime `MothershipPanel` interface
- `apps/ide-shell/src/panel/manifest-validator.ts` — the JSON Schema enforcer
