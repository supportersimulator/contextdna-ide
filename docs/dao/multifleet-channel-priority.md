# MultiFleet Channel-Priority Invariant — PROPOSAL (not yet locked)

**Authority**: HIGHEST when locked (dao — invariant)
**Status**: PROPOSAL pending Aaron approval + 3-Surgeons challenge
**Drafted**: 2026-05-07 by Atlas, in response to Aaron: "build into multifleet plugin the invariance of utilizing optimal channels in sequential priority to ensure auto healing/upgrading"

## Problem this invariant solves

Atlas (and any fleet caller) currently has **discretionary** access to all 7 channels but **defaults to whichever channel is most familiar** — usually P7 git push because it's reliable + visible. Result observed in this session:

- P2 HTTP-direct via LAN **was available the whole time** (mac1@192.168.1.144:8855, mac3@192.168.1.191:8855 — both 200 OK)
- Atlas did 100% of cross-node coordination via P7 (60-120s round-trip)
- P2 LAN would have given sub-100ms round-trip
- Multi-minute lag accumulated unnecessarily across ~15 messages

The 7-priority chain is documented in CLAUDE.md but **not enforced**. Documentation ≠ invariant. Plugins enforce; docs decay.

## The locked rule (when adopted)

**MFINV-C01: channel-priority-traversal**

> Every cross-node message MUST traverse the 7-priority channel chain in order until success or exhaustion. No caller may bypass the dispatcher. Channels skipped only on probe-confirmed unavailability, not on caller convenience.

```
gate(send) = dispatcher_only(caller) ∧ priority_order(attempts) ∧ outbox_on_exhaustion(failure)
```

If a caller invokes a transport directly (e.g. `nc.publish(...)`, `urlopen(...:8855/message)`, `scp ...`, `git push fleet-msg`) outside the dispatcher, the action is gated.

## What this rule explicitly forbids

- ❌ **Calling `nats.publish()` / `urlopen('.../message')` / `scp` / `wakeonlan` / `git push fleet-message-dir` from anywhere except `multifleet.channel_priority.send()`**. Per-channel handlers live inside the dispatcher; nothing else may import them.
- ❌ **Skipping channels because "P1 is what I always use"** or "P7 always works". The dispatcher decides; the caller declares only `(payload, target, urgency)`.
- ❌ **Catching channel-failure exceptions to silently fall back to a downstream channel** in caller code. Fallback is the dispatcher's responsibility, fail-closed at the dispatcher boundary only.
- ❌ **Hard-coding any peer URL or hostname** in caller code. The dispatcher resolves targets via `fleet_nerve_config.load_peers()`.

## What this rule explicitly permits

- ✅ The dispatcher choosing to **reorder** priority based on `channel_reliability` history (auto-heal: if P1 fails 3× in 60s, demote P1 below P2 for the next 5 min, re-probe periodically).
- ✅ The dispatcher **skipping** a channel based on a recent probe (`mac1.tailscale_ip == "" → skip P2-tailscale; LAN IP set → try P2-LAN`).
- ✅ Callers declaring `urgency` (sub-second / sub-minute / eventual) — dispatcher uses urgency to choose `max_attempts` per channel and timeouts.
- ✅ `governance_kill_switch` bypassing the dispatcher in emergency rollback (logged with WARNING + counter).

## Implementation contract (for the plugin)

### Public API (only this is exported)

```python
from multifleet.channel_priority import send, ChannelResult, Urgency

result: ChannelResult = send(
    target="mac3",          # node_id from .multifleet/config.json
    payload={"subject": "...", "body": "..."},
    urgency=Urgency.SUB_SECOND,   # SUB_SECOND | SUB_MINUTE | EVENTUAL
    expects_reply=False,
)

# result.delivered:    bool
# result.channel_used: P1_nats | P2_http_lan | P2_http_tailscale | P3_chief_relay |
#                     P4_seed | P5_ssh | P6_wol | P7_git
# result.attempts:     list[(channel, ok_bool, latency_ms, error_or_None)]
# result.exhausted:    bool — all channels failed; payload written to outbox/
```

### Channel ordering (default — dispatcher may reorder per heuristics)

| Order | Channel | Pre-conditions | Latency target |
|-------|---------|---------------|----------------|
| 1 | P1 NATS pub/sub | `nats_url` reachable, peer subscription_count > 0 | <100ms |
| 2 | P2 HTTP-direct (LAN) | `lan_ip` reachable on local subnet | <100ms |
| 3 | P2 HTTP-direct (Tailscale) | `tailscale_ip` set + reachable | <500ms |
| 4 | P3 chief relay | chief node reachable via P1 or P2 | <1s (chief forwards) |
| 5 | P4 seed file (SSH) | `ssh_host` set + key auth working | <5s |
| 6 | P5 SSH direct | as P4 | <5s |
| 7 | P6 Wake-on-LAN | `mac_address` known + LAN broadcast available | <60s (boot wait) |
| 8 | P7 git push to `.fleet-messages/<target>/` | always | <120s (peer poll) |

Dispatcher tries in order; first SUCCESS short-circuits. Each attempt logs `(channel, ok, latency, err)` to channel_reliability ledger.

### Auto-heal (the part that justifies an invariant)

