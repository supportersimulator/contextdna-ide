# RiskLane Orthogonality — LOCKED

**Authority**: HIGHEST (dao — invariant)
**Status**: LOCKED. Locks the relationship between two governance axes that the MavKa diff doc conflated.

## Provenance

```
inbox: ~/Downloads/OpenPath_Context-DNA-MavKa.md (L2589-2627, RiskLane Literal[0,1,2,3,4])
  → reflect: E-batch gap analysis 2026-05-08 (E3 verdict: orthogonal-only OR REJECT-PER-DAO)
  → cross-node 3s: mac2 +1.00 + mac1 chief +1.00 (highest convergence)
  → dao (this doc)
```

## The two axes

ContextDNA governance has two **orthogonal** dimensions:

### Agent axis — `PermissionTier` (already shipped)

Source: `memory/permission_governor.py`, INV-001 enforcer.

Answers: **WHO is acting?**

Tiers (4): `restricted` | `advisory` | `trusted` | `lead`.

Derived from outcome-evidenced behavior over a decay window. Agent-trust is earned, not assigned.

### Action axis — `RiskLane` (new from MavKa diff)

Source: `memory/mission_envelope.py` (`RiskLane = Literal[0, 1, 2, 3, 4]`).

Answers: **WHAT is being done?**

Lanes (5):

- `0` — read-only / observable / pure (e.g. `/status`, `/health`, log tail)
- `1` — local reversible (e.g. write to scratch file, run idempotent script)
- `2` — local non-trivial (e.g. commit to local branch, rebuild venv)
- `3` — fleet-affecting (e.g. push to origin, restart peer daemon)
- `4` — destructive / irreversible (e.g. `rm -rf`, `git push --force`, `DROP TABLE`)

Lanes are properties of the **action class + blast_radius**, not the agent.

## The locked rule

**Both axes apply to every action; neither replaces the other.**

```
gate(action, agent) = PermissionTier(agent) ∧ RiskLane(action) ∧ MissionEnvelope(session) ∧ Invariants(action)
```

If a proposed action fails ANY axis check, the action is gated. Allow-decisions require ALL axes to pass.

### What this rule explicitly forbids

- ❌ Replacing `PermissionTier` with `RiskLane` (R16 in `rejected-considerations-archive.md`). The MavKa diff doc's `route_action(action, envelope, context)` short-circuits to lane-only, which would silently drop the agent-trust dimension. **REJECT-PER-DAO.**
- ❌ Using high `PermissionTier` to override a high `RiskLane` block. A `lead` tier agent CANNOT do a Lane-4 action without satisfying Lane-4's evidence/rollback gates — earned trust does NOT mean carte blanche on destructive ops.
- ❌ Using low `RiskLane` to override low `PermissionTier`. A `restricted` agent doing a Lane-0 read-only action still goes through the agent gate; restricted is `restricted`.

### What this rule explicitly permits

- ✅ Both axes evaluating in parallel; both report independent reasons; gate decision is the AND.
- ✅ `MissionEnvelope` (third axis, session-scoped) gating BEFORE both per-action axes — caller chooses to delegate ahead-of-time but cannot override the per-action axes.
- ✅ `governance_kill_switch` (T3) bypassing ALL axes when activated — emergency override is its own dao concept; logged via WARNING + counter on activate/deactivate.

## Why this lock exists

E3's deep read of the MavKa diff doc found that `route_action(action, envelope, context)`:

```python
def route_action(action, envelope, context):
    assessment = assess_risk(action, envelope, context)
    if assessment.lane <= 1:
        return "execute"               # ← lane only. agent-trust not consulted.
    if assessment.lane == 2:
        if inside_envelope(action, envelope):
            return "execute_and_log"    # ← envelope override. agent-trust not consulted.
        return "chief_review"
    ...
```

If shipped as-is, this would replace `permission_governor.py`'s tier check with a single-axis lane check. Cross-node 3s convergence (mac2 +1.00, mac1 chief +1.00) confirmed the orthogonality interpretation is the only acceptable shipping shape.

## Implementation status

- ✅ `PermissionTier` shipped (`memory/permission_governor.py`, R3 commit acf18535)
- ✅ `RiskLane` literal shipped (`memory/mission_envelope.py`, this batch)
- ✅ `MissionEnvelope.check_action` evaluates both lane and scope BEFORE caller invokes per-action invariants
- ✅ `OpenPathScore` (`memory/permission_governor.py`) augments InfluenceScore with auditable action-property components

Future callers wiring the gates together:

```python
# Pseudocode — wire-up belongs in a thin dispatcher (NOT in this dao doc).
env_report = mission_envelope.check_action(envelope, action_class=..., risk_lane=...)
if env_report.decision != "allow": return env_report

inv_report = invariants.evaluate(ActionProposal(...))
if inv_report.decision == "block": return inv_report

perms = permission_governor.compute_permissions(agent_id)
if perms.tier == "restricted" and risk_lane >= 3: return "block (tier vs lane)"

# All gates pass → allow
```

The dispatcher itself is **not** part of this dao lock — only the orthogonality rule is. Callers may compose the axes in any order so long as ALL axes are evaluated and AND'd.

## Override

Same procedure as `contextdna-product-boundaries.md`:

1. Explicit Aaron decision (chat-message or commit-signed)
2. New superseding dao entry with provenance trail
3. Update `rejected-considerations-archive.md` R16 reservation conditions

Until all three hold, this lock is invariant.

## Cross-refs

- `memory/mission_envelope.py` — `RiskLane` literal + `check_action`
- `memory/permission_governor.py` — `PermissionTier` + `OpenPathScore`
- `memory/invariants.py` — per-action invariants (CTXDNA-INV-001..021)
- `memory/governance_kill_switch.py` — emergency override
- `docs/dao/rejected-considerations-archive.md` — R4 surgeon-weighted-voting (rejected); R16 will be added when Lane-0 doorway proposal is formalized
- `docs/dao/corrigibility-loop-algorithm.md` — corrigibility discipline that surgeon-weighted-voting violates
