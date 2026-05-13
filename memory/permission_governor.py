"""
Permission Governor — next consumer in Context→Behavior→Outcome→Score→Permission→Memory.

# INV-001 enforcer
Constitutional invariant from N1: "No agent may gain influence except through
outcome-evidenced behavior." This module READS Q3's append-only outcome ledger
(``memory/outcome_ledger.jsonl``) and derives per-agent influence + permission
tier. It NEVER writes to the ledger — read-only consumer.

Pipeline position::

    Context → Behavior → Outcome → Score → [Permission] → Memory
                                            ^^^^^^^^^^^^
                                            this module

Read-only contract:
  - opens LEDGER_PATH with mode="r" only
  - never calls write/truncate/append on the ledger
  - tests assert ledger mtime is unchanged after compute_*

Influence math (stdlib only, no scipy / numpy):
  - per-outcome weight = exp(-age_hours / decay_window_hours)
  - influence = sum(score_i * weight_i) / sum(weight_i)
  - low_data flag if sample_count < LOW_DATA_THRESHOLD
  - low-data agents default to NEUTRAL_SCORE (0.5) -> "advisory" tier

Tier mapping (PERMISSION_RULES):
  - 0.00-0.30  -> "restricted" (more gates, surgeon review for high blast-radius)
  - 0.30-0.60  -> "advisory"   (default; standard gates)
  - 0.60-0.85  -> "trusted"    (some gates relaxed)
  - 0.85-1.00  -> "lead"       (most gates relaxed; can grant influence to peers)

ZERO SILENT FAILURES:
  - module-level counter dict ``COMPUTE_ERRORS`` daemon-scrapable
  - every parse failure logs + bumps counter (never crashes)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import pathlib
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Literal

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_THIS_FILE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parent.parent  # memory/.. -> superrepo

# Ledger path is the same one Q3 writes to. We READ ONLY.
LEDGER_PATH: pathlib.Path = _REPO_ROOT / "memory" / "outcome_ledger.jsonl"

# Tier labels (spec contract)
PermissionTier = Literal["advisory", "trusted", "lead", "restricted"]

# Tier mapping table — exposed for callers that want to inspect rules without
# re-deriving them. Lower bound inclusive, upper bound exclusive (except top).
PERMISSION_RULES: dict[str, dict[str, Any]] = {
    "restricted": {"min": 0.0, "max": 0.30},
    "advisory":   {"min": 0.30, "max": 0.60},
    "trusted":    {"min": 0.60, "max": 0.85},
    "lead":       {"min": 0.85, "max": 1.00 + 1e-9},
}

# Gates relaxed/tightened per tier (initial v0; conservative).
# Names are ContextDNA gate identifiers — daemons that read this map should
# treat unknown names as no-op for forward compatibility.
_TIER_GATES: dict[str, dict[str, list[str]]] = {
    "restricted": {
        "relaxed": [],
        "tightened": [
            "require_3s_consensus",
            "require_surgeon_review_high_blast_radius",
            "block_self_promotion",
            "require_human_approval_memory_promotion",
        ],
    },
    "advisory": {
        "relaxed": [],
        "tightened": [],
    },
    "trusted": {
        "relaxed": [
            "skip_3s_for_minor_commits",
            "skip_cardio_for_doc_only_changes",
        ],
        "tightened": [],
    },
    "lead": {
        "relaxed": [
            "skip_3s_for_minor_commits",
            "skip_cardio_for_doc_only_changes",
            "self_grant_subagent_quota",
            "auto_promote_low_risk_memory",
        ],
        "tightened": [],
    },
}

# Decay + low-data tunables
DEFAULT_DECAY_WINDOW_HOURS = 168          # 7 days half-life-ish (e^-1 at 168h)
DEFAULT_INFLUENCE_WINDOW_HOURS = 168       # only consider outcomes within this window
LOW_DATA_THRESHOLD = 5                      # <5 outcomes -> low_data=True
NEUTRAL_SCORE = 0.5                         # default for low-data / unknown agents

# ZSF: counters daemons can scrape. Names are stable.
COMPUTE_ERRORS: dict[str, int] = {
    "permission_compute_errors": 0,
    "ledger_read_errors": 0,
    "ledger_parse_errors": 0,
    "timestamp_parse_errors": 0,
}

logger = logging.getLogger("memory.permission_governor")
if not logger.handlers:  # avoid duplicate handlers on re-import
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s permission_governor %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


def _bump(counter: str) -> None:
    """ZSF: increment a named error counter + the rollup counter."""
    COMPUTE_ERRORS[counter] = COMPUTE_ERRORS.get(counter, 0) + 1
    if counter != "permission_compute_errors":
        COMPUTE_ERRORS["permission_compute_errors"] = (
            COMPUTE_ERRORS.get("permission_compute_errors", 0) + 1
        )


# ---------------------------------------------------------------------------
# Dataclasses (spec contract)
# ---------------------------------------------------------------------------

@dataclass
class InfluenceScore:
    """Per-agent influence derived from outcome history."""

    agent_id: str
    score: float                           # 0.0 - 1.0 (clamped)
    sample_count: int                      # outcomes considered (post-window-filter)
    decay_window_hours: int                # half-life knob used for this score
    low_data: bool = False                 # True when sample_count < LOW_DATA_THRESHOLD
    last_outcome_at: str | None = None     # ISO8601 of most-recent considered outcome
    window_hours: int = DEFAULT_INFLUENCE_WINDOW_HOURS

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentPermissions:
    """Tier + relaxed/tightened gate names derived from an InfluenceScore."""

    agent_id: str
    tier: PermissionTier
    gates_relaxed: list[str] = field(default_factory=list)
    gates_tightened: list[str] = field(default_factory=list)
    influence: InfluenceScore | None = None
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        # influence is itself a dataclass -> asdict already handled.
        return d


# ---------------------------------------------------------------------------
# Ledger reading (READ-ONLY)
# ---------------------------------------------------------------------------

def _iter_ledger() -> Iterator[dict[str, Any]]:
    """Yield parsed ledger entries (raw dicts). Read-only; never writes."""
    if not LEDGER_PATH.exists():
        return
    try:
        # mode="r" is explicit — we never open the ledger for write here.
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error("ledger line %d malformed: %s", lineno, e)
                    _bump("ledger_parse_errors")
    except OSError as e:
        logger.error("ledger read failure: %s", e)
        _bump("ledger_read_errors")


def _record_agent_id(rec: dict[str, Any]) -> str:
    """Resolve agent_id from an outcome_record.

    Future-proof: if the record has an explicit ``agent_id`` use it; otherwise
    fall back to ``node`` (the actor that produced the outcome). Q3's current
    ledger uses ``node`` as the actor identifier.
    """
    aid = rec.get("agent_id")
    if isinstance(aid, str) and aid:
        return aid
    return str(rec.get("node") or "")


def _parse_iso(ts: str) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt
    except (TypeError, ValueError):
        _bump("timestamp_parse_errors")
        return None


# ---------------------------------------------------------------------------
# Influence computation
# ---------------------------------------------------------------------------

def _collect_for_agent(
    agent_id: str,
    window_hours: int,
    now: _dt.datetime,
) -> list[tuple[float, _dt.datetime]]:
    """Return [(score, timestamp), ...] for outcomes attributable to agent_id
    within the window. Uses ``write_timestamp`` (governance-relevant time)
    falling back to record timestamp.
    """
    cutoff = now - _dt.timedelta(hours=window_hours) if window_hours > 0 else None
    out: list[tuple[float, _dt.datetime]] = []
    for obj in _iter_ledger():
        try:
            rec = obj.get("outcome_record") or {}
            if not isinstance(rec, dict):
                continue
            if _record_agent_id(rec) != agent_id:
                continue
            score_raw = rec.get("score", 0.0)
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                _bump("ledger_parse_errors")
                continue
            # clamp 0..1 defensively
            if score < 0.0:
                score = 0.0
            elif score > 1.0:
                score = 1.0

            ts_str = obj.get("write_timestamp") or rec.get("timestamp") or ""
            ts = _parse_iso(str(ts_str))
            if ts is None:
                continue
            if cutoff is not None and ts < cutoff:
                continue
            out.append((score, ts))
        except Exception as e:  # noqa: BLE001 — ZSF: log + count, never raise
            logger.error("collect_for_agent unexpected error: %s", e)
            _bump("permission_compute_errors")
            continue
    return out


def compute_influence(
    agent_id: str,
    window_hours: int = DEFAULT_INFLUENCE_WINDOW_HOURS,
    decay_window_hours: int = DEFAULT_DECAY_WINDOW_HOURS,
) -> InfluenceScore:
    """Compute exponentially-decayed influence score for an agent.

    Weight per outcome: ``exp(-age_hours / decay_window_hours)``. Older
    outcomes count less. Returns InfluenceScore with low_data=True when
    sample_count < LOW_DATA_THRESHOLD; in that case the score is set to
    NEUTRAL_SCORE (0.5).
    """
    try:
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        samples = _collect_for_agent(agent_id, window_hours, now)
        sample_count = len(samples)

        if sample_count == 0:
            return InfluenceScore(
                agent_id=agent_id,
                score=NEUTRAL_SCORE,
                sample_count=0,
                decay_window_hours=decay_window_hours,
                low_data=True,
                last_outcome_at=None,
                window_hours=window_hours,
            )

        if decay_window_hours <= 0:
            # Defensive: avoid divide-by-zero. Treat all weights equal.
            weighted_sum = sum(s for s, _ in samples)
            weight_total = float(sample_count)
        else:
            weighted_sum = 0.0
            weight_total = 0.0
            for score, ts in samples:
                age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
                w = math.exp(-age_h / decay_window_hours)
                weighted_sum += score * w
                weight_total += w

        if weight_total <= 0:
            avg = NEUTRAL_SCORE
        else:
            avg = weighted_sum / weight_total

        # Clamp [0,1]
        if avg < 0.0:
            avg = 0.0
        elif avg > 1.0:
            avg = 1.0

        last_ts = max(ts for _, ts in samples)
        low_data = sample_count < LOW_DATA_THRESHOLD
        if low_data:
            # Don't trust the computed score; fall back to neutral but keep
            # the real sample_count so callers can see why.
            avg = NEUTRAL_SCORE

        return InfluenceScore(
            agent_id=agent_id,
            score=round(avg, 6),
            sample_count=sample_count,
            decay_window_hours=decay_window_hours,
            low_data=low_data,
            last_outcome_at=last_ts.isoformat(),
            window_hours=window_hours,
        )
    except Exception as e:  # noqa: BLE001 — ZSF
        logger.error("compute_influence(%s) failed: %s", agent_id, e)
        _bump("permission_compute_errors")
        return InfluenceScore(
            agent_id=agent_id,
            score=NEUTRAL_SCORE,
            sample_count=0,
            decay_window_hours=decay_window_hours,
            low_data=True,
            last_outcome_at=None,
            window_hours=window_hours,
        )


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------

def _tier_for_score(score: float) -> PermissionTier:
    """Map a [0,1] score to a tier per PERMISSION_RULES."""
    # explicit ladder — keeps Literal type-checker happy
    if score >= PERMISSION_RULES["lead"]["min"]:
        return "lead"
    if score >= PERMISSION_RULES["trusted"]["min"]:
        return "trusted"
    if score >= PERMISSION_RULES["advisory"]["min"]:
        return "advisory"
    return "restricted"


def compute_permissions(agent_id: str) -> AgentPermissions:
    """Derive AgentPermissions for an agent from current outcome history.

    INV-001 enforcer: tier is derived purely from outcome-evidenced score.
    No agent gains influence except through evidenced behavior.
    """
    try:
        infl = compute_influence(agent_id)

        if infl.low_data:
            # Low-data regime per spec: default to advisory + flag low_data.
            tier: PermissionTier = "advisory"
            reason = (
                f"low_data: sample_count={infl.sample_count} < {LOW_DATA_THRESHOLD}; "
                f"defaulting to advisory tier."
            )
        else:
            tier = _tier_for_score(infl.score)
            reason = (
                f"score={infl.score:.4f} from {infl.sample_count} outcomes "
                f"(decay_window={infl.decay_window_hours}h, window={infl.window_hours}h) "
                f"-> tier={tier}"
            )

        gates = _TIER_GATES.get(tier, {"relaxed": [], "tightened": []})
        return AgentPermissions(
            agent_id=agent_id,
            tier=tier,
            gates_relaxed=list(gates.get("relaxed", [])),
            gates_tightened=list(gates.get("tightened", [])),
            influence=infl,
            reason=reason,
        )
    except Exception as e:  # noqa: BLE001 — ZSF
        logger.error("compute_permissions(%s) failed: %s", agent_id, e)
        _bump("permission_compute_errors")
        return AgentPermissions(
            agent_id=agent_id,
            tier="restricted",  # fail-closed: on error, assume least privilege
            gates_relaxed=[],
            gates_tightened=list(_TIER_GATES["restricted"]["tightened"]),
            influence=None,
            reason=f"compute_permissions error: {e!r}; failing closed to restricted.",
        )


# ---------------------------------------------------------------------------
# Bulk listing + daemon hook
# ---------------------------------------------------------------------------

def _all_agent_ids() -> list[str]:
    """Distinct agent_ids found in the ledger (preserving discovery order)."""
    seen: set[str] = set()
    order: list[str] = []
    for obj in _iter_ledger():
        try:
            rec = obj.get("outcome_record") or {}
            aid = _record_agent_id(rec)
            if aid and aid not in seen:
                seen.add(aid)
                order.append(aid)
        except Exception as e:  # noqa: BLE001 — ZSF
            logger.error("_all_agent_ids parse error: %s", e)
            _bump("ledger_parse_errors")
            continue
    return order


def list_agent_influences() -> list[InfluenceScore]:
    """Compute influence for every agent currently observed in the ledger."""
    return [compute_influence(a) for a in _all_agent_ids()]


# ---------------------------------------------------------------------------
# OpenPathScore — composite score augmenting InfluenceScore.
#
# Origin: MavKa diff doc (~/Downloads/OpenPath_Context-DNA-MavKa.md L1205-1216) +
# E1 worth-adopting verdict + cross-node 3s green (mac2 +0.59, mac1 chief +1.00).
#
# Components are AUDITABLE EVIDENCE-GROUNDED ACTION PROPERTIES — never
# faith-based surgeon credentials (which would be R4 surgeon-weighted-voting,
# rejected per dao corrigibility-loop-algorithm.md).
#
# Score range: -100 to +100. Higher = safer. Caller maps to gate decision.
#
# Math (stdlib only):
#   score = Alignment + Reversibility + EvidenceSupport
#         - SecretExposure - ExternalImpact - Destructiveness - Novelty
#
# Each component is 0-100 (positive contributors) or 0-100 (negative subtractors).
# Caller supplies booleans/integers; this module aggregates and explains.
# ---------------------------------------------------------------------------


@dataclass
class OpenPathScore:
    """Composite action-property score. Augments (does NOT replace) InfluenceScore.

    Use this when you need an *action-axis* signal alongside the *agent-axis*
    InfluenceScore. Both feed into the gate decision; neither subsumes the
    other (per dao RiskLane-orthogonality clause).

    Components (each 0-100):
      Positive (raise score — action is safer):
        alignment            — match between action and stated user goal
        reversibility        — ease of rollback if action turns out wrong
        evidence_support     — strength of evidence backing the action

      Negative (lower score — action is riskier):
        secret_exposure      — risk of leaking credentials/keys/tokens
        external_impact      — blast radius beyond local node/repo
        destructiveness      — irreversibility of side effects (rm/drop/force)
        novelty              — distance from established patterns (untested = riskier)
    """

    agent_id: str
    alignment: int = 0
    reversibility: int = 0
    evidence_support: int = 0
    secret_exposure: int = 0
    external_impact: int = 0
    destructiveness: int = 0
    novelty: int = 0
    components_summary: str = ""

    @property
    def composite(self) -> int:
        """Composite score: positives - negatives. Range approx [-400, +300]."""
        positives = self.alignment + self.reversibility + self.evidence_support
        negatives = (
            self.secret_exposure + self.external_impact
            + self.destructiveness + self.novelty
        )
        return positives - negatives

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["composite"] = self.composite
        return d


def compute_openpath_score(
    agent_id: str,
    *,
    alignment: int = 0,
    reversibility: int = 0,
    evidence_support: int = 0,
    secret_exposure: int = 0,
    external_impact: int = 0,
    destructiveness: int = 0,
    novelty: int = 0,
) -> OpenPathScore:
    """Construct an OpenPathScore from caller-supplied component values.

    All components clamped to [0, 100]. Caller is responsible for deriving
    the integer values from action context (e.g., reversibility=100 if the
    action is `git checkout` on a clean branch; reversibility=0 if `rm -rf`).

    Returns an explainable dataclass — components_summary is a human-readable
    one-liner showing the math.
    """

    def _clamp(v: int) -> int:
        return max(0, min(100, int(v)))

    a = _clamp(alignment)
    r = _clamp(reversibility)
    e = _clamp(evidence_support)
    se = _clamp(secret_exposure)
    ei = _clamp(external_impact)
    d = _clamp(destructiveness)
    n = _clamp(novelty)

    summary = (
        f"+{a + r + e} (align={a}+rev={r}+ev={e}) "
        f"-{se + ei + d + n} (secret={se}+ext={ei}+destr={d}+novel={n}) "
        f"= {a + r + e - (se + ei + d + n)}"
    )
    return OpenPathScore(
        agent_id=agent_id,
        alignment=a,
        reversibility=r,
        evidence_support=e,
        secret_exposure=se,
        external_impact=ei,
        destructiveness=d,
        novelty=n,
        components_summary=summary,
    )


def summarize_for_health() -> dict[str, Any]:
    """Health snapshot for the fleet daemon to surface at /health.permissions.

    Returns ``{agents: [...], updated_at, errors}``. The daemon can poll this
    function — this module DOES NOT modify daemon code or auto-register.
    """
    agents_out: list[dict[str, Any]] = []
    for infl in list_agent_influences():
        # Re-derive tier directly so we don't pay another ledger pass.
        if infl.low_data:
            tier: PermissionTier = "advisory"
        else:
            tier = _tier_for_score(infl.score)
        agents_out.append({
            "id": infl.agent_id,
            "tier": tier,
            "score": infl.score,
            "sample_count": infl.sample_count,
            "low_data": infl.low_data,
            "last_outcome_at": infl.last_outcome_at,
        })
    return {
        "agents": agents_out,
        "updated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "errors": dict(COMPUTE_ERRORS),
        "ledger_path": str(LEDGER_PATH),
        "rules": {k: {"min": v["min"], "max": v["max"]} for k, v in PERMISSION_RULES.items()},
    }


# ---------------------------------------------------------------------------
# R3 — Outcome→Permission Governor (action-proposal evaluator surface)
# ---------------------------------------------------------------------------
#
# Companion surface to the agent-influence calculator above. Same module so
# `/health.permissions` callers and constitutional-physics consumers have a
# single import. This surface decides whether a *specific proposed action*
# proceeds, based on:
#   - the action's risk *tier* (T0_observe..T4_external_visible)
#   - prior *outcomes* of similar proposals (proposal_hash match)
#   - the action's reversibility (lowers required evidence count)
#
# Storage: sqlite single file at ``memory/permission_governor.db`` with two
# additive tables (existing ``permission_map_snapshots`` table left intact):
#   decisions(id, proposal_hash, tier, allow, rationale, ts)
#   outcomes (decision_id, outcome, evidence_json, ts)
#
# Constitutional physics:
#   - Preserve Determinism — same proposal + same ledger -> same decision
#     (proposal_hash is stable; sqlite query is deterministic).
#   - Prefer Reversible — reversible proposals get a lower evidence floor.
#   - Evidence Over Confidence — confidence alone never unlocks T2+; only
#     a track record of successful outcomes does.
#   - ZSF — every code path bumps a counter in ``GOVERNOR_COUNTERS``.
#
# No LLM calls. Pure rule-based. stdlib + (sqlite3 already in stdlib).
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import sqlite3 as _sqlite3
import threading as _threading
import uuid as _uuid

# Tier table — each tier names a minimum evidence count (successful prior
# outcomes for the same proposal_hash) and whether the tier is allowed by
# default with zero evidence. Reversible proposals get min_evidence-1.
PERMISSION_TIERS: dict[str, dict[str, Any]] = {
    "T0_observe": {
        "description": "read-only / observe — always allowed",
        "min_evidence": 0,
        "default_allow": True,
        "block_on_failure": False,
    },
    "T1_internal": {
        "description": "internal state mutation (memory updates, local caches)",
        "min_evidence": 0,
        "default_allow": True,
        "block_on_failure": True,
    },
    "T2_local_irreversible": {
        "description": "local irreversible (rm -rf, schema drop, file delete)",
        "min_evidence": 3,
        "default_allow": False,
        "block_on_failure": True,
    },
    "T3_cross_node": {
        "description": "affects other nodes (NATS publish to other macs, scp)",
        "min_evidence": 3,
        "default_allow": False,
        "block_on_failure": True,
    },
    "T4_external_visible": {
        "description": "public-facing (git push to remote, deploy, send email)",
        "min_evidence": 5,
        "default_allow": False,
        "block_on_failure": True,
    },
}

# Governor DB lives next to outcome ledger; sqlite single-file additive schema.
GOVERNOR_DB_PATH: pathlib.Path = _REPO_ROOT / "memory" / "permission_governor.db"

# ZSF counters — every code path lands in one of these. Surfaces via
# get_governor_counters() and on /health.zsf_counters.permission_governor.
GOVERNOR_COUNTERS: dict[str, int] = {
    "evaluate_total": 0,
    "evaluate_allow": 0,
    "evaluate_block": 0,
    "evaluate_t0_observe_allow": 0,
    "evaluate_unknown_tier_block": 0,
    "evaluate_insufficient_evidence_block": 0,
    "evaluate_failed_prior_block": 0,
    "evaluate_db_error": 0,
    "evaluate_internal_error": 0,
    "record_outcome_total": 0,
    "record_outcome_db_error": 0,
    "record_outcome_internal_error": 0,
}

_GOVERNOR_LOCK = _threading.Lock()


def _bump_governor(counter: str) -> None:
    """ZSF: increment a governor counter. Never raises."""
    with _GOVERNOR_LOCK:
        GOVERNOR_COUNTERS[counter] = GOVERNOR_COUNTERS.get(counter, 0) + 1


def get_governor_counters() -> dict[str, int]:
    """Snapshot of governor ZSF counters (thread-safe copy)."""
    with _GOVERNOR_LOCK:
        return dict(GOVERNOR_COUNTERS)


def _resolve_db_path(ledger_path: str | None) -> pathlib.Path:
    """Resolve the governor db path. ``ledger_path`` here is the GOVERNOR
    db path (mis-named in the spec but kept for back-compat with the
    R3 design doc). None -> module default.
    """
    if ledger_path is None:
        return GOVERNOR_DB_PATH
    return pathlib.Path(ledger_path)


def _ensure_schema(conn: _sqlite3.Connection) -> None:
    """Create governor tables if missing. Additive — never drops anything."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id            TEXT PRIMARY KEY,
            proposal_hash TEXT NOT NULL,
            tier          TEXT NOT NULL,
            allow         INTEGER NOT NULL,
            rationale     TEXT NOT NULL,
            ts            TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            decision_id   TEXT NOT NULL,
            outcome       TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            ts            TEXT NOT NULL,
            FOREIGN KEY (decision_id) REFERENCES decisions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_proposal_hash "
        "ON decisions(proposal_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_decision_id "
        "ON outcomes(decision_id)"
    )
    conn.commit()


