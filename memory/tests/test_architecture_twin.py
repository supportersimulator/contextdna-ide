"""
Tests for Architecture Twin — Module dependency graph from code_chunks.db.

Covers:
- refresh_twin() produces valid JSON with expected schema
- Idempotent on unchanged code (same output twice)
- Handles missing DB gracefully
- S3 integration includes twin data when map exists
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add memory directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary code_chunks.db with test data."""
    db_path = tmp_path / ".code_chunks.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE code_chunks (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            chunk_type TEXT,
            name TEXT,
            content TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            git_sha TEXT,
            embedding BLOB,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Insert test data: two modules with a dependency relationship
    conn.execute("""
        INSERT INTO code_chunks (id, file_path, chunk_type, name, content, start_line, end_line, git_sha)
        VALUES ('chunk1', 'memory/professor.py', 'function', 'ask_professor',
                'from memory.sqlite_storage import get_sqlite_storage\ndef ask_professor(query): pass',
                1, 5, 'abc123')
    """)
    conn.execute("""
        INSERT INTO code_chunks (id, file_path, chunk_type, name, content, start_line, end_line, git_sha)
        VALUES ('chunk2', 'memory/sqlite_storage.py', 'class', 'SQLiteStorage',
                'class SQLiteStorage:\n    def __init__(self): pass',
                1, 10, 'abc123')
    """)
    conn.execute("""
        INSERT INTO code_chunks (id, file_path, chunk_type, name, content, start_line, end_line, git_sha)
        VALUES ('chunk3', 'memory/professor.py', 'function', 'get_wisdom',
                'import memory.query\ndef get_wisdom(): pass',
                10, 15, 'abc123')
    """)
    conn.execute("""
        INSERT INTO code_chunks (id, file_path, chunk_type, name, content, start_line, end_line, git_sha)
        VALUES ('chunk4', 'memory/query.py', 'function', 'run_query',
                'def run_query(q): return []',
                1, 5, 'abc123')
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tmp_output(tmp_path):
    """Temporary output path for architecture.map.json."""
    return tmp_path / "architecture.map.json"


class TestRefreshTwin:
    """Test refresh_twin() function."""

    def test_produces_valid_json_schema(self, tmp_db, tmp_output):
        """refresh_twin() produces valid JSON with expected schema."""
        from memory.architecture_twin import refresh_twin

        stats = refresh_twin(db_path=tmp_db, output_path=tmp_output)

        # Verify stats
        assert stats["modules"] > 0
        assert stats["files"] > 0
        assert isinstance(stats["edges"], int)

        # Verify JSON file was written
        assert tmp_output.exists()
        data = json.loads(tmp_output.read_text())

        # Verify schema
        assert "generated_at" in data
        assert "modules" in data
        assert "edges" in data
        assert isinstance(data["modules"], dict)
        assert isinstance(data["edges"], list)

        # Verify module structure
        for mod_name, mod_data in data["modules"].items():
            assert "files" in mod_data
            assert "dependencies" in mod_data
            assert "dependents" in mod_data
            assert isinstance(mod_data["files"], list)
            assert isinstance(mod_data["dependencies"], list)
            assert isinstance(mod_data["dependents"], list)

        # Verify edge structure
        for edge in data["edges"]:
            assert "from" in edge
            assert "to" in edge

    def test_correct_dependency_extraction(self, tmp_db, tmp_output):
        """Verifies dependencies are correctly extracted from import statements."""
        from memory.architecture_twin import refresh_twin

        refresh_twin(db_path=tmp_db, output_path=tmp_output)
        data = json.loads(tmp_output.read_text())

        # memory.professor imports memory.sqlite_storage and memory.query
        professor = data["modules"].get("memory.professor")
        assert professor is not None
        assert "memory.sqlite_storage" in professor["dependencies"]
        assert "memory.query" in professor["dependencies"]

        # memory.sqlite_storage should be a dependent of memory.professor
        storage = data["modules"].get("memory.sqlite_storage")
        assert storage is not None
        assert "memory.professor" in storage["dependents"]

    def test_idempotent_on_unchanged_code(self, tmp_db, tmp_output):
        """Same DB content produces same output (minus timestamp)."""
        from memory.architecture_twin import refresh_twin

        stats1 = refresh_twin(db_path=tmp_db, output_path=tmp_output)
        data1 = json.loads(tmp_output.read_text())

        stats2 = refresh_twin(db_path=tmp_db, output_path=tmp_output)
        data2 = json.loads(tmp_output.read_text())

        # Stats should be identical
        assert stats1["modules"] == stats2["modules"]
        assert stats1["edges"] == stats2["edges"]
        assert stats1["files"] == stats2["files"]

        # Structure should be identical (timestamp will differ)
        assert data1["modules"] == data2["modules"]
        assert data1["edges"] == data2["edges"]

    def test_handles_missing_db(self, tmp_path):
        """Gracefully handles missing .code_chunks.db."""
        from memory.architecture_twin import refresh_twin

        missing_db = tmp_path / "nonexistent.db"
        output = tmp_path / "architecture.map.json"

        stats = refresh_twin(db_path=missing_db, output_path=output)

        # Should return empty stats
        assert stats["modules"] == 0
        assert stats["edges"] == 0
        assert stats["files"] == 0

        # Should still write a valid (empty) JSON file
        assert output.exists()
        data = json.loads(output.read_text())
        assert data["modules"] == {}
        assert data["edges"] == []
        assert "generated_at" in data

    def test_handles_empty_db(self, tmp_path):
        """Handles a code_chunks.db with no rows."""
        from memory.architecture_twin import refresh_twin

        db_path = tmp_path / ".code_chunks.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE code_chunks (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                chunk_type TEXT,
                name TEXT,
                content TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                git_sha TEXT,
                embedding BLOB,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        output = tmp_path / "architecture.map.json"
        stats = refresh_twin(db_path=db_path, output_path=output)

        assert stats["modules"] == 0
        assert stats["edges"] == 0
        assert stats["files"] == 0


class TestGetTwinSummary:
    """Test get_twin_summary() function."""

    def test_returns_summary_when_map_exists(self, tmp_db, tmp_output):
        """Returns module/edge/timestamp summary when map exists."""
        from memory.architecture_twin import refresh_twin, get_twin_summary

        refresh_twin(db_path=tmp_db, output_path=tmp_output)

        with patch("memory.architecture_twin.MAP_OUTPUT", tmp_output):
            summary = get_twin_summary()

        assert summary is not None
        assert "modules" in summary
        assert "edges" in summary
        assert "last_refresh" in summary
        assert summary["modules"] > 0

    def test_returns_none_when_no_map(self, tmp_path):
        """Returns None when architecture.map.json doesn't exist."""
        from memory.architecture_twin import get_twin_summary

        fake_path = tmp_path / "nonexistent.json"
        with patch("memory.architecture_twin.MAP_OUTPUT", fake_path):
            summary = get_twin_summary()

        assert summary is None


class TestS3Integration:
    """Test that S3 section includes twin data when map exists."""

    def test_s3_includes_twin_summary(self):
        """S3 generation includes twin summary line when map exists."""
        mock_summary = {"modules": 42, "edges": 78, "last_refresh": "2026-03-03T12:00:00+00:00"}

        with patch("memory.persistent_hook_structure.get_recent_git_changes", return_value=[]), \
             patch("memory.persistent_hook_structure.get_ripple_effects", return_value=[]), \
             patch("memory.persistent_hook_structure.get_previous_mistakes", return_value=[]), \
             patch("memory.persistent_hook_structure.get_architecture_topology", return_value=[]), \
             patch("memory.persistent_hook_structure.get_mansion_warnings", return_value=[]), \
             patch("memory.architecture_twin.get_twin_summary", return_value=mock_summary), \
             patch("memory.refresh_architecture_twin.get_structural_drift", return_value=None):

            from memory.persistent_hook_structure import generate_section_3, InjectionConfig, RiskLevel
            config = InjectionConfig()
            config.awareness_depth = "full"
            config.emoji_enabled = False

            result = generate_section_3("test prompt for architecture analysis", RiskLevel.HIGH, config)

            assert "Twin: 42 modules, 78 edges" in result
            assert "2026-03-03" in result

    def test_s3_skips_twin_when_no_map(self):
        """S3 generation skips twin line gracefully when no map."""
        with patch("memory.persistent_hook_structure.get_recent_git_changes", return_value=[]), \
             patch("memory.persistent_hook_structure.get_ripple_effects", return_value=[]), \
             patch("memory.persistent_hook_structure.get_previous_mistakes", return_value=[]), \
             patch("memory.persistent_hook_structure.get_architecture_topology", return_value=[]), \
             patch("memory.persistent_hook_structure.get_mansion_warnings", return_value=[]), \
             patch("memory.architecture_twin.get_twin_summary", return_value=None), \
             patch("memory.refresh_architecture_twin.get_structural_drift", return_value=None):

            from memory.persistent_hook_structure import generate_section_3, InjectionConfig, RiskLevel
            config = InjectionConfig()
            config.awareness_depth = "full"
            config.emoji_enabled = False

            result = generate_section_3("test prompt for basic check", RiskLevel.LOW, config)

            assert "Twin:" not in result


class TestImportExtraction:
    """Test internal import extraction helper."""

    def test_extract_from_import(self):
        """Extracts 'from memory.X import ...' patterns."""
        from memory.architecture_twin import _extract_imports_from_content

        content = "from memory.professor import ask_professor"
        imports = _extract_imports_from_content(content)
        assert "memory.professor" in imports

    def test_extract_import_statement(self):
        """Extracts 'import memory.X' patterns."""
        from memory.architecture_twin import _extract_imports_from_content

        content = "import memory.query"
        imports = _extract_imports_from_content(content)
        assert "memory.query" in imports

    def test_no_non_memory_imports(self):
        """Ignores non-memory imports."""
        from memory.architecture_twin import _extract_imports_from_content

        content = "import os\nfrom pathlib import Path\nimport json"
        imports = _extract_imports_from_content(content)
        assert imports == []

    def test_multiple_imports(self):
        """Extracts multiple import targets."""
        from memory.architecture_twin import _extract_imports_from_content

        content = "from memory.professor import X\nimport memory.query\nfrom memory.db_utils import connect_wal"
        imports = _extract_imports_from_content(content)
        assert "memory.professor" in imports
        assert "memory.query" in imports
        assert "memory.db_utils" in imports


class TestModulePathExtraction:
    """Test module name extraction from file paths."""

    def test_simple_path(self):
        """Converts 'memory/professor.py' -> 'memory.professor'."""
        from memory.architecture_twin import _extract_module_from_path

        assert _extract_module_from_path("memory/professor.py") == "memory.professor"

    def test_nested_path(self):
        """Converts nested paths correctly."""
        from memory.architecture_twin import _extract_module_from_path

        assert _extract_module_from_path("memory/agents/anticipation.py") == "memory.agents.anticipation"

    def test_single_file(self):
        """Converts single file path."""
        from memory.architecture_twin import _extract_module_from_path

        assert _extract_module_from_path("setup.py") == "setup"
