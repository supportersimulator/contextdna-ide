"""
Refresh Architecture Twin — Auto-generate architecture.current.md from code.

Scans the actual codebase + architecture.map.json to produce a truthful
snapshot of system state. Designed for scheduled or manual execution.

Features:
- Git-aware smart refresh: skips costly scan when no architecture-relevant files changed
- Content fingerprint: SHA256 hash stored in Redis detects actual structural drift
- Structural drift signal: S3 AWARENESS can query if architecture recently changed
- Auto-evolving map: discovers new modules via AST import analysis, adds to map additively

Usage:
    PYTHONPATH=. python memory/refresh_architecture_twin.py           # refresh
    PYTHONPATH=. python memory/refresh_architecture_twin.py --diff    # also generate diff
    PYTHONPATH=. python memory/refresh_architecture_twin.py --check   # check if refresh needed
    PYTHONPATH=. python memory/refresh_architecture_twin.py --evolve  # run map evolution only
"""

import ast
import copy
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("contextdna.arch_twin")

REPO_ROOT = Path(__file__).parent.parent
VAULT_DIR = REPO_ROOT / ".projectdna" / "derived"
MEMORY_DIR = REPO_ROOT / "memory"
ARCH_MAP_PATH = REPO_ROOT / ".projectdna" / "architecture.map.json"

# Redis keys for fingerprint tracking
REDIS_KEY_ARCH_HASH = "arch_twin:content_hash"
REDIS_KEY_ARCH_CHANGED = "arch_twin:last_changed"
REDIS_KEY_ARCH_REFRESHED = "arch_twin:last_refreshed"

# Architecture-relevant paths — changes to these trigger refresh
ARCH_RELEVANT_GLOBS = [
    "memory/*.py",
    "admin.contextdna.io/lib/ide/",
    "admin.contextdna.io/components/ide/",
    "ContextDNASupervisor/Sources/",
    "electron/",
    ".projectdna/architecture.map.json",
]

# Directories to skip when counting files
SKIP_DIRS = {
    "node_modules", ".venv", ".venv-mlx", "venv", "__pycache__",
    ".build", "DerivedData", ".git", ".expo", "build", "dist",
    ".next", ".swiftpm", "coverage",
}


def _get_redis():
    """Get Redis connection (best-effort, non-blocking)."""
    try:
        import redis
        return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=1)
    except Exception:
        return None