def _connect(db_path: pathlib.Path) -> _sqlite3.Connection:
    """Open a connection and ensure schema. Caller closes."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(str(db_path))
    _ensure_schema(conn)
    return conn


def _canonical_proposal_payload(proposal: dict) -> str:
    """Stable JSON for hashing. Excludes volatile fields (timestamps, ids).

    Deterministic via sort_keys=True. Volatile keys filtered so two
    identical proposals issued seconds apart hash the same.
    """
    if not isinstance(proposal, dict):
        return ""
    volatile = {"timestamp", "ts", "now", "decision_id", "proposal_id", "nonce"}
    filtered = {k: v for k, v in proposal.items() if k not in volatile}
    try:
        return json.dumps(filtered, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — ZSF: fall back to repr
        return repr(sorted(filtered.items()))


def _hash_proposal(proposal: dict) -> str:
    payload = _canonical_proposal_payload(proposal)
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _prior_outcomes(
    conn: _sqlite3.Connection, proposal_hash: str
) -> tuple[int, int]:
    """Return (success_count, failure_count) for prior decisions whose
    proposal_hash matches. Counts only outcomes attached to *allowed*
    prior decisions — blocked decisions are not evidence.
    """
    rows = conn.execute(
        """
        SELECT o.outcome
        FROM outcomes o
        JOIN decisions d ON d.id = o.decision_id
        WHERE d.proposal_hash = ? AND d.allow = 1
        """,
        (proposal_hash,),
    ).fetchall()
    success = sum(1 for r in rows if str(r[0]).lower() in ("success", "ok", "pass"))
    failure = sum(1 for r in rows if str(r[0]).lower() in ("failure", "fail", "error", "blocked"))
    return success, failure


def _default_block_decision(
    rationale: str, counter: str, tier: str = "unknown"
) -> dict[str, Any]:
    """Build a deterministic default-block decision dict. Bumps counter."""
    _bump_governor(counter)
    return {
        "allow": False,
        "tier": tier,
        "rationale": rationale,
        "evidence_refs": [],
        "counter_id": f"governor::{counter}",
    }


def evaluate(
    proposal: dict,
    ledger_path: str | None = None,
) -> dict[str, Any]:
    """Decide whether a proposed action proceeds.

    Inputs:
      proposal: dict with at least ``action_class`` and ``tier`` keys.
        Optional: ``reversible`` (bool, lowers required evidence by 1),
        ``evidence_refs`` (list, propagated for audit), and any extra
        metadata (hashed for determinism).
      ledger_path: optional override for the governor db path.

    Returns dict:
      {'allow': bool, 'tier': str, 'rationale': str,
       'evidence_refs': [...], 'counter_id': str}

    Never raises. Every code path bumps a counter in GOVERNOR_COUNTERS.
    """
    _bump_governor("evaluate_total")

    # Defensive: non-dict / missing tier -> default-block.
    if not isinstance(proposal, dict):
        return _default_block_decision(
            f"proposal must be a dict, got {type(proposal).__name__}",
            "evaluate_internal_error",
        )

    tier = proposal.get("tier") or "unknown"
    if tier not in PERMISSION_TIERS:
        return _default_block_decision(
            f"unknown tier {tier!r} (known: {sorted(PERMISSION_TIERS)})",
            "evaluate_unknown_tier_block",
            tier=str(tier),
        )

    tier_spec = PERMISSION_TIERS[tier]
    reversible = bool(proposal.get("reversible", False))
    evidence_refs = list(proposal.get("evidence_refs") or [])
    proposal_hash = _hash_proposal(proposal)

    # T0 observe: always allowed regardless of prior outcomes.
    if tier == "T0_observe":
        _bump_governor("evaluate_t0_observe_allow")
        _bump_governor("evaluate_allow")
        return _persist_decision(
            ledger_path=ledger_path,
            proposal_hash=proposal_hash,
            tier=tier,
            allow=True,
            rationale="T0_observe: always allowed (read-only/observe class).",
            evidence_refs=evidence_refs,
            counter_id="governor::evaluate_t0_observe_allow",
        )

    # Query prior outcomes for this proposal_hash.
    try:
        db = _resolve_db_path(ledger_path)
        conn = _connect(db)
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: db open failed: %s", exc)
        return _default_block_decision(
            f"db open failed ({type(exc).__name__}): {exc}",
            "evaluate_db_error",
            tier=tier,
        )

    try:
        success_count, failure_count = _prior_outcomes(conn, proposal_hash)
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: prior_outcomes failed: %s", exc)
        conn.close()
        return _default_block_decision(
            f"prior_outcomes query failed ({type(exc).__name__}): {exc}",
            "evaluate_db_error",
            tier=tier,
        )

    # Any prior failure on a tier that blocks-on-failure -> block.
    if failure_count > 0 and tier_spec["block_on_failure"]:
        rationale = (
            f"prior failure on tier {tier}: {failure_count} failed outcome(s) "
            f"recorded for proposal_hash={proposal_hash}; block-on-failure "
            f"policy applies."
        )
        _bump_governor("evaluate_failed_prior_block")
        _bump_governor("evaluate_block")
        result = _persist_decision(
            conn=conn,
            ledger_path=ledger_path,
            proposal_hash=proposal_hash,
            tier=tier,
            allow=False,
            rationale=rationale,
            evidence_refs=evidence_refs,
            counter_id="governor::evaluate_failed_prior_block",
        )
        conn.close()
        return result

    # Evidence floor: reversible lowers the bar by 1 (min floor of 0).
    min_evidence = int(tier_spec["min_evidence"])
    if reversible and min_evidence > 0:
        min_evidence = min_evidence - 1

    if tier_spec["default_allow"] and success_count >= min_evidence:
        rationale = (
            f"tier {tier} default-allow met: success_count={success_count} "
            f">= min_evidence={min_evidence} (reversible={reversible})."
        )
        _bump_governor("evaluate_allow")
        result = _persist_decision(
            conn=conn,
            ledger_path=ledger_path,
            proposal_hash=proposal_hash,
            tier=tier,
            allow=True,
            rationale=rationale,
            evidence_refs=evidence_refs,
            counter_id="governor::evaluate_allow",
        )
        conn.close()
        return result

    if success_count >= min_evidence and min_evidence > 0:
        rationale = (
            f"tier {tier} unlocked by evidence: success_count={success_count} "
            f">= min_evidence={min_evidence} (reversible={reversible})."
        )
        _bump_governor("evaluate_allow")
        result = _persist_decision(
            conn=conn,
            ledger_path=ledger_path,
            proposal_hash=proposal_hash,
            tier=tier,
            allow=True,
            rationale=rationale,
            evidence_refs=evidence_refs,
            counter_id="governor::evaluate_allow",
        )
        conn.close()
        return result

    rationale = (
        f"tier {tier} requires min_evidence={min_evidence} successful prior "
        f"outcomes (have {success_count}); reversible={reversible}; "
        f"blocked pending evidence."
    )
    _bump_governor("evaluate_insufficient_evidence_block")
    _bump_governor("evaluate_block")
    result = _persist_decision(
        conn=conn,
        ledger_path=ledger_path,
        proposal_hash=proposal_hash,
        tier=tier,
        allow=False,
        rationale=rationale,
        evidence_refs=evidence_refs,
        counter_id="governor::evaluate_insufficient_evidence_block",
    )
    conn.close()
    return result


def _persist_decision(
    *,
    proposal_hash: str,
    tier: str,
    allow: bool,
    rationale: str,
    evidence_refs: list,
    counter_id: str,
    ledger_path: str | None = None,
    conn: _sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Insert a decision row + return the Decision dict.

    If ``conn`` is provided the caller manages connection lifetime;
    otherwise we open + close locally. Persistence failures are ZSF —
    counter bumped, but the in-memory decision is still returned so
    governance is never silently dropped.
    """
    decision_id = _uuid.uuid4().hex
    ts = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    own_conn = False
    if conn is None:
        try:
            conn = _connect(_resolve_db_path(ledger_path))
            own_conn = True
        except Exception as exc:  # noqa: BLE001 — ZSF
            logger.error("permission_governor: db open for persist failed: %s", exc)
            _bump_governor("evaluate_db_error")
            return {
                "allow": allow,
                "tier": tier,
                "rationale": rationale + f" (persist skipped: {exc!r})",
                "evidence_refs": evidence_refs,
                "counter_id": counter_id,
                "decision_id": decision_id,
            }

    try:
        conn.execute(
            "INSERT INTO decisions (id, proposal_hash, tier, allow, rationale, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, proposal_hash, tier, 1 if allow else 0, rationale, ts),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: persist failed: %s", exc)
        _bump_governor("evaluate_db_error")
    finally:
        if own_conn:
            conn.close()

    return {
        "allow": allow,
        "tier": tier,
        "rationale": rationale,
        "evidence_refs": evidence_refs,
        "counter_id": counter_id,
        "decision_id": decision_id,
    }


