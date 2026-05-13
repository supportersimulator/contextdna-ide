"""Full integration test — real chains with real segments end-to-end."""
import subprocess
import sys
import os
from pathlib import Path
import pytest

from memory.chain_engine import ChainExecutor, SEGMENT_REGISTRY, clear_registry
from memory.chain_requirements import RuntimeContext
from memory.chain_modes import ModeAuthority, PRESETS
from memory.chain_telemetry import ChainTelemetry, ExecutionRecord

# Register all segments
import memory.chain_segments_init  # noqa: F401


REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
CLI = [sys.executable, "memory/chain_cli.py"]
ENV = {**os.environ, "PYTHONPATH": REPO_ROOT}


def _ctx(llms=None, git=True):
    return RuntimeContext(
        healthy_llms=llms or ["test-llm"],
        state="mock", evidence="mock",
        git_available=git,
        git_root=REPO_ROOT if git else None,
    )


class TestSegmentRegistration:
    def test_core_segments_registered(self):
        assert "pre-flight" in SEGMENT_REGISTRY
        assert "verify" in SEGMENT_REGISTRY
        assert "gains-gate" in SEGMENT_REGISTRY

    def test_analysis_segments_registered(self):
        assert "risk-scan" in SEGMENT_REGISTRY
        assert "contradiction-scan" in SEGMENT_REGISTRY

    def test_audit_segments_registered(self):
        assert "audit-discover" in SEGMENT_REGISTRY
        assert "audit-read" in SEGMENT_REGISTRY
        assert "audit-extract" in SEGMENT_REGISTRY
        assert "audit-crosscheck" in SEGMENT_REGISTRY
        assert "audit-report" in SEGMENT_REGISTRY

    def test_review_segments_registered(self):
        assert "plan-review" in SEGMENT_REGISTRY
        assert "pre-impl" in SEGMENT_REGISTRY

    def test_total_segments(self):
        # 3 core + 2 analysis + 5 audit + 2 review = 12
        assert len(SEGMENT_REGISTRY) >= 12


class TestLightweightChainEndToEnd:
    def test_lightweight_runs_all_segments(self):
        """Lightweight chain: pre-flight → execute → verify.
        'execute' is not registered, so it gets skipped."""
        executor = ChainExecutor(halt_on_error=False)
        ctx = _ctx()
        segments = PRESETS["lightweight"]  # pre-flight, execute, verify

        state = executor.run(segments, ctx, initial_data={"topic": "test"})

        # pre-flight should run
        assert "preflight_ok" in state.data
        # execute should be skipped (not registered)
        assert "execute" in [s[0] for s in state.skipped]
        # verify should run
        assert "verified" in state.data

    def test_lightweight_telemetry(self):
        executor = ChainExecutor(halt_on_error=False)
        ctx = _ctx()
        state = executor.run(PRESETS["lightweight"], ctx, initial_data={"topic": "test"})

        total_ms = sum(state.segment_times_ns.values()) / 1_000_000
        rec = ExecutionRecord.create(
            chain_id="lightweight",
            segments_run=[s for s in PRESETS["lightweight"]
                          if s not in [sk[0] for sk in state.skipped]],
            segments_skipped=[sk[0] for sk in state.skipped],
            success=len(state.errors) == 0,
            duration_ms=total_ms,
            duration_by_segment={k: v/1e6 for k, v in state.segment_times_ns.items()},
            project_id="test",
        )
        ct = ChainTelemetry(backend="memory")
        ct.record(rec)
        assert len(ct.recent_executions("lightweight")) == 1


class TestPlanReviewChainEndToEnd:
    def test_plan_review_chain(self):
        """Plan-review chain: contradiction-scan → plan-review → pre-impl."""
        executor = ChainExecutor(halt_on_error=False)
        ctx = _ctx()
        segments = PRESETS["plan-review"]

        state = executor.run(segments, ctx, initial_data={
            "topic": "add chain telemetry to orchestration system",
        })

        # contradiction-scan should run (needs git)
        assert "contradiction_aligned" in state.data
        # plan-review should run
        assert "plan_review_verdict" in state.data
        # pre-impl should run
        assert "pre_impl_proceed" in state.data
        assert "pre_impl_summary" in state.data


class TestFullChainEndToEnd:
    def test_full_3s_partial(self):
        """Full-3s: runs what's registered, skips what's not."""
        executor = ChainExecutor(halt_on_error=False)
        ctx = _ctx()
        segments = PRESETS["full-3s"]

        state = executor.run(segments, ctx, initial_data={"topic": "full test"})

        # These should run (registered):
        assert "preflight_ok" in state.data        # pre-flight
        assert "risk_level" in state.data           # risk-scan
        assert "plan_review_verdict" in state.data  # plan-review
        assert "pre_impl_proceed" in state.data     # pre-impl
        assert "verified" in state.data             # verify
        assert "gains_gate_pass" in state.data      # gains-gate

        # These should be skipped (not registered):
        skipped_names = [s[0] for s in state.skipped]
        assert "execute" in skipped_names
        assert "arch-gate" in skipped_names
        assert "doc-flow" in skipped_names


class TestAuditChainViaExecutor:
    def test_audit_chain_test_mode(self):
        """Run audit segments as a custom chain."""
        executor = ChainExecutor(halt_on_error=False)
        ctx = _ctx(llms=["test-llm"])
        audit_segments = [
            "audit-discover", "audit-read", "audit-extract",
            "audit-crosscheck", "audit-report",
        ]

        state = executor.run(audit_segments, ctx, initial_data={
            "topic": "chain orchestration",
            "_test_mode": True,
        })

        assert state.data.get("audit_complete") is True
        assert state.data.get("audit_docs_read", 0) > 0
        assert len(state.errors) == 0


class TestCLIWithSegments:
    def test_cli_presets_shows_registered(self):
        result = subprocess.run(
            CLI + ["presets"], capture_output=True, text=True,
            cwd=REPO_ROOT, env=ENV)
        assert result.returncode == 0, result.stderr
        # Should show checkmarks for registered segments
        assert "✓" in result.stdout
        assert "Registered segments:" in result.stdout

    def test_cli_run_plan_review(self):
        result = subprocess.run(
            CLI + ["run", "plan-review", "--topic", "test chain segments"],
            capture_output=True, text=True, cwd=REPO_ROOT, env=ENV)
        assert result.returncode == 0, result.stderr
        # Should show actual results, not all skipped
        assert "plan-review" in result.stdout.lower() or "Results" in result.stdout

    def test_cli_run_lightweight(self):
        result = subprocess.run(
            CLI + ["run", "lightweight", "--topic", "quick test"],
            capture_output=True, text=True, cwd=REPO_ROOT, env=ENV)
        assert result.returncode == 0, result.stderr
