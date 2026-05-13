#!/usr/bin/env python3
"""
Tests for Professor Confidence Decay (Natural Selection mechanism).

Covers:
1. No stale domains — graceful no-op
2. 30-day decay tier — -0.05
3. 60-day decay tier — -0.10
4. 90-day decay tier — -0.15
5. Floor enforcement — never below 0.3
6. Fresh domains untouched — <30 days no decay
7. Missing/invalid timestamps — skipped gracefully
8. Flagging — heavily decayed domains flagged for review
9. Idempotency — decay doesn't double-apply on same data
10. Public API — standalone decay_stale_confidence() function works
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

MEMORY_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(MEMORY_DIR.parent))


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temp directory and patch file paths."""
    evolution_file = tmp_path / ".professor_evolution.json"
    confidence_file = tmp_path / ".professor_domain_confidence.json"

    patches = [
        patch("memory.professor.EVOLUTION_FILE", evolution_file),
        patch("memory.professor.DOMAIN_CONFIDENCE_FILE", confidence_file),
    ]
    for p in patches:
        p.start()

    yield tmp_path

    for p in patches:
        p.stop()


@pytest.fixture
def evolution(temp_dir):
    """Fresh ProfessorEvolution instance with temp storage."""
    from memory.professor import ProfessorEvolution
    return ProfessorEvolution()


def _seed_confidence(tmp_path, domains: dict):
    """Helper: write domain confidence data to the sidecar file."""
    confidence_file = tmp_path / ".professor_domain_confidence.json"
    confidence_file.write_text(json.dumps(domains, indent=2))


def _days_ago(days: int) -> str:
    """Helper: ISO timestamp for N days ago."""
    return (datetime.now() - timedelta(days=days)).isoformat()


# -- Test 1: No stale domains --

