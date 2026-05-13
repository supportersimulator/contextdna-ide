# MultiFleet Coordination Invariants — MFINV-C02..C05

**Authority**: HIGHEST when locked (dao — invariant suite extending MFINV-C01)
**Status**: PROPOSAL pending Aaron approval + 3-Surgeons challenge
**Drafted**: 2026-05-12 by Atlas (GGG1), in response to Aaron: "add to the mf plugin the invariance of the most sophisticated communications and coordinations used in this session"

## Provenance — what this session demonstrated

This session (2026-05-12, FFF2 + EEE1-5 + GGG1) exercised four coordination patterns whose value was empirically validated but not yet codified as invariants. MFINV-C01 covers dispatcher-only access; MFINV-C02..C05 codify the **what to do when dispatcher first-success is not enough** layer above it.

Live evidence captured:
- **Tailscale dup-node-key collision** — mac2 stole mac1's tailnet identity; mac1 displaced. LAN-IP path stayed healthy; cascade fall-through to P5_ssh worked. Single layer-7 fault did not collapse the system.
- **L1 NOTIFY auto-repair** — daemon watchdog detected mac2 silence at 10:56:33, escalated L1 NOTIFY at 10:56:37, mac2 responded at 10:57:09 (36s end-to-end). Then `Channel state change: mac2/P1_nats broken → ok` + `mac2/P2_http broken → ok` at 10:58:59-10:59:00. Fully autonomous.
- **Rate-limited outbound repair** — `Rate limited OUTBOUND repair to mac2 -- skipping send`. Self-throttle prevented repair storm.
- **FFF2 multi-surface broadcast** — 12 surfaces attempted; 5+ confirmed delivered (P1 NATS, P4 seed scp, P5 SSH exec, P10 osascript notify, P11 osascript voice, P12 Desktop file). Operator-action message reached the human regardless of which transport was wedged.

---

## MFINV-C02: diverse-surface broadcast for blocking operator actions

> When a fleet message **requires a human action on the peer** (e.g., `sudo tailscale logout`, manual approval), the broadcast MUST exercise **≥3 distinct surface families** simultaneously — not relying on the dispatcher's first-success.

**Why**: cascade short-circuits on first success. If that success is on a surface the operator does not observe (NATS queue, daemon log), the human never sees the message. The fleet self-heals; the operator does not.

