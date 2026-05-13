"""
Integration tests for SynapticPersonality (Session-5 ALIVE deliverable).

Verifies the persistent personality contract:
1. Cross-process persistence (write -> reopen -> read)
2. Relationship interaction_count accumulates across simulated sessions
3. Emotional state retrieval returns latest with timestamp
4. Idempotent schema init does not reset data
5. Trait evolution_count increments on identical-value updates (stability signal)

Run:
    PYTHONPATH=. python3 -m pytest memory/test_synaptic_personality.py -v
    PYTHONPATH=. python3 memory/test_synaptic_personality.py        # standalone
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_tmp_db() -> Path:
    """Return a fresh temp DB path that does NOT route through unified DB."""
    return Path(tempfile.mkdtemp(prefix="syn_pers_test_")) / "test_personality.db"


class CrossProcessPersistenceTest(unittest.TestCase):
    """A trait written in process A must be readable from process B."""

    def test_trait_persists_across_process_restart(self):
        db = _make_tmp_db()

        # Process A: write a trait, then exit
        write_code = (
            "import sys; sys.path.insert(0, %r);"
            "from memory.synaptic_personality import SynapticPersonality;"
            "from pathlib import Path;"
            "p = SynapticPersonality(db_path=Path(%r));"
            "p.update_trait('warmth', 'high', confidence=0.85);"
            "p.update_trait('directness', 'high', confidence=0.9);"
            "print('WROTE')"
        ) % (str(REPO_ROOT), str(db))
        r = subprocess.run([sys.executable, "-c", write_code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"writer failed: {r.stderr}")
        self.assertIn("WROTE", r.stdout)

        # Process B: read it back fresh
        read_code = (
            "import sys, json; sys.path.insert(0, %r);"
            "from memory.synaptic_personality import SynapticPersonality;"
            "from pathlib import Path;"
            "p = SynapticPersonality(db_path=Path(%r));"
            "print('OUT:' + json.dumps(p.get_traits()))"
        ) % (str(REPO_ROOT), str(db))
        r2 = subprocess.run([sys.executable, "-c", read_code], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, f"reader failed: {r2.stderr}")
        out_line = [l for l in r2.stdout.splitlines() if l.startswith("OUT:")][0]
        import json as _j
        traits = _j.loads(out_line[len("OUT:"):])
        self.assertIn("warmth", traits)
        self.assertIn("directness", traits)
        self.assertEqual(traits["warmth"]["value"], "high")
        self.assertEqual(traits["directness"]["value"], "high")


class RelationshipAccumulationTest(unittest.TestCase):
    """update_relationship called twice across simulated sessions -> count=2."""

    def test_relationship_interaction_count_accumulates(self):
        from memory.synaptic_personality import SynapticPersonality

        db = _make_tmp_db()

        # Simulated session 1
        p1 = SynapticPersonality(db_path=db)
        p1.update_relationship("aaron", trust_delta=0.1, notes="first contact")
        del p1  # drop reference

        # Simulated session 2 (new instance)
        p2 = SynapticPersonality(db_path=db)
        p2.update_relationship("aaron", trust_delta=0.1, notes="second")
        rel = p2.get_relationship("aaron")

        self.assertIsNotNone(rel)
        self.assertEqual(rel["interaction_count"], 2)
        # 0.5 baseline + 0.1 + 0.1 = 0.7
        self.assertAlmostEqual(rel["trust_score"], 0.7, places=5)


class EmotionalStateLatestTest(unittest.TestCase):
    """record_emotion 5 times; get_emotional_state returns the LATEST with timestamp."""

    def test_record_5_emotions_returns_latest(self):
        from memory.synaptic_personality import SynapticPersonality

        db = _make_tmp_db()
        p = SynapticPersonality(db_path=db)

        valences = [-0.5, 0.0, 0.3, 0.6, 0.95]
        for v in valences:
            p.record_emotion(v, arousal=0.5, focus=0.7, notes=f"v={v}")
            time.sleep(0.001)  # ensure timestamp ordering monotonic

        latest = p.get_emotional_state()
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest["valence"], 0.95, places=5)
        self.assertEqual(latest["notes"], "v=0.95")
        self.assertTrue(latest["timestamp"])  # has timestamp


class IdempotentSchemaTest(unittest.TestCase):
    """Re-initializing the personality DB must NOT wipe existing data."""

    def test_reinit_preserves_data(self):
        from memory.synaptic_personality import SynapticPersonality

        db = _make_tmp_db()

        p1 = SynapticPersonality(db_path=db)
        p1.update_trait("focus", "deep", confidence=0.8)
        p1.record_emotion(0.5, 0.5, 0.9, "test")
        p1.update_relationship("atlas", trust_delta=0.3)
        sid = p1.record_session_start()
        p1.record_session_end("idempotent test", themes=["schema", "stability"], session_id=sid)

        # Re-init (simulates server restart hitting _init_database again)
        p2 = SynapticPersonality(db_path=db)
        p3 = SynapticPersonality(db_path=db)  # third init for good measure

        traits = p3.get_traits()
        self.assertIn("focus", traits)
        self.assertEqual(traits["focus"]["value"], "deep")

        emo = p3.get_emotional_state()
        self.assertIsNotNone(emo)
        self.assertAlmostEqual(emo["focus"], 0.9, places=5)

        rel = p3.get_relationship("atlas")
        self.assertIsNotNone(rel)
        self.assertEqual(rel["interaction_count"], 1)

        sessions = p3.get_recent_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["summary"], "idempotent test")
        self.assertEqual(sorted(sessions[0]["themes"]), ["schema", "stability"])


class TraitEvolutionCountTest(unittest.TestCase):
    """Identical-value trait updates increment evolution_count without changing value."""

    def test_identical_updates_bump_evolution_count(self):
        from memory.synaptic_personality import SynapticPersonality

        db = _make_tmp_db()
        p = SynapticPersonality(db_path=db)

        for _ in range(4):
            p.update_trait("curiosity", "high", confidence=0.9)

        traits = p.get_traits()
        self.assertEqual(traits["curiosity"]["value"], "high")
        self.assertEqual(traits["curiosity"]["evolution_count"], 4)

        # Now flip the value -> evolution_count keeps climbing, value changes
        p.update_trait("curiosity", "moderate", confidence=0.7)
        traits = p.get_traits()
        self.assertEqual(traits["curiosity"]["value"], "moderate")
        self.assertEqual(traits["curiosity"]["evolution_count"], 5)


class SummaryLineTest(unittest.TestCase):
    """Personality summary line for S6/S8 webhook injection."""

    def test_summary_line_format(self):
        from memory.synaptic_personality import SynapticPersonality

        db = _make_tmp_db()
        p = SynapticPersonality(db_path=db)
        # No state -> empty
        self.assertEqual(p.get_personality_summary_line(), "")

        p.record_emotion(0.6, 0.4, 0.85, "flow")
        p.update_relationship("aaron", trust_delta=0.4)
        line = p.get_personality_summary_line()

        self.assertTrue(line.startswith("Synaptic:"))
        self.assertIn("focus=high", line)
        self.assertIn("trust(aaron)=0.90", line)
        self.assertIn("last seen", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
