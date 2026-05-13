"""Tests for chain_engine — segment registration, state, and execution."""
import pytest
from memory.chain_engine import (
    ChainSegment, SEGMENT_REGISTRY, segment, clear_registry,
    ChainState, ChainExecutor,
)
from memory.chain_requirements import CommandRequirements, RuntimeContext, GateResult


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(SEGMENT_REGISTRY)
    clear_registry()
    yield
    clear_registry()
    SEGMENT_REGISTRY.update(saved)


def _make_ctx(llms=None, state="mock", evidence="mock", git=True):
    return RuntimeContext(
        healthy_llms=llms or ["claude", "deepseek"],
        state=state, evidence=evidence,
        git_available=git, git_root="/tmp/repo" if git else None,
    )


# --- Segment registration tests ---

def test_segment_decorator_registers():
    @segment(name="test-seg", requires=CommandRequirements())
    def my_seg(ctx, data):
        return {"result": "ok"}
    assert "test-seg" in SEGMENT_REGISTRY
    assert SEGMENT_REGISTRY["test-seg"].fn is my_seg


def test_segment_decorator_preserves_function():
    @segment(name="preserved", requires=CommandRequirements())
    def my_fn(ctx, data):
        return {"x": 1}
    assert my_fn(None, {}) == {"x": 1}


def test_segment_duplicate_raises():
    @segment(name="dup", requires=CommandRequirements())
    def first(ctx, data):
        return {}
    with pytest.raises(ValueError, match="already registered"):
        @segment(name="dup", requires=CommandRequirements())
        def second(ctx, data):
            return {}


def test_segment_tags():
    @segment(name="tagged", requires=CommandRequirements(), tags=["audit", "research"])
    def tagged_fn(ctx, data):
        return {}
    seg = SEGMENT_REGISTRY["tagged"]
    assert "audit" in seg.tags
    assert "research" in seg.tags


def test_chain_segment_dataclass():
    seg = ChainSegment(name="manual", fn=lambda ctx, data: {},
                       requires=CommandRequirements(min_llms=1), tags=["test"])
    assert seg.name == "manual"
    assert seg.requires.min_llms == 1
    assert seg.learned_deps == set()
    assert seg.learned_synergies == set()


def test_clear_registry():
    @segment(name="temp", requires=CommandRequirements())
    def temp(ctx, data):
        return {}
    assert "temp" in SEGMENT_REGISTRY
    clear_registry()
    assert "temp" not in SEGMENT_REGISTRY


# --- ChainState tests ---

def test_chain_state_defaults():
    state = ChainState()
    assert state.data == {}
    assert state.skipped == []
    assert state.degraded == []
    assert state.errors == []
    assert state.halted is False


# --- Executor tests ---

def test_executor_runs_segments_in_order():
    results = []
    @segment(name="step-a", requires=CommandRequirements())
    def step_a(ctx, data):
        results.append("a")
        return {"from_a": True}
    @segment(name="step-b", requires=CommandRequirements())
    def step_b(ctx, data):
        results.append("b")
        return {"from_b": True, "saw_a": data.get("from_a")}
    executor = ChainExecutor()
    state = executor.run(["step-a", "step-b"], _make_ctx())
    assert results == ["a", "b"]
    assert state.data["from_a"] is True
    assert state.data["from_b"] is True
    assert state.segment_results["step-b"]["saw_a"] is True


def test_executor_skips_blocked_segments():
    @segment(name="needs-3", requires=CommandRequirements(min_llms=3))
    def needs_three(ctx, data):
        return {"ran": True}
    @segment(name="no-reqs", requires=CommandRequirements())
    def no_reqs(ctx, data):
        return {"fallback": True}
    executor = ChainExecutor()
    state = executor.run(["needs-3", "no-reqs"], _make_ctx(llms=["one"]))
    assert "needs-3" in [s[0] for s in state.skipped]
    assert state.data.get("ran") is None
    assert state.data["fallback"] is True


def test_executor_records_degraded():
    @segment(name="wants-3", requires=CommandRequirements(min_llms=1, recommended_llms=3))
    def wants_three(ctx, data):
        return {"ran": True}
    executor = ChainExecutor()
    state = executor.run(["wants-3"], _make_ctx(llms=["one"]))
    assert state.data["ran"] is True
    assert "wants-3" in [d[0] for d in state.degraded]


def test_executor_halt_on_error():
    @segment(name="explode", requires=CommandRequirements())
    def explode(ctx, data):
        raise RuntimeError("boom")
    @segment(name="after", requires=CommandRequirements())
    def after(ctx, data):
        return {"reached": True}
    executor = ChainExecutor(halt_on_error=True)
    state = executor.run(["explode", "after"], _make_ctx())
    assert state.halted is True
    assert "boom" in state.halt_reason
    assert state.data.get("reached") is None


def test_executor_collect_errors_mode():
    @segment(name="err-seg", requires=CommandRequirements())
    def err_seg(ctx, data):
        raise RuntimeError("oops")
    @segment(name="ok-seg", requires=CommandRequirements())
    def ok_seg(ctx, data):
        return {"ok": True}
    executor = ChainExecutor(halt_on_error=False)
    state = executor.run(["err-seg", "ok-seg"], _make_ctx())
    assert state.halted is False
    assert len(state.errors) == 1
    assert state.data["ok"] is True


def test_executor_records_timing():
    @segment(name="timed", requires=CommandRequirements())
    def timed(ctx, data):
        return {"done": True}
    executor = ChainExecutor()
    state = executor.run(["timed"], _make_ctx())
    assert "timed" in state.segment_times_ns
    assert state.segment_times_ns["timed"] > 0


def test_executor_unknown_segment_skipped():
    executor = ChainExecutor()
    state = executor.run(["nonexistent"], _make_ctx())
    assert "nonexistent" in [s[0] for s in state.skipped]


def test_executor_initial_data():
    @segment(name="reader", requires=CommandRequirements())
    def reader(ctx, data):
        return {"saw_init": data.get("init_val")}
    executor = ChainExecutor()
    state = executor.run(["reader"], _make_ctx(), initial_data={"init_val": 42})
    assert state.segment_results["reader"]["saw_init"] == 42