**Surface families** (need ≥3 of):
1. **Transport** — `multifleet.channel_priority.send()` (P1-P9 cascade)
2. **Persistent file** — seed file in `.fleet-messages/<target>/<broadcast_id>.md` AND `<target>:/tmp/fleet-seed-<target>-<broadcast_id>.md`
3. **macOS HUD** — `ssh <target> 'osascript -e "display notification ..."'` (visual + sound)
4. **macOS voice** — `ssh <target> 'osascript -e "say ..."'` (audible TTS)
5. **IDE inbox** — copy of seed file into `<target>:~/dev/.../`.fleet-messages/<target>/` for next Claude Code session pickup
6. **Desktop marker** — `<target>:~/Desktop/<BROADCAST_ID>-<action>.txt` for high-visibility login surface
7. **External channel** — Discord broadcast / Telegram doorway (when EEE2 doorway has authorized chats)

**Required artefact**: `.fleet/audits/<date>-<broadcast_id>-multichannel-broadcast.md` with a per-surface table:

| Pri | Surface | Status | Evidence |
|-----|---------|--------|----------|
| Px  | Name    | ✅/⚠️/❌/⏭️ | one-line quote of stdout / file path / SHA |

**Counter**: `multi_surface_broadcast_total`, `multi_surface_broadcast_surfaces_used` (histogram).

**Enforcement**: doctrinal (review-time, not hook-time). The PreToolUse hook cannot tell a "regular send" from a "needs-multi-surface" send by syntax — caller declares intent via `broadcast_id` in payload + audit artefact presence.

---

## MFINV-C03: autonomous L1→L4 repair escalation

> Daemon watchdog detection of a silent peer MUST escalate `L1 NOTIFY → L2 PROBE → L3 SSH-EXEC → L4 BREACH-EMERGENCY` with each hop rate-limited, ZSF-counted, logged, and auto-deescalated on peer resume.

**Why**: a binary "alive / dead" model loses signal. The four-level escalation lets the operator + fleet see exactly where in the recovery chain a peer is stuck. Validated live this session (10:56:33 → 10:57:09 = 36s end-to-end for L1).

**Escalation table**:

| Level | Action | Rate limit | ZSF counter | Auto-deescalate trigger |
|-------|--------|------------|-------------|-------------------------|
| L1 NOTIFY | NATS message to peer via P1 | ≥30s/peer | `repair_l1_attempts_total` / `repair_l1_success_total` | peer ACK within 5min |
| L2 PROBE | HTTP /health probe via P2 LAN + tailscale | ≥60s/peer | `repair_l2_attempts_total` / `repair_l2_success_total` | /health returns 200 with body |
| L3 SSH-EXEC | SSH-driven daemon restart attempt | ≥300s/peer | `repair_l3_attempts_total` / `repair_l3_success_total` | daemon reports `uptime_s < 60` |
| L4 BREACH-EMERGENCY | Discord + macOS notif + voice to operator | ≥900s/peer | `repair_l4_attempts_total` / `repair_l4_breach_total` | manual override only |

**Required behaviour**:
- Each level increments its counter regardless of outcome.
- Rate-limit denial is itself logged + counted (`repair_l<n>_rate_limited_total`).
- Auto-deescalate emits `fleet.peer.recovered.<peer>` on P1 NATS with the level it healed at.
- L4 BREACH triggers MFINV-C02 multi-surface broadcast automatically (operator must act).

**Live evidence (this session)**:
```
10:56:33 Watchdog: mac2 never seen -- attempting contact
10:56:37 Repair escalation for mac2: broken=['P1_nats', 'P2_http'], action=restart_nats
10:56:37 Repair L1 NOTIFY -> mac2
10:57:09 Repair SUCCESS: mac2 back on P1 after L1 notify
10:58:59 Channel state change: mac2/P1_nats broken -> ok
10:59:00 Channel state change: mac2/P2_http broken -> ok
10:59:02 audit.complete peer=mac2 reason=heartbeat_recovery elapsed_ms=2913 results={'P1_nats':True,'P2_http':True,'P7_git':True}
```

---

## MFINV-C04: transport-layer fault containment

> No single transport-layer outage (Tailscale, NATS broker, LAN switch) may collapse the cascade. Each transport layer MUST have an independent health probe + counter, and the cascade MUST be able to traverse to a non-shared-fate channel.

**Why**: this session, Tailscale dup-node-key broke the tailnet view (mac1 displaced from admin panel) yet LAN-IP transports (P5 SSH at 192.168.1.183, P4 scp, osascript-over-ssh) continued working. A naive observer might conclude "mac2 is gone." MFINV-C04 prevents that mistake: the dispatcher MUST keep a separate health vector per transport-layer, not a single per-channel boolean.

**Required transport-layer probes** (each surfaces its own counter on /health):
- `tailscale_health` (`tailscale status` exit + dup-key flag)
- `lan_health` (ping + arp resolution per peer)
- `nats_cluster_health` (subscription_count > 0 per peer)
- `wifi_layer_health` (`/usr/sbin/networksetup -getairportnetwork en0`)
- `ssh_keyauth_health` (last successful keyauth time per peer)

**Cascade rule**:
- When `tailscale_health == broken` → dispatcher MUST still attempt P2_lan, P5_lan_ssh, P4_lan_scp; only P2_tailscale variant is skipped.
- When `nats_cluster_health == broken` for cluster → dispatcher MUST still attempt P2 HTTP direct (different routing fabric).
- When all WAN paths broken → P5/P6/P12 (LAN-only) MUST remain attempted.

**Counter**: `cascade_layer_fault_contained_total{layer=tailscale|nats|wifi|ssh}` increments each time a layer fault was bypassed by an alternative channel that succeeded.

---

## MFINV-C05: multi-urgency redundant dispatch (for high-stakes)

> For broadcasts where redundancy beats latency, caller MAY invoke `channel_priority.send()` ≥2 times with **escalating urgency tiers** (`sub_second → sub_minute → emergency`) to force exploration of progressively deeper cascade layers.

**Why**: cascade's first-success short-circuits. For a routine status update that's correct. For a high-stakes broadcast where receipt matters more than speed, the caller wants the message to land on **multiple** channels for redundancy. Multi-urgency dispatch achieves this without violating MFINV-C01 (still via dispatcher).

**Allowed pattern** (only for `broadcast_id` payloads):
```python
from multifleet.channel_priority import send, URGENCY_SUB_SECOND, URGENCY_SUB_MINUTE, URGENCY_EMERGENCY

