#!/usr/bin/env python3
"""
Tests for gold pass → quarantine pipeline wiring.

Verifies that session_gold_passes._ds_quarantine_claim correctly routes
gold extractions into the ObservabilityStore quarantine system, and that
_route_downstream chains store_learning → quarantine_claim.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add memory directory to path for imports
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))


def _make_pass_def(downstream_type="fix", pass_id=1, name="SOP: Bug Fix Mining"):
    """Helper: build a minimal pass_def dict matching PASS_REGISTRY shape."""
    return {
        "id": pass_id,
        "name": name,
        "downstream": "store_learning",
        "downstream_type": downstream_type,
    }


def _make_item(session_id="abc123def456"):
    return {"session_id": session_id, "id": session_id}


class TestQuarantineClaimDownstream:
    """Unit tests for _ds_quarantine_claim handler."""

    @patch("memory.observability_store.get_observability_store")
    def test_quarantine_claim_creates_claim_with_correct_grade(self, mock_get_store):
        """Gold type 'fix' should map to 'anecdotal' evidence grade."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_store.record_claim_with_evidence.return_value = "claim-id-123"
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        pass_def = _make_pass_def(downstream_type="fix", pass_id=1)
        item = _make_item("sess_abcdef1234")
        content = "TITLE: Fix port binding\nSYMPTOM: Port 5044 in use"

        sgp._ds_quarantine_claim(pass_def, item, content)

        mock_store.record_claim_with_evidence.assert_called_once()
        call_kwargs = mock_store.record_claim_with_evidence.call_args
        # Positional or keyword — extract from kwargs
        kw = call_kwargs.kwargs if call_kwargs.kwargs else {}
        if not kw:
            # Called positionally — unpack
            args = call_kwargs.args
            kw = {
                "claim_text": args[0],
                "evidence_grade": args[1],
                "source": args[2],
                "confidence": args[3],
                "tags": args[4],
                "area": args[5],
            }

        assert kw["evidence_grade"] == "anecdotal"
        assert kw["confidence"] == 0.3
        assert "[GOLD:FIX]" in kw["claim_text"]
        assert kw["area"] == "gold_mining"
        assert "gold_type:fix" in kw["tags"]

    @patch("memory.observability_store.get_observability_store")
    def test_quarantine_claim_pattern_maps_to_case_series(self, mock_get_store):
        """Gold type 'pattern' should map to 'case_series' evidence grade."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_store.record_claim_with_evidence.return_value = "claim-id-456"
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        pass_def = _make_pass_def(downstream_type="pattern", pass_id=2, name="SOP: Pattern Mining")
        item = _make_item()
        content = "TITLE: Deploy workflow\nPROCESS: 1. Build 2. Test 3. Push"

        sgp._ds_quarantine_claim(pass_def, item, content)

        kw = mock_store.record_claim_with_evidence.call_args.kwargs
        assert kw["evidence_grade"] == "case_series"
        assert kw["confidence"] == 0.4
        assert "[GOLD:PATTERN]" in kw["claim_text"]

    @patch("memory.observability_store.get_observability_store")
    def test_quarantine_claim_decision_maps_to_expert_opinion(self, mock_get_store):
        """Gold type 'decision' should map to 'expert_opinion' evidence grade."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_store.record_claim_with_evidence.return_value = "claim-id-789"
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        pass_def = _make_pass_def(downstream_type="decision", pass_id=4, name="SOP: Architecture")
        item = _make_item()
        content = "TITLE: Use SQLite over Postgres for local\nDECISION: SQLite for speed"

        sgp._ds_quarantine_claim(pass_def, item, content)

        kw = mock_store.record_claim_with_evidence.call_args.kwargs
        assert kw["evidence_grade"] == "expert_opinion"
        assert kw["confidence"] == 0.5

    @patch("memory.observability_store.get_observability_store")
    def test_quarantine_claim_skips_empty_and_skip_content(self, mock_get_store):
        """Empty content or 'SKIP' should not create a claim."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        pass_def = _make_pass_def()
        item = _make_item()

        # Empty content
        sgp._ds_quarantine_claim(pass_def, item, "")
        mock_store.record_claim_with_evidence.assert_not_called()

        # SKIP content
        sgp._ds_quarantine_claim(pass_def, item, "SKIP")
        mock_store.record_claim_with_evidence.assert_not_called()

        # skip with whitespace
        sgp._ds_quarantine_claim(pass_def, item, "  skip  ")
        mock_store.record_claim_with_evidence.assert_not_called()


class TestRouteDownstreamChaining:
    """Tests that _route_downstream chains store_learning → quarantine."""

    @patch("memory.observability_store.get_observability_store")
    def test_store_learning_also_fires_quarantine(self, mock_get_store):
        """When downstream='store_learning', both handlers should fire."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_store.record_claim_with_evidence.return_value = "claim-id"
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        # Mock _ds_store_learning to avoid SQLite dependency
        sgp._ds_store_learning = MagicMock()

        pass_def = _make_pass_def(downstream_type="fix", pass_id=1)
        item = _make_item()
        result = {"content": "TITLE: Fix something\nSYMPTOM: It broke"}

        sgp._route_downstream("sop_bugfix", pass_def, item, result)

        # Both should have fired
        sgp._ds_store_learning.assert_called_once_with(pass_def, item, result["content"])
        mock_store.record_claim_with_evidence.assert_called_once()

    def test_non_store_learning_does_not_fire_quarantine(self):
        """Downstreams other than store_learning should NOT chain quarantine."""
        from memory.session_gold_passes import SessionGoldPassRunner

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        sgp._ds_sop_quality_score = MagicMock()
        sgp._ds_quarantine_claim = MagicMock()

        pass_def = {
            "id": 5,
            "name": "Eval: Quality",
            "downstream": "sop_quality_score",
        }
        item = _make_item()
        result = {"content": "SCORE: 4\nDETAIL: good"}

        sgp._route_downstream("eval_sop_quality", pass_def, item, result)

        sgp._ds_sop_quality_score.assert_called_once()
        sgp._ds_quarantine_claim.assert_not_called()

    @patch("memory.observability_store.get_observability_store")
    def test_quarantine_failure_does_not_break_store_learning(self, mock_get_store):
        """If quarantine insert fails, store_learning result should persist."""
        from memory.session_gold_passes import SessionGoldPassRunner

        mock_store = MagicMock()
        mock_store.record_claim_with_evidence.side_effect = Exception("DB locked")
        mock_get_store.return_value = mock_store

        sgp = SessionGoldPassRunner.__new__(SessionGoldPassRunner)
        sgp._ds_store_learning = MagicMock()

        pass_def = _make_pass_def(downstream_type="gotcha", pass_id=3)
        item = _make_item()
        result = {"content": "TITLE: Never do X\nNEVER_DO: X"}

        # Should not raise — error is caught
        sgp._route_downstream("sop_antipattern", pass_def, item, result)

        # store_learning still fired successfully
        sgp._ds_store_learning.assert_called_once()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