- After every send, write `channel_reliability[target][channel] = (ok_count, fail_count, ema_latency_ms)` to NATS KV `fleet_channel_health` (TTL 1h).
- Re-probe each channel every `60s × 2^consecutive_failures` (capped at 1h) — exponential backoff on broken channels, fast retry on transients.
- `fleet_upgrader` reads `fleet_channel_health` to surface degraded channels in a HUD: "mac3 P1=ok mac3 P2-LAN=ok mac3 P2-tailscale=DEGRADED 12 fails / 60s".
- Healing action surfaced as a **specific suggestion** ("restart Tailscale on mac3") not just a counter — uses the per-channel pre-condition table above.

### Outbox-on-exhaustion (fail-closed)

If all 8 channels fail for a single payload, write JSON to `multi-fleet/.outbox/<target>/<TS>-<urgency>.json` and increment `fleet_outbox_pending` counter (xbar surfaces). On next probe-success of any channel, `dispatcher.flush_outbox(target)` drains in order.

### Caller-bypass detection (the static enforcement)

Pre-commit lint script `scripts/lint-channel-bypass.sh`:

```bash
# Forbidden patterns outside multifleet.channel_priority and tests/:
forbidden=(
    'nc\.publish.*subject.*=.*"fleet'        # NATS direct
    'urlopen.*:8855/message'                  # HTTP direct
    'scp\s+.*\.fleet-messages'                # seed-file direct
    'wakeonlan'                                # WoL direct
    'git push.*\.fleet-messages'              # P7 direct
)
# grep across tracked files; exit 1 if found outside allowlist
```

Called by `.git/hooks/pre-commit` (canonical, symlinked via `sync-node-config.sh`).

## Why a plugin invariant (not a doc, not a guideline)

Aaron's framing: "plugins enforce a degree of invariance which is tremendously valuable and time saving."

Three levels of enforcement strength:

1. **Doc (CLAUDE.md)** — Atlas reads, sometimes ignores. Most session work was P7-only DESPITE the 7-channel doc. **Insufficient.**
2. **Code convention (named function `send_via_best`)** — Atlas finds it sometimes, calls it sometimes. Direct urlopen/nc.publish remains tempting and not blocked. **Better but leaks.**
3. **Plugin invariant (MFINV-C01) + lint hook + surgeon-review interlock** — caller-bypass is mechanically prevented at commit time. The dispatcher is the ONLY surface area. Failures get observability. **Tight.**

Time savings come from:
- No Atlas decision overhead per-message ("which channel?" → just `send(target, payload, urgency)`)
- No repeated learning ("oh right, P2 LAN works") — dispatcher remembers via `fleet_channel_health` KV
- Auto-heal surfaces channel degradation BEFORE next P7-only session
- `fleet_upgrader` can use the same dispatcher for upgrade-rollouts (consistent transport for both messages + binary distribution)

## Implementation status

- ⏳ Design doc (this file) — drafted, awaiting Aaron approval + 3s challenge
- ⏳ `multi-fleet/multifleet/channel_priority.py` — module skeleton (next commit if approved)
- ⏳ `MFINV-C01` registered in `multi-fleet/multifleet/invariants.py` (mirroring `memory/invariants.py` 21-CTXDNA pattern)
- ⏳ `scripts/lint-channel-bypass.sh` + canonical pre-commit hook update
- ⏳ NATS KV `fleet_channel_health` bucket creation + 1h TTL
- ⏳ `fleet_upgrader.surface_channel_health()` HUD wire-up

## Dependencies / cross-refs

- `tools/fleet_nerve_config.py` — already has `load_peers()` returning lan_ip/tailscale_ip/mac_address; dispatcher consumes this
- `memory/invariants.py` — pattern to mirror for MFINV-C* registry
- `memory/governance_kill_switch.py` — emergency bypass path
- `docs/dao/risklane-orthogonality.md` — channel-bypass = Lane-3 (fleet-affecting); dispatcher = Lane-2 (local + reversible)
- This dao lock would supersede any informal "use P1 if you can" advice elsewhere

## Override

Same procedure as `risklane-orthogonality.md`:

1. Explicit Aaron decision (chat-message or commit-signed)
2. New superseding dao entry with provenance trail
3. Update `rejected-considerations-archive.md` if proposal rejected

## Open questions before locking

1. **Outbox retention** — how long do we keep outbox messages before TTL? Suggest 24h.
2. **Urgency labels** — is SUB_SECOND / SUB_MINUTE / EVENTUAL the right discretization, or do we want explicit `max_latency_ms`?
3. **Retry budget per channel** — 1 attempt then move on, or 3 attempts with 200ms backoff per channel?
4. **Channel demotion threshold** — 3 fails / 60s suggested. Tune?
5. **Lint hook scope** — repo-wide? Or just paths importing `multifleet.*`?
6. **Surgeon-review interlock for sends** — currently blocks P1 CLI sends ("interlock_surgeon_review"). Keep, or scope down to only Lane-3+ payloads?

Atlas recommends: SUB_SECOND/SUB_MINUTE/EVENTUAL labels (#2), 1 attempt per channel for SUB_SECOND, 3 for EVENTUAL (#3), 3 fails / 60s demotion (#4), repo-wide lint with explicit allowlist (#5), narrow surgeon interlock to Lane-3+ (#6).

— Atlas, drafted 2026-05-07