msg = {"broadcast_id": "FFF2", ...}
r1 = send("mac2", msg, urgency=URGENCY_SUB_SECOND)   # P1/P2 fast path
r2 = send("mac2", msg, urgency=URGENCY_SUB_MINUTE)   # forces P3-P5 attempt
r3 = send("mac2", msg, urgency=URGENCY_EMERGENCY, use_cloud=True)  # P0 cloud opt-in
```

**Required**: each call MUST share the same `broadcast_id`. Dispatcher MUST de-dupe at the receiver via `(broadcast_id, target)` → first one wins downstream effects; counter `multi_urgency_dispatch_dedup_total` tracks duplicates suppressed.

**Counter**: `multi_urgency_dispatch_calls_total`, `multi_urgency_dispatch_distinct_channels_used` (histogram per broadcast_id).

**Anti-pattern (forbidden)**: looping `send()` until delivered=True without urgency escalation. That's polling-disguised-as-dispatch and burns channel quota without exploring depth.

---

## MFINV-C06: closed-loop comms with delta-bundle payloads

> For peer-to-peer comms in an ongoing **thread** (multi-message conversation referencing a common payload context), senders SHOULD wrap payloads in a **delta-bundle** — git/video-style content-addressable encoding that ships only the changed keys vs a parent SHA. Receivers verify reconstructed SHA matches sender's claimed SHA. Missing parent → receiver requests resend with `mode=full`. ZSF: every error path counter-incremented.

**Why**: Aaron 2026-05-12: "git technology allows for an awareness of not sending the entire thing over and over it shows what specifically changed... there is a tech that uses video like embeded tech to show what is changed - it doesn't load everything it just shows the diff". For high-frequency thread updates (status pings, polling loops, surgery-team turn-by-turn), full-payload retransmission burns bandwidth + churn. Delta-bundles let the second-N message in a thread ship as `parent_sha + changed_keys` — typically a fraction of full payload size.

**Bundle shapes** (composed by `multifleet.delta_bundle.compose_bundle`):
```
full:   {mode:"full",  sha, payload, thread_id, msg_id, bundle_v:1, bytes_full}
delta:  {mode:"delta", sha, parent_sha, json_patch, thread_id, msg_id, bundle_v:1,
         bytes_full, bytes_delta, saved_pct}