def _git_changed_since(since_timestamp: float) -> bool:
    """Check if architecture-relevant files had git changes since timestamp."""
    try:
        since_iso = datetime.fromtimestamp(since_timestamp).strftime("%Y-%m-%d %H:%M:%S")
        result = subprocess.run(
            ["git", "log", "--since", since_iso, "--name-only", "--pretty=format:", "--"]
            + ARCH_RELEVANT_GLOBS,
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        changed_files = [f for f in result.stdout.strip().split("\n") if f.strip()]
        return len(changed_files) > 0
    except Exception:
        return True  # Assume changed if we can't check


def should_refresh() -> bool:
    """Check if architecture twin needs refresh based on git changes.

    Returns True if:
    - Redis unavailable (safe fallback)
    - Never refreshed before
    - Git shows architecture-relevant changes since last refresh
    """
    r = _get_redis()
    if not r:
        return True

    try:
        last_refreshed = r.get(REDIS_KEY_ARCH_REFRESHED)
        if not last_refreshed:
            return True
        return _git_changed_since(float(last_refreshed))
    except Exception:
        return True


def get_structural_drift() -> dict | None:
    """Check if architecture structure recently changed.

    Returns dict with drift info if changed within last hour, else None.
    Used by S3 AWARENESS to signal structural changes to Atlas.
    Includes what specifically changed (not just that it changed).
    """
    r = _get_redis()
    if not r:
        return None

    try:
        last_changed = r.get(REDIS_KEY_ARCH_CHANGED)
        if not last_changed:
            return None
        changed_ts = float(last_changed)
        age_s = datetime.now().timestamp() - changed_ts
        if age_s > 3600:  # Only signal within 1 hour
            return None
        content_hash = r.get(REDIS_KEY_ARCH_HASH) or "?"
        details = r.get("arch_twin:change_details")
        result = {
            "changed_ago_min": int(age_s / 60),
            "content_hash": content_hash,
        }
        if details:
            try:
                result["details"] = json.loads(details)
            except Exception:
                pass
        return result
    except Exception:
        return None


def _compute_change_details(prev_snapshot: dict | None, current_snapshot: dict) -> list[str]:
    """Compare two architecture snapshots and describe what changed.

    Returns list of human-readable change descriptions for S3 injection.
    """
    if not prev_snapshot:
        return ["Initial architecture snapshot established"]

    changes = []
    # Compare file counts
    for key in ["py_mem_count", "ts_count", "swift_count", "db_count"]:
        prev = prev_snapshot.get(key, 0)
        curr = current_snapshot.get(key, 0)
        delta = curr - prev
        if delta != 0:
            labels = {
                "py_mem_count": "Python files in memory/",
                "ts_count": "TS/TSX files in admin.contextdna.io/",
                "swift_count": "Swift files in ContextDNASupervisor/",
                "db_count": "SQLite databases",
            }
            label = labels.get(key, key)
            sign = "+" if delta > 0 else ""
            changes.append(f"{sign}{delta} {label} ({prev}→{curr})")

    # Compare node/edge counts
    for key, label in [("node_count", "architecture nodes"), ("edge_count", "graph edges")]:
        prev = prev_snapshot.get(key, 0)
        curr = current_snapshot.get(key, 0)
        delta = curr - prev
        if delta != 0:
            sign = "+" if delta > 0 else ""
            changes.append(f"{sign}{delta} {label}")

    # Compare hub scores (top 3)
    prev_hubs = {h["name"]: h["degree"] for h in prev_snapshot.get("top_hubs", [])}
    curr_hubs = {h["name"]: h["degree"] for h in current_snapshot.get("top_hubs", [])}
    for name in set(list(prev_hubs.keys())[:3] + list(curr_hubs.keys())[:3]):
        p = prev_hubs.get(name, 0)
        c = curr_hubs.get(name, 0)
        if p != c:
            changes.append(f"{name} connectivity: {p}→{c}")

    return changes if changes else ["Minor structural changes (no major shifts)"]


def _count_files(directory: Path, extensions: set, skip_dirs: set = SKIP_DIRS) -> tuple:
    """Count files and LOC in a directory. Returns (file_count, loc)."""
    count = 0
    loc = 0
    if not directory.exists():
        return 0, 0
    for f in directory.rglob("*"):
        if any(s in f.parts for s in skip_dirs):
            continue
        if f.suffix in extensions and f.is_file():
            count += 1
            try:
                loc += sum(1 for _ in f.open(errors="replace"))
            except Exception:
                pass
    return count, loc


def _find_databases(directory: Path) -> list:
    """Find all SQLite databases in a directory."""
    if not directory.exists():
        return []
    seen = set()
    dbs = []
    for f in directory.glob("*"):
        if not f.is_file() or not f.name.endswith(".db"):
            continue
        if f.name.endswith(("-wal", "-shm")) or ".bak" in f.name:
            continue
        if f.name in seen:
            continue
        seen.add(f.name)
        size_kb = f.stat().st_size / 1024
        dbs.append({"name": f.name, "size_kb": size_kb})
    # Also get hidden .db files
    for f in directory.glob(".*"):
        if not f.is_file() or not f.name.endswith(".db"):
            continue
        if f.name.endswith(("-wal", "-shm")) or ".bak" in f.name:
            continue
        if f.name in seen:
            continue
        seen.add(f.name)
        size_kb = f.stat().st_size / 1024
        dbs.append({"name": f.name, "size_kb": size_kb})
    return sorted(dbs, key=lambda d: d["size_kb"], reverse=True)


def _load_arch_map() -> dict:
    """Load architecture.map.json."""
    if not ARCH_MAP_PATH.exists():
        return {"nodes": [], "edges": []}
    with open(ARCH_MAP_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Auto-Evolution: Discover new modules via import analysis, add to map
# ---------------------------------------------------------------------------

# Kind inference patterns — checked against file content
_KIND_PATTERNS = [
    ("service", [r"FastAPI|Flask|uvicorn|@app\.(route|get|post|put|delete)", r"def main\(\).*serve"]),
    ("store", [r"sqlite3\.connect|connect_wal|CREATE TABLE|\.execute\(", r"class \w+Store"]),
    ("engine", [r"class \w+Engine|class \w+Analyzer|class \w+Evaluator|class \w+Chain"]),
    ("entrypoint", [r"if __name__\s*==\s*['\"]__main__['\"]"]),
]

# Directories to skip during import discovery
_EVOLVE_SKIP_DIRS = {"__pycache__", ".venv", ".venv-mlx", "venv", "agents", "code_parser", "major_skills", "providers", "ide_adapters"}
_EVOLVE_SKIP_PREFIXES = ("test_", "seed_", "migrate_", "enable_")

# Minimum file size to consider architecturally significant (bytes)
_MIN_FILE_SIZE = 500


def _extract_imports(file_path: Path) -> list[str]:
    """Extract imported module paths from a Python file using AST.

    Returns list of relative paths like 'memory/foo.py' for local imports.
    """
    try:
        source = file_path.read_text(errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            # Convert dotted module path to file path
            # e.g., 'memory.professor' → 'memory/professor.py'
            parts = node.module.split(".")
            if parts[0] == "memory" and len(parts) >= 2:
                rel = "/".join(parts) + ".py"
                candidate = REPO_ROOT / rel
                if candidate.exists():
                    imports.append(rel)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "memory" and len(parts) >= 2:
                    rel = "/".join(parts) + ".py"
                    candidate = REPO_ROOT / rel
                    if candidate.exists():
                        imports.append(rel)
    return imports


def _infer_kind(file_path: Path) -> str:
    """Infer the architecture map 'kind' from file content patterns."""
    try:
        content = file_path.read_text(errors="replace")
    except Exception:
        return "utility"

    for kind, patterns in _KIND_PATTERNS:
        for pat in patterns:
            if re.search(pat, content):
                return kind
    return "utility"


def _infer_name(file_path: Path) -> str:
    """Infer a human-readable name from file path.

    'memory/anticipation_engine.py' → 'Anticipation Engine'
    """
    stem = file_path.stem
    return stem.replace("_", " ").title()


def _infer_metadata(file_path: Path, kind: str) -> dict:
    """Infer basic metadata from file content."""
    meta = {}
    try:
        content = file_path.read_text(errors="replace")
        lines = content.split("\n")
        meta["lines"] = len(lines)

        # Extract role from module docstring
        try:
            tree = ast.parse(content)
            docstring = ast.get_docstring(tree)
            if docstring:
                # Use first sentence as role hint
                first_line = docstring.strip().split("\n")[0].strip()
                if len(first_line) < 80:
                    meta["role"] = first_line.rstrip(".")
        except Exception:
            pass
    except Exception:
        pass
    return meta


def _build_mapped_file_index(arch: dict) -> dict[str, str]:
    """Build index: file_path → node_id for all mapped files."""
    index = {}
    for node in arch.get("nodes", []):
        for fp in node.get("filePaths", []):
            index[fp] = node["id"]
    return index


def evolve_architecture_map(dry_run: bool = False) -> dict:
    """Auto-evolve architecture.map.json by discovering new modules.

    Strategy: Find modules imported by already-mapped modules that aren't
    in the map yet. This grows the map organically from its core.

    Rules:
    - Additive only: never removes nodes or edges
    - Backup before write
    - Deterministic: same code → same additions
    - Signals changes via Redis

    Returns dict with evolution results.
    """
    arch = _load_arch_map()
    file_index = _build_mapped_file_index(arch)
    existing_node_ids = {n["id"] for n in arch.get("nodes", [])}
    existing_edges = {(e["from"], e["to"]) for e in arch.get("edges", [])}

    new_nodes = []
    new_edges = []
    stale_nodes = []

    # Phase 1: Discover imports from mapped modules
    discovered_deps = {}  # file_path → set of importer node_ids
    for node in arch.get("nodes", []):
        for fp in node.get("filePaths", []):
            abs_path = REPO_ROOT / fp
            if not abs_path.exists() or not fp.endswith(".py"):
                continue
            imports = _extract_imports(abs_path)
            for imp in imports:
                if imp not in file_index:
                    # This import target is not yet in the map
                    discovered_deps.setdefault(imp, set()).add(node["id"])

    # Phase 2: Filter and add new modules
    for dep_path, importer_ids in discovered_deps.items():
        abs_path = REPO_ROOT / dep_path
        if not abs_path.exists():
            continue

        # Skip small files, seeds, migrations, tests
        try:
            if abs_path.stat().st_size < _MIN_FILE_SIZE:
                continue
        except OSError:
            continue

        parts = Path(dep_path).parts
        if any(d in parts for d in _EVOLVE_SKIP_DIRS):
            continue
        if any(Path(dep_path).name.startswith(p) for p in _EVOLVE_SKIP_PREFIXES):
            continue

        # Skip __init__.py
        if Path(dep_path).name == "__init__.py":
            continue

        # Infer properties
        kind = _infer_kind(abs_path)
        name = _infer_name(abs_path)
        metadata = _infer_metadata(abs_path, kind)
        metadata["auto_discovered"] = True

        node_id = dep_path
        new_node = {
            "id": node_id,
            "kind": kind,
            "name": name,
            "filePaths": [dep_path],
            "metadata": metadata,
        }
        new_nodes.append(new_node)

        # Add edges from importers to this new node (deduplicated)
        for importer_id in importer_ids:
            edge_key = (importer_id, node_id)
            if edge_key not in existing_edges:
                # Check not already added (handles multi-file nodes importing same target)
                if not any(e["from"] == importer_id and e["to"] == node_id for e in new_edges):
                    new_edges.append({
                        "from": importer_id,
                        "to": node_id,
                        "relation": "imports",
                    })

    # Phase 3: Discover inter-module edges between already-mapped modules
    # Track all added edges (including from Phase 2) to prevent duplicates
    added_edge_keys = {(e["from"], e["to"]) for e in new_edges}
    for node in arch.get("nodes", []):
        for fp in node.get("filePaths", []):
            abs_path = REPO_ROOT / fp
            if not abs_path.exists() or not fp.endswith(".py"):
                continue
            imports = _extract_imports(abs_path)
            for imp in imports:
                target_id = file_index.get(imp)
                if target_id and target_id != node["id"]:
                    edge_key = (node["id"], target_id)
                    if edge_key not in existing_edges and edge_key not in added_edge_keys:
                        new_edges.append({
                            "from": node["id"],
                            "to": target_id,
                            "relation": "imports",
                        })
                        added_edge_keys.add(edge_key)

    # Phase 4: Detect stale nodes (files that no longer exist)
    for node in arch.get("nodes", []):
        file_paths = node.get("filePaths", [])
        if not file_paths:
            continue
        # Only flag if ALL file paths are gone (some nodes have directories)
        all_gone = all(
            not (REPO_ROOT / fp).exists()
            for fp in file_paths
            if not fp.endswith("/")  # Skip directory entries
        )
        file_entries = [fp for fp in file_paths if not fp.endswith("/")]
        if file_entries and all_gone:
            stale_nodes.append(node["id"])

    result = {
        "new_nodes": len(new_nodes),
        "new_edges": len(new_edges),
        "stale_nodes": stale_nodes,
        "dry_run": dry_run,
        "details": {
            "added_nodes": [n["id"] for n in new_nodes],
            "added_edges": [(e["from"], e["to"]) for e in new_edges],
        },
    }

    if dry_run or (not new_nodes and not new_edges):
        return result

    # Phase 5: Write updated map (additive only)
    # Backup first
    bak_path = ARCH_MAP_PATH.parent / (ARCH_MAP_PATH.name + ".bak")
    try:
        shutil.copy2(ARCH_MAP_PATH, bak_path)
    except Exception as e:
        logger.warning(f"Backup failed: {e}")

    updated = copy.deepcopy(arch)
    updated["nodes"].extend(new_nodes)
    updated["edges"].extend(new_edges)
    updated["generated"] = datetime.now().strftime("%Y-%m-%d")

    with open(ARCH_MAP_PATH, "w") as f:
        json.dump(updated, f, indent=2)
        f.write("\n")

    # Signal evolution via Redis
    r = _get_redis()
    if r:
        try:
            summary = {
                "timestamp": datetime.now().isoformat(),
                "new_nodes": [n["id"] for n in new_nodes],
                "new_edges": len(new_edges),
                "stale_nodes": stale_nodes,
            }
            r.set("arch_twin:auto_evolved", json.dumps(summary), ex=3600)
        except Exception:
            pass

    logger.info(f"Architecture map evolved: +{len(new_nodes)} nodes, +{len(new_edges)} edges")
    return result


def _detect_ports() -> list:
    """Detect port assignments from code and config."""
    ports = []
    # Known ports from architecture map
    arch = _load_arch_map()
    for node in arch.get("nodes", []):
        for port in node.get("ports", []):
            ports.append({
                "port": port,
                "service": node["name"],
                "source": "architecture.map.json",
            })
    return sorted(ports, key=lambda p: p["port"])


def _detect_services(arch: dict) -> list:
    """Extract service nodes from architecture map."""
    services = []
    for node in arch.get("nodes", []):
        if node.get("kind") in ("service", "infrastructure"):
            services.append({
                "name": node["name"],
                "kind": node["kind"],
                "ports": node.get("ports", []),
                "role": node.get("metadata", {}).get("role", ""),
                "files": node.get("filePaths", []),
            })
    return services


def _compute_hub_scores(arch: dict) -> list:
    """Compute connectivity scores for all nodes."""
    edges = arch.get("edges", [])
    degree = Counter()
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    nodes = {n["id"]: n for n in arch.get("nodes", [])}
    hubs = []
    for nid, count in degree.most_common():
        node = nodes.get(nid, {})
        hubs.append({
            "id": nid,
            "name": node.get("name", nid),
            "kind": node.get("kind", "?"),
            "degree": count,
        })
    return hubs


def _classify_databases(dbs: list) -> dict:
    """Classify databases into tiers."""
    tier1_names = {
        "learnings.db", ".observability.db", "session_gold_archive.db",
        ".session_archive.db",
    }
    tier2_names = {
        ".context-dna.db", ".dialogue_mirror.db", ".pattern_evolution.db",
        ".work_log.db", "pattern_evolution.db",
    }

    tiers = {"tier1": [], "tier2": [], "tier3": []}
    for db in dbs:
        name = db["name"]
        if name in tier1_names:
            tiers["tier1"].append(db)
        elif name in tier2_names:
            tiers["tier2"].append(db)
        else:
            tiers["tier3"].append(db)
    return tiers


def _generate_tech_debt(arch: dict, db_count: int) -> list:
    """Generate known technical debt items from code analysis."""
    debts = []

    # Count raw sqlite3.connect calls
    raw_conn_count = 0
    for f in MEMORY_DIR.rglob("*.py"):
        try:
            text = f.read_text(errors="replace")
            raw_conn_count += len(re.findall(r"sqlite3\.connect\(", text))
        except Exception:
            pass
    if raw_conn_count > 50:
        debts.append(f"**{raw_conn_count} raw sqlite3.connect calls** — WAL-safe but bypass audit trail")

    if db_count > 25:
        debts.append(f"**{db_count} SQLite databases** — Many feature-specific, could consolidate")

    # Check for known issues
    debts.append("**agent_service FD leak** — PID accumulates 50+ FDs on .observability.db")
    debts.append("**Celery broken** — Lite scheduler workaround, 39 jobs in-process")

    return debts


def generate_architecture_current() -> str:
    """Generate architecture.current.md from code analysis."""
    now = datetime.now().strftime("%Y-%m-%d")

    # Gather data
    arch = _load_arch_map()
    py_mem_count, py_mem_loc = _count_files(MEMORY_DIR, {".py"})
    py_other_count, _ = _count_files(REPO_ROOT / "context-dna", {".py"})
    ts_count, ts_loc = _count_files(REPO_ROOT / "admin.contextdna.io", {".ts", ".tsx"})
    swift_count, _ = _count_files(REPO_ROOT / "ContextDNASupervisor", {".swift"})
    dbs = _find_databases(MEMORY_DIR)
    db_tiers = _classify_databases(dbs)
    ports = _detect_ports()
    services = _detect_services(arch)
    hubs = _compute_hub_scores(arch)
    debts = _generate_tech_debt(arch, len(dbs))

    nodes = arch.get("nodes", [])
    edges = arch.get("edges", [])

    # --- Build document ---
    lines = [
        "# Architecture — er-simulator-superrepo",
        "> Auto-generated from code analysis. Citation: CODE.",
        f"> Last refreshed: {now}. Confidence: high.",
        "> Patch-only updates. Do not overwrite — append diffs.",
        "",
        "## System Overview",
        "",
        "Monorepo housing ContextDNA (AI knowledge OS), Atlas (navigator agent), "
        "Synaptic (8th intelligence), ER Simulator (medical training), and supporting infrastructure.",
        "",
        f"- **{py_mem_count} Python files** in memory/ ({py_mem_loc // 1000}K LOC)",
        f"- **{ts_count} TS/TSX files** in admin.contextdna.io/ ({ts_loc // 1000}K LOC)" if ts_count else None,
        f"- **{swift_count} Swift files** in ContextDNASupervisor/" if swift_count else None,
        f"- **{len(dbs)} SQLite databases** across memory/",
        f"- **{len(nodes)} architecture nodes**, **{len(edges)} edges** mapped",
        f"- **{len(ports)} ports** in active use",
        "",
    ]
    lines = [l for l in lines if l is not None]

    # Core Services table
    lines.extend([
        "## Core Services (Runtime)",
        "",
        "| Service | Port | Entry Point | Role |",
        "|---------|------|-------------|------|",
    ])
    for s in services:
        port_str = ", ".join(str(p) for p in s["ports"]) if s["ports"] else "—"
        files_str = ", ".join(s["files"][:2]) if s["files"] else "—"
        lines.append(f"| {s['name']} | {port_str} | {files_str} | {s['role']} |")
    lines.append("")

    # Hub Analysis
    top_hubs = [h for h in hubs if h["degree"] >= 4]
    if top_hubs:
        lines.extend([
            "## System Hubs (high connectivity — changes cascade)",
            "",
        ])
        for h in top_hubs[:8]:
            lines.append(f"- **{h['name']}** [{h['kind']}] — {h['degree']} connections")
        lines.append("")

    # Module Map — grouped by node kind
    kind_groups = {}
    for node in nodes:
        kind = node.get("kind", "other")
        kind_groups.setdefault(kind, []).append(node)

    lines.extend(["## Module Map", ""])

    kind_labels = {
        "engine": "Engines (processing logic)",
        "store": "Stores (persistent state)",
        "service": "Services (runtime processes)",
        "utility": "Utilities",
        "entrypoint": "Entry Points",
        "ide-core": "IDE Core (nervous system)",
        "ide-shell": "IDE Shell",
        "ide-ipc": "IDE IPC Bridges",
        "ide-panel": "IDE Panels",
        "infrastructure": "Infrastructure",
        "application": "Applications",
    }

    for kind in ["store", "engine", "service", "entrypoint", "utility",
                  "ide-core", "ide-shell", "ide-panel", "infrastructure", "application"]:
        group = kind_groups.get(kind, [])
        if not group:
            continue
        label = kind_labels.get(kind, kind.title())
        lines.append(f"### {label}")
        lines.append("")
        for node in sorted(group, key=lambda n: n["name"]):
            name = node["name"]
            role = node.get("metadata", {}).get("role", "")
            files = ", ".join(node.get("filePaths", [])[:2])
            meta_parts = []
            if role:
                meta_parts.append(role)
            # Add select metadata
            md = node.get("metadata", {})
            for k in ["endpoints", "passes", "tiers", "tools", "services",
                       "databases", "sections", "profiles", "lines", "panels"]:
                if k in md:
                    val = md[k]
                    meta_parts.append(f"{k}={val}" if not isinstance(val, list) else f"{k}={len(val)}")
            meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"- **{name}**{meta_str} — `{files}`")
        lines.append("")

    # Database Inventory
    lines.extend(["## Database Inventory", ""])
    for tier_name, tier_label in [("tier1", "Tier 1 — Core (high-write)"),
                                   ("tier2", "Tier 2 — Operational"),
                                   ("tier3", "Tier 3 — Feature-specific")]:
        tier_dbs = db_tiers[tier_name]
        if not tier_dbs:
            continue
        lines.append(f"### {tier_label}")
        lines.append("")
        for db in tier_dbs[:15]:
            size_str = f"{db['size_kb']:.0f}KB" if db["size_kb"] < 1024 else f"{db['size_kb']/1024:.1f}MB"
            lines.append(f"- `{db['name']}` ({size_str})")
        if len(tier_dbs) > 15:
            lines.append(f"- ... and {len(tier_dbs) - 15} more")
        lines.append("")

    # Data Flow
    lines.extend([
        "## Data Flow (Current)",
        "",
        "```",
        "User types in IDE",
        "  → Webhook fires (auto-memory-query.sh)",
        "  → Dedup guard (/tmp/.context-dna-hook-dedup)",
        "  → agent_service.py receives prompt",
        "  → persistent_hook_structure.py generates S0-S8",
        "  → S2: professor.py queries wisdom (Redis cache, <100ms)",
        "  → S3: architecture topology + ripple effects + mansion warnings",
        "  → S8: Synaptic voice (Redis cache, always present)",
        "  → Payload assembled, validated for determinism",
        "  → Injected into LLM context",
        "  → LLM responds with enriched understanding",
        "  → Session historian captures events (2min fast + 15min full)",
        "  → Gold mining extracts learnings (4 passes × 3min cycles)",
        "  → Evidence evaluator grades claims",
        "  → Graded wisdom feeds back into S2 for next injection",
        "```",
        "",
    ])

    # Technical Debt
    if debts:
        lines.extend(["## Known Technical Debt", ""])
        for i, debt in enumerate(debts, 1):
            lines.append(f"{i}. {debt}")
        lines.append("")

    return "\n".join(lines)


def generate_architecture_diff() -> str:
    """Generate architecture.diff.md — gaps between current and planned."""
    planned_path = VAULT_DIR / "architecture.planned.md"
    current_path = VAULT_DIR / "architecture.current.md"

    if not planned_path.exists() or not current_path.exists():
        return ""

    now = datetime.now().strftime("%Y-%m-%d")
    arch = _load_arch_map()
    nodes_by_id = {n["id"]: n for n in arch.get("nodes", [])}

    # Known gaps — these could be more sophisticated with AST analysis
    # For now, derive from the architecture map + filesystem checks
    gaps = []

    # Check for BridgeServer.swift
    bridge_path = REPO_ROOT / "ContextDNASupervisor" / "Sources" / "BridgeServer.swift"
    if not bridge_path.exists():
        gaps.append("**BridgeServer.swift** — Supervisor HTTP bridge (port 9090) NOT BUILT")

    # Check for MCP server
    mcp_server = REPO_ROOT / "memory" / "mcp_server.py"
    if not mcp_server.exists():
        gaps.append("**MCP Server** — .projectdna operations server NOT BUILT")

    # Check for mode_switch.py
    mode_switch = REPO_ROOT / "memory" / "mode_switch.py"
    if not mode_switch.exists():
        gaps.append("**mode_switch.py** — Formal 8-stage mode migration NOT BUILT")

    # Check for self_reference_filter
    self_ref = REPO_ROOT / "memory" / "self_reference_filter.py"
    if not self_ref.exists():
        gaps.append("**self_reference_filter.py** — Product mode self-reference suppression NOT BUILT")

    # Check for LSP bridge
    lsp_files = list((REPO_ROOT / "admin.contextdna.io").rglob("*lsp*"))
    if not lsp_files:
        gaps.append("**LSP Bridge** — IDE autocomplete, diagnostics, go-to-definition NOT BUILT")

    if not gaps:
        return ""

    lines = [
        "# Architecture Diff — Current vs Planned",
        f"> Auto-generated: {now}",
        "",
        "## Gaps (Planned but NOT BUILT)",
        "",
    ]
    for gap in gaps:
        lines.append(f"- {gap}")
    lines.append("")

    return "\n".join(lines)


def refresh(generate_diff: bool = False, force: bool = False) -> dict:
    """Refresh the architecture twin files. Returns stats.

    Args:
        generate_diff: Also generate architecture.diff.md
        force: Skip should_refresh() check and always regenerate
    """
    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    # Smart skip: if no architecture-relevant changes, return early
    if not force and not should_refresh():
        return {"skipped": True, "reason": "no_changes"}

    # Auto-evolve the map BEFORE generating current.md
    # This ensures new discoveries appear in the generated snapshot
    try:
        evo = evolve_architecture_map()
        if evo.get("new_nodes") or evo.get("new_edges"):
            logger.info(f"Map evolved: +{evo['new_nodes']} nodes, +{evo['new_edges']} edges")
    except Exception as e:
        logger.warning(f"Map evolution failed (non-fatal): {e}")
        evo = {}

    # Gather snapshot data (used for both generation and change detection)
    arch = _load_arch_map()
    py_mem_count, py_mem_loc = _count_files(MEMORY_DIR, {".py"})
    ts_count, ts_loc = _count_files(REPO_ROOT / "admin.contextdna.io", {".ts", ".tsx"})
    swift_count, _ = _count_files(REPO_ROOT / "ContextDNASupervisor", {".swift"})
    dbs = _find_databases(MEMORY_DIR)
    hubs = _compute_hub_scores(arch)

    current_snapshot = {
        "py_mem_count": py_mem_count,
        "ts_count": ts_count,
        "swift_count": swift_count,
        "db_count": len(dbs),
        "node_count": len(arch.get("nodes", [])),
        "edge_count": len(arch.get("edges", [])),
        "top_hubs": hubs[:5],
    }

    # Generate current architecture doc
    current_md = generate_architecture_current()
    current_path = VAULT_DIR / "architecture.current.md"
    current_path.write_text(current_md, encoding="utf-8")

    # Compute content fingerprint
    content_hash = hashlib.sha256(current_md.encode()).hexdigest()[:16]

    result = {
        "current_path": str(current_path),
        "current_size": len(current_md),
        "content_hash": content_hash,
        "structure_changed": False,
    }
    if evo.get("new_nodes") or evo.get("new_edges"):
        result["evolution"] = evo

    # Store fingerprint in Redis + detect drift with change details
    r = _get_redis()
    if r:
        try:
            prev_hash = r.get(REDIS_KEY_ARCH_HASH)
            prev_snapshot_raw = r.get("arch_twin:snapshot")
            now_ts = str(datetime.now().timestamp())
            r.set(REDIS_KEY_ARCH_REFRESHED, now_ts)
            r.set(REDIS_KEY_ARCH_HASH, content_hash)
            r.set("arch_twin:snapshot", json.dumps(current_snapshot))
            if prev_hash and prev_hash != content_hash:
                r.set(REDIS_KEY_ARCH_CHANGED, now_ts)
                result["structure_changed"] = True
                # Compute what specifically changed
                prev_snapshot = json.loads(prev_snapshot_raw) if prev_snapshot_raw else None
                change_details = _compute_change_details(prev_snapshot, current_snapshot)
                r.set("arch_twin:change_details", json.dumps(change_details))
                result["change_details"] = change_details
                logger.info(f"Architecture structure changed: {prev_hash} → {content_hash} ({change_details})")
        except Exception as e:
            logger.debug(f"Redis fingerprint store failed: {e}")

    # Optionally generate diff
    if generate_diff:
        diff_md = generate_architecture_diff()
        if diff_md:
            diff_path = VAULT_DIR / "architecture.diff.md"
            diff_path.write_text(diff_md, encoding="utf-8")
            result["diff_path"] = str(diff_path)
            result["diff_size"] = len(diff_md)

    return result


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        needed = should_refresh()
        print(f"Refresh needed: {needed}")
        drift = get_structural_drift()
        if drift:
            print(f"Structural drift: changed {drift['changed_ago_min']}min ago (hash: {drift['content_hash']})")
        else:
            print("No recent structural drift")
        sys.exit(0 if not needed else 1)

    if "--evolve" in sys.argv:
        dry = "--dry-run" in sys.argv
        evo = evolve_architecture_map(dry_run=dry)
        prefix = "[DRY RUN] " if dry else ""
        print(f"{prefix}Evolution: +{evo['new_nodes']} nodes, +{evo['new_edges']} edges")
        if evo.get("stale_nodes"):
            print(f"  Stale (not removed): {evo['stale_nodes']}")
        if evo["details"]["added_nodes"]:
            print(f"  Added nodes:")
            for n in evo["details"]["added_nodes"]:
                print(f"    + {n}")
        if evo["details"]["added_edges"]:
            print(f"  Added edges:")
            for src, tgt in evo["details"]["added_edges"]:
                print(f"    {src} → {tgt}")
        sys.exit(0)

    generate_diff = "--diff" in sys.argv
    force = "--force" in sys.argv
    result = refresh(generate_diff=generate_diff, force=force)

    if result.get("skipped"):
        print(f"Skipped: {result['reason']}")
    else:
        changed = " [STRUCTURE CHANGED]" if result.get("structure_changed") else ""
        print(f"Refreshed architecture.current.md ({result['current_size']:,} chars, hash: {result['content_hash']}){changed}")
        if result.get("evolution"):
            evo = result["evolution"]
            print(f"  Map evolved: +{evo['new_nodes']} nodes, +{evo['new_edges']} edges")
        if "diff_path" in result:
            print(f"Generated architecture.diff.md ({result['diff_size']:,} chars)")