def record_outcome(
    decision_id: str,
    outcome: str,
    evidence: list,
    ledger_path: str | None = None,
) -> None:
    """Record what actually happened for a prior decision.

    Inputs:
      decision_id: id returned by evaluate()
      outcome: one of "success", "failure", "error", "blocked", or a free-form
        string (case-insensitive matching is used by _prior_outcomes).
      evidence: list of evidence refs (paths, urls, hashes); stored as JSON.
      ledger_path: optional override for the governor db path.

    Never raises. ZSF — every failure path bumps a counter.
    """
    _bump_governor("record_outcome_total")

    if not isinstance(decision_id, str) or not decision_id:
        _bump_governor("record_outcome_internal_error")
        return
    try:
        ev_json = json.dumps(list(evidence or []), default=str, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: evidence serialize failed: %s", exc)
        _bump_governor("record_outcome_internal_error")
        ev_json = json.dumps([])

    ts = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    try:
        conn = _connect(_resolve_db_path(ledger_path))
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: db open for record_outcome failed: %s", exc)
        _bump_governor("record_outcome_db_error")
        return

    try:
        conn.execute(
            "INSERT INTO outcomes (decision_id, outcome, evidence_json, ts) "
            "VALUES (?, ?, ?, ?)",
            (decision_id, str(outcome), ev_json, ts),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — ZSF
        logger.error("permission_governor: record_outcome insert failed: %s", exc)
        _bump_governor("record_outcome_db_error")
    finally:
        conn.close()


__all__ = [
    "InfluenceScore",
    "PermissionTier",
    "AgentPermissions",
    "OpenPathScore",
    "compute_openpath_score",
    "compute_influence",
    "compute_permissions",
    "list_agent_influences",
    "summarize_for_health",
    "PERMISSION_RULES",
    "LEDGER_PATH",
    "COMPUTE_ERRORS",
    "LOW_DATA_THRESHOLD",
    "NEUTRAL_SCORE",
    "DEFAULT_DECAY_WINDOW_HOURS",
    "DEFAULT_INFLUENCE_WINDOW_HOURS",
    # R3 action-proposal surface
    "PERMISSION_TIERS",
    "GOVERNOR_DB_PATH",
    "GOVERNOR_COUNTERS",
    "evaluate",
    "record_outcome",
    "get_governor_counters",
]