```

**Required behaviour**:
- `compose_bundle(payload, thread_id, ...)` stores full payload by SHA in `.fleet/delta-store/<thread>/<sha>.json` and returns either `{mode:"full"}` or `{mode:"delta"}` based on whether savings ≥ `min_savings_pct` (default 20%).
- `apply_bundle(bundle)` verifies SHA match after reconstruction. SHA mismatch → return None + increment `delta_bundle_apply_invalid_total`.
- Parent missing on receive → return None + increment `delta_bundle_apply_parent_missing_total`. Caller responsibility: request resend with `bundle_thread_id=None` (forces full).
- `channel_priority.send(..., bundle_thread_id=<thread>)` opts the message into the bundle layer transparently. Return dict gains a `"bundle"` field with `{mode, sha, parent_sha, saved_pct, bytes_full, bytes_delta}`.

**Counters** (surfaced under `/health.channel_priority.delta_bundle`):
- `delta_bundle_compose_full_total`, `delta_bundle_compose_delta_total`
- `delta_bundle_apply_full_total`, `delta_bundle_apply_delta_total`
- `delta_bundle_apply_parent_missing_total`, `delta_bundle_apply_invalid_total`
- `delta_bundle_store_write_errors_total`, `delta_bundle_store_read_errors_total`
- `delta_bundle_bytes_saved_total`, `delta_bundle_bytes_saved_max`

**Layered with existing ACK infra** (already live in `tools/fleet_nerve_nats.py`):
- Sender's `_store.update_ack_status(msg_id, "awaiting")` tracks pending receipts
- Receiver publishes `fleet.<sender>.ack` with `msg_id` + (NEW) reconstructed `sha256` for SHA-match proof
- Sender's 30s re-send loop (`get_awaiting_ack` at line 9176) retransmits unACK'd messages — with delta-bundle, retransmission is **just the bundle** (often ≪ full payload)
- Closed loop: send(thread="X") → receiver applies bundle → publishes ack with sha → sender records ack with sha → if responding, sender composes next bundle with previous one as parent_sha → repeat

**Security**:
- `thread_id` is sanitised on disk (`[a-zA-Z0-9_-.]` only) to prevent path traversal from hostile peers.
- SHA-match verification means a tampered patch can't reconstruct to a different payload than sender intended.
- The store is local-only — peers do not share the cache directly; they reconstruct independently.

**Anti-pattern (forbidden)**:
- Sending the same large payload N times in a tight loop without supplying `bundle_thread_id` — the delta layer exists exactly to eliminate this waste.
- Trusting `apply_bundle` return value of None as a normal flow — None means failure; caller must request resend or escalate.

**Live evidence (this session)**: `multi-fleet/multifleet/tests/test_delta_bundle.py` 12/12 green. Test `test_compose_delta_saves_bytes` verifies a one-key mutation of a 3KB payload produces ≥20% savings.

---

## MFINV-C07: metric-driven channel scoring (ideal-fallback invariance)

> The cascade order MUST be **computed from observable per-channel metrics**
> (latency EMA, reliability ratio, recency of last success, broken state)
> combined with a STATIC_PRIORITY_WEIGHT bias — not from author convenience.
> Same payload + same target = same chosen channel IF observed metrics agree.

**Why** — Aaron 2026-05-12: *"if any channels fail there is invariance of ideal
fallbacks starting from ideal down to least ideal taking into account speed,
reliability, etc..."*

Pre-HHH1 the cascade was a hand-rolled list `P0_discord, P1_nats, P2_http,
P2t_tailscale, P3_chief, P4_seed, P5_ssh, P6_wol, P7_git`. The order was
chosen by intuition. EEE3 added broken-channel skipping but kept the
ordering fixed. MFINV-C07 codifies that the same fleet state (metrics +
broken flags) MUST yield the same cascade order, regardless of caller.

**Formula** (`multifleet.channel_scoring.score_channel`):

```
speed_score    = 1000.0 / max(latency_ema_ms, 1.0)        # ms — bigger = faster
reliability    = clamp(s / (s + f + 1), 0.01, 1.0)        # Laplace-smoothed
recency_bonus  = exp(-last_success_age_s / 300.0)         # 5-min decay
broken_penalty = 0.01 if is_known_broken else 1.0
score = speed_score * reliability * recency_bonus * broken_penalty
        * STATIC_PRIORITY_WEIGHT[channel]
