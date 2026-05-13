# ContextDNA IDE — Extensible Profiles

This directory holds the **layered profile schema** that supersedes the rigid
`draft/profiles/heavy.yaml` and `draft/profiles/lite.yaml` presets. Profiles
now form an inheritance chain: a root schema defines the shape and injection
points; presets fill in defaults; operators author their own profile on top.

```
extensible.yaml          (root schema — defaults + injection points)
        ▲
heavy.yaml / lite.yaml   (presets — extends: extensible)
        ▲
my-profile.yaml          (operator profile — extends: heavy | lite | …)
```

## Inheritance model

A profile names its parent via the top-level `extends:` key:

```yaml
# my-profile.yaml
extends: heavy
```

At resolve time the chain is walked **leaf → root**. Once the root is reached
the resolver walks back **root → leaf** applying overrides. Concretely:

1. **Load chain.** Follow `extends:` from the named profile up to the first
   profile with `extends: null` (always `extensible.yaml`). Cycles are
   detected and rejected.
2. **Merge `services:`.** Each named service in a child either *creates* the
   entry (if no ancestor declared it) or *merges* into the ancestor's
   definition. Primitive keys (`image`, `enabled`) replace; map keys (`env`)
   deep-merge; list keys (`ports`, `volumes`) **append**.
3. **Resolve per-service `extends:`.** When a service block itself names an
   `extends:` (e.g., a custom `postgres-replica` that `extends: postgres`),
   the resolver copies the named base service in first, then layers the
   declaring profile's keys on top.
4. **Apply environment `overrides:`** in chain order, root first, leaf last
   (LIFO — leaf wins ties). These are the final word; they beat anything in
   the `services:` block of the same profile.
5. **Stitch `extensions:`.** New services that don't collide with any
   ancestor name get added to the resolved set as-is.
6. **Append `hooks:`** root-first, leaf-last. Lifecycle commands execute in
   that order so operator scripts always see the preset's state.

The resolver enforces three invariants and aborts with a clear message on
violation:

| Invariant | Violation example |
|-----------|-------------------|
| `schema_version` of every profile in the chain is `>= resolver.min_schema` | Stale profile loaded against newer resolver |
| `mothership_min_version` matches the running mothership | Profile authored for v0.2, mothership is v0.1 |
| `extensions:` names do not collide with any inherited service | Operator named their sidecar `redis` |

## Authoring a custom profile

1. Pick a parent. Start from `heavy` for server-class boxes, `lite` for
   laptops. Hardware floor is enforced by `scripts/bootstrap-check.sh` —
   the resolved profile's `resources:` block must be satisfiable on the host.

2. Create `migrate2/profiles/my-profile.yaml`:

   ```yaml
   schema_version: "1.0.0"
   mothership_min_version: ">=0.1.0"
   extends: heavy

   profile: my-mac-studio
   description: "Mac Studio rig with a private LLM sidecar"

   services:
     # Trim/tweak inherited services. Anything you don't mention is kept.
     grafana:
       enabled: false           # operator doesn't want Grafana on this box
     postgres:
       env:
         POSTGRES_MAX_CONNECTIONS: "400"

   overrides:
     - service: redis
       patch:
         image: redis:7.4-alpine    # private mirror with bugfix backport

   extensions:
     mlx-sidecar:
       enabled: true
       image: ghcr.io/my-org/mlx-sidecar:0.4.2
       env:
         MLX_MODEL: qwen3-4b-4bit
       ports:
         - {host: 5099, container: 8080, bind: 127.0.0.1, description: "MLX sidecar"}

   hooks:
     pre_up:
       - "scripts/check-metal-available.sh"
   ```

3. Resolve to a Compose file:

   ```bash
   python migrate2/scripts/profile-resolve.py my-profile > docker-compose.merged.yml
   docker compose -f docker-compose.merged.yml up -d
   ```

The resolver emits a single docker-compose-format YAML document. Diffing
that against the upstream `docker-compose.heavy.yml` is a useful sanity
check before bringing the stack up.

## Migration from `heavy.yaml` / `lite.yaml`

The two existing presets become *first-class profiles* under
`migrate2/profiles/`:

- `heavy.yaml` (to be added): `extends: extensible`, copies every service
  definition from `draft/profiles/heavy.yaml` into `services:`, sets the
  heavy `resources:` floor, and copies the heavy `sync:` block verbatim.
- `lite.yaml` (to be added): same treatment for the laptop preset.

Existing operators have two migration paths:

1. **Drop-in.** Run `python migrate2/scripts/profile-resolve.py heavy`. The
   output is byte-equivalent (up to YAML key ordering) to today's
   `docker-compose.heavy.yml`. No operator action needed.
2. **Lift their tweaks.** Operators who today hand-edit `docker-compose.*.yml`
   move their patches into a new `my-profile.yaml extends: heavy` and stop
   touching the upstream Compose file. Future preset updates flow through
   without merge conflicts.

`draft/profiles/heavy.yaml` and `draft/profiles/lite.yaml` remain in the repo
for now as *informational references* — CI still validates them as schema
documents, but the runtime path consumes `migrate2/profiles/` exclusively.

## Versioning

`schema_version` is plain semver applied to the **profile schema**, not the
content. Bumping rules:

| Bump | Meaning | Operator action |
|------|---------|-----------------|
| `1.0` → `1.1` | Additive — new optional keys or sections; existing profiles still resolve unchanged. | None. Adopt new keys when convenient. |
| `1.1` → `1.2` | Same shape as additive bump. | None. |
| `1.x` → `2.0` | Breaking — renamed keys, removed sections, changed merge semantics. | Run `migrate2/scripts/profile-migrate.py 1.x→2.0` (delivered alongside the bump). Resolver refuses pre-2.0 profiles after the bump lands. |

`mothership_min_version` uses a semver-range string consumed by `packaging`.
The resolver reads the running mothership's version from
`contextdna-ide-oss/pyproject.toml`. Mismatches abort with a remediation
message pointing to the matching mothership tag.

## Example: a "Mac Studio" profile

`migrate2/profiles/mac-studio.yaml` (illustrative — not shipped):

```yaml
schema_version: "1.0.0"
mothership_min_version: ">=0.1.0"
extends: heavy
profile: mac-studio
description: >-
  Apple Silicon home server. Skips Ollama (MLX native instead),
  bumps Postgres connection limits, adds a private MLX sidecar.

services:
  ollama:
    enabled: false
  ollama-setup:
    enabled: false
  postgres:
    env:
      POSTGRES_MAX_CONNECTIONS: "400"
      POSTGRES_SHARED_BUFFERS: "2GB"

extensions:
  mlx-sidecar:
    enabled: true
    build: services/mlx-sidecar/Dockerfile
    env:
      MLX_MODEL: qwen3-4b-4bit
    ports:
      - {host: 5099, container: 8080, bind: 127.0.0.1, description: "MLX sidecar"}
    volumes:
      - {source: ~/.cache/huggingface, target: /root/.cache/huggingface, type: bind}

hooks:
  pre_up:
    - "scripts/check-metal-available.sh"
  post_up:
    - "curl -sf http://127.0.0.1:5099/health"
```

Resolved Compose output preserves the entire heavy stack minus the two
disabled Ollama services, with the Postgres tweaks merged in and the
mlx-sidecar appended. Operator's repo stays clean; upstream preset updates
flow in unchanged.
