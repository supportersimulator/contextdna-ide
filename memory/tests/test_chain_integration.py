"""Integration test — full chain lifecycle: register → resolve → execute → telemetry."""
import pytest
from memory.chain_engine import (
    ChainSegment, ChainState, ChainExecutor, SEGMENT_REGISTRY,
    segment, clear_registry,
)
from memory.chain_requirements import CommandRequirements, RuntimeContext
from memory.chain_modes import ModeAuthority
from memory.chain_telemetry import ChainTelemetry, ExecutionRecord, EvidenceGrade
from memory.chain_consultation import should_consult, ChainConsultation
from memory.chain_config import ChainConfig, TelemetryConfig


@pytest.fixture(autouse=True)
def clean():
    saved = dict(SEGMENT_REGISTRY)
    clear_registry()
    yield
    clear_registry()
    SEGMENT_REGISTRY.update(saved)


def _ctx(llms=2):
    names = ["llm-a", "llm-b", "llm-c"][:llms]
    return RuntimeContext(
        healthy_llms=names, state="mock", evidence="mock",
        git_available=True, git_root="/tmp",
    )


def test_full_lifecycle():
    """Register segments → resolve preset → execute → verify telemetry."""

    @segment(name="pre-flight", requires=CommandRequirements())
    def pre_flight(ctx, data):
        return {"preflight": "ok"}

    @segment(name="execute", requires=CommandRequirements(min_llms=1))
    def execute(ctx, data):
        return {"executed": True, "saw_preflight": data.get("preflight")}

    @segment(name="verify", requires=CommandRequirements())
    def verify(ctx, data):
        return {"verified": data.get("executed", False)}

    ma = ModeAuthority()
    segments = ma.resolve("lightweight")
    assert segments == ["pre-flight", "execute", "verify"]

    executor = ChainExecutor()
    ctx = _ctx()
    state = executor.run(segments, ctx, initial_data={"topic": "test"})

    assert state.data["preflight"] == "ok"
    assert state.data["executed"] is True
    assert state.segment_results["execute"]["saw_preflight"] == "ok"
    assert state.data["verified"] is True
    assert len(state.errors) == 0
    assert state.halted is False

    total_ms = sum(state.segment_times_ns.values()) / 1_000_000
    rec = ExecutionRecord.create(
        chain_id="lightweight",
        segments_run=["pre-flight", "execute", "verify"],
        segments_skipped=[], success=True, duration_ms=total_ms,
        duration_by_segment={k: v / 1e6 for k, v in state.segment_times_ns.items()},
        project_id="test",
    )
    ct = ChainTelemetry(backend="memory")
    ct.record(rec)

    recent = ct.recent_executions("lightweight")
    assert len(recent) == 1
    assert recent[0].success is True


def test_mixed_gate_results():
    """Blocked, degraded, and proceed segments in one chain."""

    @segment(name="always-runs", requires=CommandRequirements())
    def always(ctx, data):
        return {"always": True}

    @segment(name="needs-git", requires=CommandRequirements(needs_git=True))
    def needs_git(ctx, data):
        return {"git_ran": True}

    @segment(name="wants-3", requires=CommandRequirements(min_llms=1, recommended_llms=3))
    def wants_3(ctx, data):
        return {"degraded_ran": True}

    @segment(name="needs-5", requires=CommandRequirements(min_llms=5))
    def needs_5(ctx, data):
        return {"impossible": True}

    executor = ChainExecutor(halt_on_error=False)
    ctx = _ctx(llms=2)
    state = executor.run(["always-runs", "needs-git", "wants-3", "needs-5"], ctx)

    assert state.data["always"] is True
    assert state.data["git_ran"] is True
    assert state.data["degraded_ran"] is True
    assert "wants-3" in [d[0] for d in state.degraded]
    assert state.data.get("impossible") is None
    assert "needs-5" in [s[0] for s in state.skipped]


def test_consultation_cadence_with_telemetry():
    """Consultation fires at cadence intervals."""
    consultation = ChainConsultation()
    ct = ChainTelemetry(backend="memory")

    for i in range(20):
        rec = ExecutionRecord.create(
            chain_id="cadence-test", segments_run=["a"],
            segments_skipped=[], success=True, duration_ms=10,
            duration_by_segment={"a": 10}, project_id="test",
        )
        ct.record(rec)

    total = len(ct.recent_executions("cadence-test"))
    assert should_consult(total, consultation.last_consultation_at, cadence=20)
    consultation.mark_consulted(at_execution=total)
    assert not should_consult(total, consultation.last_consultation_at, cadence=20)


def test_config_drives_telemetry():
    """TelemetryConfig values are respected."""
    cfg = TelemetryConfig(min_observations_for_pattern=3, min_frequency_for_pattern=0.60)
    ct = ChainTelemetry(
        backend="memory",
        min_observations=cfg.min_observations_for_pattern,
        min_frequency=cfg.min_frequency_for_pattern,
    )
    for _ in range(3):
        rec = ExecutionRecord.create(
            chain_id="cfg-test", segments_run=["x", "y"],
            segments_skipped=[], success=True, duration_ms=10,
            duration_by_segment={"x": 5, "y": 5}, project_id="test",
        )
        ct.record(rec)

    patterns = ct.detect_patterns("cfg-test")
    assert len(patterns) >= 1