class TestNoStaleDomains:
    def test_empty_confidence_file(self, evolution):
        """No confidence data at all results in no-op."""
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"] == {}
        assert result["domains_at_floor"] == []
        assert result["decay_log"] == []

    def test_all_domains_fresh(self, evolution, temp_dir):
        """Domains updated recently are not decayed."""
        _seed_confidence(temp_dir, {
            "async_python": {
                "score": 0.8,
                "adjustments": 5,
                "last_updated": _days_ago(10),
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"] == {}


# -- Test 2: 30-day decay --

class TestThirtyDayDecay:
    def test_30_day_stale_decays_005(self, evolution, temp_dir):
        """Domain with 30+ days staleness loses 0.05."""
        _seed_confidence(temp_dir, {
            "async_python": {
                "score": 0.7,
                "adjustments": 3,
                "last_updated": _days_ago(35),
            },
        })
        result = evolution.decay_stale_confidence()
        assert "async_python" in result["domains_decayed"]
        entry = result["domains_decayed"]["async_python"]
        assert entry["old_score"] == 0.7
        assert entry["new_score"] == 0.65
        assert entry["decay_applied"] == 0.05
        assert entry["days_stale"] == 35

    def test_45_days_still_005_tier(self, evolution, temp_dir):
        """45 days is still in the 30-day tier (not 60)."""
        _seed_confidence(temp_dir, {
            "docker_ecs": {
                "score": 0.8,
                "adjustments": 2,
                "last_updated": _days_ago(45),
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"]["docker_ecs"]["decay_applied"] == 0.05


# -- Test 3: 60-day decay --

class TestSixtyDayDecay:
    def test_60_day_stale_decays_010(self, evolution, temp_dir):
        """Domain with 60+ days staleness loses 0.10."""
        _seed_confidence(temp_dir, {
            "voice_pipeline": {
                "score": 0.75,
                "adjustments": 4,
                "last_updated": _days_ago(65),
            },
        })
        result = evolution.decay_stale_confidence()
        entry = result["domains_decayed"]["voice_pipeline"]
        assert entry["old_score"] == 0.75
        assert entry["new_score"] == 0.65
        assert entry["decay_applied"] == 0.1


# -- Test 4: 90-day decay --

class TestNinetyDayDecay:
    def test_90_day_stale_decays_015(self, evolution, temp_dir):
        """Domain with 90+ days staleness loses 0.15."""
        _seed_confidence(temp_dir, {
            "webrtc_livekit": {
                "score": 0.8,
                "adjustments": 2,
                "last_updated": _days_ago(100),
            },
        })
        result = evolution.decay_stale_confidence()
        entry = result["domains_decayed"]["webrtc_livekit"]
        assert entry["old_score"] == 0.8
        assert entry["new_score"] == 0.65
        assert entry["decay_applied"] == 0.15

    def test_180_days_still_015_tier(self, evolution, temp_dir):
        """180 days uses the same 0.15 decay as 90 days."""
        _seed_confidence(temp_dir, {
            "django_backend": {
                "score": 0.9,
                "adjustments": 10,
                "last_updated": _days_ago(180),
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"]["django_backend"]["decay_applied"] == 0.15


# -- Test 5: Floor enforcement --

class TestFloorEnforcement:
    def test_decay_respects_030_floor(self, evolution, temp_dir):
        """Score cannot drop below 0.3 floor."""
        _seed_confidence(temp_dir, {
            "testing": {
                "score": 0.35,
                "adjustments": 8,
                "last_updated": _days_ago(95),
            },
        })
        result = evolution.decay_stale_confidence()
        entry = result["domains_decayed"]["testing"]
        assert entry["new_score"] == 0.3
        assert "testing" in result["domains_at_floor"]

    def test_already_at_floor_no_change(self, evolution, temp_dir):
        """Domain already at 0.3 is not decayed further."""
        _seed_confidence(temp_dir, {
            "build_deploy": {
                "score": 0.3,
                "adjustments": 12,
                "last_updated": _days_ago(120),
            },
        })
        result = evolution.decay_stale_confidence()
        assert "build_deploy" not in result["domains_decayed"]
        assert "build_deploy" in result["domains_at_floor"]


# -- Test 6: Fresh domains untouched --

class TestFreshDomains:
    def test_29_days_no_decay(self, evolution, temp_dir):
        """Domain updated 29 days ago is not decayed."""
        _seed_confidence(temp_dir, {
            "database": {
                "score": 0.7,
                "adjustments": 1,
                "last_updated": _days_ago(29),
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"] == {}

    def test_mixed_fresh_and_stale(self, evolution, temp_dir):
        """Only stale domains are decayed, fresh ones untouched."""
        _seed_confidence(temp_dir, {
            "async_python": {
                "score": 0.8,
                "adjustments": 5,
                "last_updated": _days_ago(5),
            },
            "docker_ecs": {
                "score": 0.7,
                "adjustments": 3,
                "last_updated": _days_ago(40),
            },
        })
        result = evolution.decay_stale_confidence()
        assert "async_python" not in result["domains_decayed"]
        assert "docker_ecs" in result["domains_decayed"]


# -- Test 7: Missing/invalid timestamps --

class TestInvalidTimestamps:
    def test_missing_last_updated_skipped(self, evolution, temp_dir):
        """Domain with no last_updated is skipped."""
        _seed_confidence(temp_dir, {
            "memory_system": {
                "score": 0.7,
                "adjustments": 2,
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"] == {}

    def test_invalid_timestamp_skipped(self, evolution, temp_dir):
        """Domain with unparseable timestamp is skipped with log entry."""
        _seed_confidence(temp_dir, {
            "git_version_control": {
                "score": 0.7,
                "adjustments": 1,
                "last_updated": "not-a-timestamp",
            },
        })
        result = evolution.decay_stale_confidence()
        assert result["domains_decayed"] == {}
        assert any("invalid" in log for log in result["decay_log"])

    def test_internal_keys_ignored(self, evolution, temp_dir):
        """Keys starting with _ (like _watermark) are ignored."""
        _seed_confidence(temp_dir, {
            "_watermark": "2026-01-01T00:00:00",
            "async_python": {
                "score": 0.7,
                "adjustments": 3,
                "last_updated": _days_ago(40),
            },
        })
        result = evolution.decay_stale_confidence()
        assert "async_python" in result["domains_decayed"]
        # _watermark should not appear in any result
        assert "_watermark" not in result["domains_decayed"]


# -- Test 8: Flagging heavily decayed domains --

class TestFlagging:
    def test_domain_below_04_flagged_for_review(self, evolution, temp_dir):
        """Domain decayed below 0.4 is flagged for review."""
        _seed_confidence(temp_dir, {
            "frontend_react": {
                "score": 0.45,
                "adjustments": 6,
                "last_updated": _days_ago(95),
            },
        })
        result = evolution.decay_stale_confidence()
        # 0.45 - 0.15 = 0.30 (below 0.4)
        flagged_domains = [
            f["domain"] for f in evolution.data.get("flagged_for_review", [])
            if isinstance(f, dict)
        ]
        assert "frontend_react" in flagged_domains

    def test_no_duplicate_flags(self, evolution, temp_dir):
        """Running decay twice doesn't create duplicate flags."""
        _seed_confidence(temp_dir, {
            "frontend_react": {
                "score": 0.45,
                "adjustments": 6,
                "last_updated": _days_ago(95),
            },
        })
        evolution.decay_stale_confidence()

        # Re-seed slightly above floor so decay can trigger again
        _seed_confidence(temp_dir, {
            "frontend_react": {
                "score": 0.35,
                "adjustments": 7,
                "last_updated": _days_ago(95),
                "last_decay": datetime.now().isoformat(),
            },
        })
        evolution.decay_stale_confidence()

        stale_flags = [
            f for f in evolution.data.get("flagged_for_review", [])
            if isinstance(f, dict) and f.get("domain") == "frontend_react"
            and "stale" in f.get("reason", "").lower()
        ]
        assert len(stale_flags) == 1


# -- Test 9: Persistence --

class TestPersistence:
    def test_decay_persisted_to_sidecar(self, temp_dir, evolution):
        """Decayed scores are persisted to the confidence sidecar file."""
        _seed_confidence(temp_dir, {
            "async_python": {
                "score": 0.8,
                "adjustments": 5,
                "last_updated": _days_ago(40),
            },
        })
        evolution.decay_stale_confidence()

        # Read the file directly
        confidence_file = temp_dir / ".professor_domain_confidence.json"
        data = json.loads(confidence_file.read_text())
        assert data["async_python"]["score"] == 0.75
        assert "last_decay" in data["async_python"]


# -- Test 10: Public API --

class TestPublicAPI:
    def test_standalone_decay_function(self, temp_dir):
        """The module-level decay_stale_confidence() function works."""
        import memory.professor as prof_mod
        prof_mod._evolution = None

        _seed_confidence(temp_dir, {
            "voice_pipeline": {
                "score": 0.7,
                "adjustments": 2,
                "last_updated": _days_ago(65),
            },
        })

        from memory.professor import decay_stale_confidence
        result = decay_stale_confidence()
        assert "voice_pipeline" in result["domains_decayed"]
        assert result["domains_decayed"]["voice_pipeline"]["new_score"] == 0.6

        # Cleanup singleton
        prof_mod._evolution = None