```

`STATIC_PRIORITY_WEIGHT`: `P1=10.0, P2=8.0, P2t=7.5, P3=6.0, P4=4.0,
P5=3.0, P6=1.5, P7=1.0, P8_superset=0.5, P9_telegram=0.4`. The 10x
P1→P7 gap is the conservative bias: metrics only override the baseline
when the gap is dramatic and consistent.

**Counters** (surfaced under `/health.channel_scoring`):

- `channel_scoring_calls_total`
- `channel_scoring_metrics_missing_total`
- `channel_scoring_static_fallback_total`
- `channel_scoring_per_channel_chosen_total{channel}`
- `channel_scoring_score_max` (gauge)
- `channel_scoring_score_min` (gauge)
- `channel_scoring_reordered_total` (computed ≠ static-baseline)
- `channel_scoring_errors_total`

**Override / risk safety**: gated behind env flag
`FLEET_CHANNEL_SCORING_ENABLED=1`. **Default OFF** for at least one
fleet cycle of A/B comparison vs the static cascade. Per-invariant
emergency rollback honors `MULTIFLEET_INVARIANT_OVERRIDE=C07` like the
rest of the C02-C06 family.

**MFINV-C01 untouched**: this invariant changes *which* channel is
tried first; it does NOT add a new transport, nor bypass the dispatcher.
All sends still funnel through `CommunicationProtocol.send`. The
first-success short-circuit semantics are preserved.

**Live evidence (this session)**: `multi-fleet/multifleet/tests/test_channel_scoring.py`
21/21 green. `test_speed_can_override_priority_when_gap_is_extreme`
shows P7 (1ms) beating P1 (5000ms) when metrics diverge dramatically;
`test_static_weight_dominates_modest_speed_advantage` shows P1 still
wins when the gap is modest — encoding Aaron's "ideal down to least
ideal" constraint without flipping order on noise.

**Open questions**:
1. Should `STATIC_PRIORITY_WEIGHT` be peer-tunable (e.g. mac2 behind a
   router may prefer P2t_tailscale ahead of P2_http)?
2. Recency decay half-life of 5 min — too tight for low-traffic peers
   that legitimately go quiet for hours?
3. Reliability floor of 0.01 — should an unproven channel (0 success,
   0 fail) rank identically to a heavily-failed one?

---

## Implementation status

- ✅ MFINV-C01 — locked (PROPOSAL → adopted; hook enforces; allowlist active)
- ⏳ MFINV-C02 — proposed; needs C02-broadcast helper in `channel_priority.py` + audit-artefact contract test
- ⏳ MFINV-C03 — partially live (L1 NOTIFY observed); L2-L4 escalation chain + auto-deescalate needs codification
- ⏳ MFINV-C04 — partially live (cascade does survive Tailscale outage); transport-layer health probes are scattered, need consolidation
- ⏳ MFINV-C05 — pattern exists in `URGENCY_*` constants; needs `broadcast_id` de-dupe + counters
- ✅ MFINV-C06 — module shipped (`multifleet/delta_bundle.py` + tests + counter surfacing in `channel_priority.send`); ACK-side SHA-match verification still TODO in `tools/fleet_nerve_nats.py`
- ⏳ MFINV-C07 — module shipped (`multifleet/channel_scoring.py` + 21/21 tests); wired into `CommunicationProtocol.send` behind `FLEET_CHANNEL_SCORING_ENABLED` (default OFF). Promote to default ON after one fleet cycle of A/B vs static order.

---

## Dependencies / cross-refs

- `multi-fleet/multifleet/channel_priority.py` — public API
- `multi-fleet/multifleet-plugin/hooks/pre_tool_use.py` — MFINV-C01 enforcement
- `multi-fleet/multifleet-plugin/SKILL.md` — operator-facing summary
- `.fleet/audits/2026-05-12-FFF2-mac2-multichannel-broadcast.md` — first MFINV-C02 audit artefact
- `tools/fleet_nerve_nats.py` — L1 NOTIFY repair escalation lives here

---

## Override

Any of MFINV-C02..C07 may be overridden in emergency rollback by setting `MULTIFLEET_INVARIANT_OVERRIDE=C02,C03,C04,C05,C06,C07` (comma-separated). Override emits WARNING log + increments `mfinv_override_total{invariant=Cxx}` counter. Persistent overrides (>1h) trigger a `fleet.invariant.override.persistent` event for 3-Surgeons review.

---

## Open questions before locking

1. **C02 surface threshold** — is 3 distinct surface families correct? Or should "blocking operator action" specifically require Transport + Persistent-file + macOS-HUD minimum (a fixed triple, not just any 3)?
2. **C03 L4 trigger criteria** — what defines BREACH-EMERGENCY clearly enough to avoid false-positive operator pages?
3. **C04 transport-layer enumeration** — is the 5-layer model (tailscale/lan/nats/wifi/ssh) complete? Where does P8 Superset / P9 Telegram fit?
4. **C05 de-dupe semantics** — strict (first-only) vs additive (operator may want to see N delivery confirmations)?
