"""
Architecture Twin — Module-level dependency graph from code_chunks.db.

Reads .code_chunks.db (populated by code_chunk_indexer.py) and extracts
module names + dependency relationships. Writes architecture.map.json
to .projectdna/ with module/edge/file structure.

No LLM needed — pure DB + JSON. Safe for T4 BACKGROUND scheduling.

Usage:
    PYTHONPATH=. python memory/architecture_twin.py           # refresh
    PYTHONPATH=. python memory/architecture_twin.py --stats   # stats only
"""

import ast
import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("contextdna.arch_twin_db")

REPO_ROOT = Path(__file__).parent.parent
CHUNKS_DB = Path(os.path.expanduser("~/.context-dna/.code_chunks.db"))
MAP_OUTPUT = REPO_ROOT / ".projectdna" / "architecture.map.json"


def _connect_db(db_path: Path) -> sqlite3.Connection | None:
    """Connect to code_chunks.db with WAL mode. Returns None if missing."""
    if not db_path.exists():
        logger.warning(f"code_chunks.db not found at {db_path}")
        return None
    try:
        from memory.db_utils import connect_wal
        return connect_wal(db_path, row_factory=sqlite3.Row)
    except ImportError:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


def _extract_module_from_path(file_path: str) -> str:
    """Extract module name from file path.

    'memory/professor.py' -> 'memory.professor'
    'memory/agents/anticipation.py' -> 'memory.agents.anticipation'
    """
    p = Path(file_path)
    parts = list(p.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _extract_imports_from_content(content: str) -> list[str]:
    """Extract local import targets from chunk content using regex.

    Faster than AST for content fragments. Returns module-style names.
    """
    imports = set()
    # Match: from memory.X import ... or import memory.X
    for m in re.finditer(r'(?:from|import)\s+(memory(?:\.\w+)+)', content):
        imports.add(m.group(1))
    return sorted(imports)


def refresh_twin(
    db_path: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Refresh architecture.map.json from code_chunks.db.

    Reads code_chunks table, groups chunks by module (file_path),
    extracts dependency relationships from import statements in content,
    and writes a JSON structure with modules and edges.

    Args:
        db_path: Override path to .code_chunks.db (default: ~/.context-dna/.code_chunks.db)
        output_path: Override output path (default: .projectdna/architecture.map.json)

    Returns:
        Stats dict: {"modules": int, "edges": int, "files": int}
    """
    db = db_path or CHUNKS_DB
    out = output_path or MAP_OUTPUT

    conn = _connect_db(db)
    if conn is None:
        # Graceful handling: return empty map, write minimal JSON
        empty_map = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "modules": {},
            "edges": [],
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(empty_map, indent=2) + "\n")
        logger.warning("No code_chunks.db — wrote empty architecture map")
        return {"modules": 0, "edges": 0, "files": 0}

    try:
        # Query all chunks grouped by file
        rows = conn.execute(
            "SELECT file_path, chunk_type, name, content FROM code_chunks ORDER BY file_path, start_line"
        ).fetchall()

        # Group by module (file_path)
        modules: dict[str, dict] = {}
        file_set: set[str] = set()
        all_imports: dict[str, set[str]] = defaultdict(set)  # module -> set of imported modules

        for row in rows:
            fp = row["file_path"]
            file_set.add(fp)
            module_name = _extract_module_from_path(fp)

            if module_name not in modules:
                modules[module_name] = {
                    "files": [],
                    "dependencies": [],
                    "dependents": [],
                }

            # Track files per module (deduplicated)
            if fp not in modules[module_name]["files"]:
                modules[module_name]["files"].append(fp)

            # Extract imports from content
            content = row["content"] or ""
            for imp in _extract_imports_from_content(content):
                all_imports[module_name].add(imp)

        # Build edges from import relationships
        edges = []
        known_modules = set(modules.keys())

        for source_mod, imported_mods in all_imports.items():
            for target_mod in imported_mods:
                if target_mod in known_modules and target_mod != source_mod:
                    edge = {"from": source_mod, "to": target_mod}
                    if edge not in edges:
                        edges.append(edge)
                    # Populate dependency/dependent lists
                    if target_mod not in modules[source_mod]["dependencies"]:
                        modules[source_mod]["dependencies"].append(target_mod)
                    if source_mod not in modules[target_mod]["dependents"]:
                        modules[target_mod]["dependents"].append(source_mod)

        # Sort for determinism
        for mod_data in modules.values():
            mod_data["files"].sort()
            mod_data["dependencies"].sort()
            mod_data["dependents"].sort()
        edges.sort(key=lambda e: (e["from"], e["to"]))

        # Build output
        arch_map = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "modules": dict(sorted(modules.items())),
            "edges": edges,
        }

        # Write to .projectdna/
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(arch_map, indent=2) + "\n")

        stats = {
            "modules": len(modules),
            "edges": len(edges),
            "files": len(file_set),
        }
        logger.info(f"Architecture twin refreshed: {stats['modules']} modules, {stats['edges']} edges, {stats['files']} files")
        return stats

    finally:
        conn.close()


def get_twin_summary() -> dict[str, Any] | None:
    """Read architecture.map.json and return brief summary for S3 injection.

    Returns dict: {"modules": int, "edges": int, "last_refresh": str} or None.
    """
    if not MAP_OUTPUT.exists():
        return None
    try:
        data = json.loads(MAP_OUTPUT.read_text())
        modules = data.get("modules", {})
        edges = data.get("edges", [])
        generated_at = data.get("generated_at", "")
        return {
            "modules": len(modules),
            "edges": len(edges),
            "last_refresh": generated_at,
        }
    except Exception as e:
        logger.debug(f"Failed to read twin summary: {e}")
        return None


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if "--stats" in sys.argv:
        summary = get_twin_summary()
        if summary:
            print(f"Twin: {summary['modules']} modules, {summary['edges']} edges. Last refresh: {summary['last_refresh']}")
        else:
            print("No architecture.map.json found")
        sys.exit(0)

    stats = refresh_twin()
    print(f"Refreshed: {stats['modules']} modules, {stats['edges']} edges, {stats['files']} files")
