"""
Codebase Map — Architecture Wake-Up Summary for Atlas

Wraps the existing ArchitectureGraphBuilder to produce compact text
summaries of file dependencies, hot files, and recent changes.
Injected into Section 4 (DEEP_CONTEXT) so Atlas wakes up oriented.

Usage:
    python memory/codebase_map.py summary          # Wake-up text
    python memory/codebase_map.py hot               # Top 15 central files
    python memory/codebase_map.py deps <file>       # What depends on this
    python memory/codebase_map.py impact <file>     # Transitive impact radius
    python memory/codebase_map.py changes           # Changes since last commit
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

# Ensure imports work from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from memory.code_parser.models import ArchGraph, ArchNode, ArchEdge, EdgeType, NodeType
from memory.code_parser.graph_builder import ArchitectureGraphBuilder

# Module-level cache
_graph_cache: Optional[ArchGraph] = None
_builder_cache: Optional[ArchitectureGraphBuilder] = None


_EXTRA_EXCLUDES = [
    "**/.venv/**",
    "**/.venv-*/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.git/**",
    "**/build/**",
    "**/dist/**",
    "**/.next/**",
    "**/coverage/**",
    "**/*.min.js",
    "**/*.test.*",
    "**/*.spec.*",
    "**/test_*.py",
    "**/*_test.py",
    "**/acontext/**",
    "**/context-dna-data/**",
    "**/context-dna-data-OLD/**",
    "**/github-external/**",
]


def _get_builder() -> ArchitectureGraphBuilder:
    global _builder_cache
    if _builder_cache is None:
        _builder_cache = ArchitectureGraphBuilder(
            str(_REPO_ROOT),
            exclude_patterns=_EXTRA_EXCLUDES,
        )
    return _builder_cache


def _get_graph(force_rebuild: bool = False) -> ArchGraph:
    global _graph_cache
    if _graph_cache is None or force_rebuild:
        builder = _get_builder()
        _graph_cache = builder.build_graph(force_rebuild=force_rebuild)
    return _graph_cache


def _build_module_to_file_map(graph: ArchGraph) -> Dict[str, str]:
    """Map module names to file node IDs for internal imports.

    E.g., 'memory.sqlite_storage' → node_id of memory/sqlite_storage.py
    """
    # Build file_path → node_id map
    path_to_id: Dict[str, str] = {}
    for n in graph.nodes:
        if n.type == NodeType.FILE:
            path_to_id[n.file_path] = n.id

    # Build module_name → node_id map using common patterns
    mod_to_id: Dict[str, str] = {}
    for fpath, nid in path_to_id.items():
        # Python: memory/sqlite_storage.py → memory.sqlite_storage
        if fpath.endswith(".py"):
            mod = fpath[:-3].replace("/", ".")
            mod_to_id[mod] = nid
            # Also register the basename (e.g., "sqlite_storage")
            base = fpath.rsplit("/", 1)[-1][:-3]
            if base not in mod_to_id:
                mod_to_id[base] = nid
        # JS/TS: components/Foo.tsx → Foo, ./Foo, @/components/Foo
        elif fpath.endswith((".ts", ".tsx", ".js", ".jsx")):
            base = fpath.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if base == "index":
                # Use parent dir name
                parts = fpath.rsplit("/", 2)
                if len(parts) >= 2:
                    base = parts[-2]
            if base not in mod_to_id:
                mod_to_id[base] = nid

    return mod_to_id


def _compute_in_degree(graph: ArchGraph) -> Dict[str, int]:
    """Count how many files import each file (via module name metadata)."""
    mod_map = _build_module_to_file_map(graph)
    in_deg: Dict[str, int] = defaultdict(int)

    for e in graph.edges:
        if e.type == EdgeType.IMPORTS:
            module = e.metadata.get("module", "")
            if not module:
                continue
            # Try exact match first, then basename
            target_id = mod_map.get(module)
            if not target_id:
                # Try last segment: memory.sqlite_storage → sqlite_storage
                basename = module.rsplit(".", 1)[-1]
                target_id = mod_map.get(basename)
            if target_id:
                in_deg[target_id] += 1

    return dict(in_deg)


def _build_reverse_imports(graph: ArchGraph) -> Dict[str, Set[str]]:
    """Build reverse import adjacency: target_file_id → set of source file IDs."""
    mod_map = _build_module_to_file_map(graph)
    rev: Dict[str, Set[str]] = defaultdict(set)

    for e in graph.edges:
        if e.type == EdgeType.IMPORTS:
            module = e.metadata.get("module", "")
            if not module:
                continue
            target_id = mod_map.get(module)
            if not target_id:
                target_id = mod_map.get(module.rsplit(".", 1)[-1])
            if target_id:
                rev[target_id].add(e.source)

    return dict(rev)


def _node_by_path(graph: ArchGraph, file_path: str) -> Optional[ArchNode]:
    """Find a FILE node by partial path match."""
    file_path = file_path.replace("\\", "/")
    for n in graph.nodes:
        if n.type == NodeType.FILE:
            if n.file_path == file_path or n.file_path.endswith(file_path):
                return n
    return None


def _file_clusters(graph: ArchGraph) -> Dict[str, int]:
    """Count FILE nodes per top-level directory."""
    clusters: Dict[str, int] = defaultdict(int)
    for n in graph.nodes:
        if n.type == NodeType.FILE:
            parts = n.file_path.split("/")
            top = parts[0] if parts else "root"
            clusters[top] += 1
    return dict(sorted(clusters.items(), key=lambda x: -x[1]))


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def get_hot_files(limit: int = 15) -> List[Dict]:
    """Files ranked by import centrality (in-degree on IMPORTS edges)."""
    graph = _get_graph()
    in_deg = _compute_in_degree(graph)
    node_map = {n.id: n for n in graph.nodes}

    ranked = sorted(in_deg.items(), key=lambda x: -x[1])[:limit]
    results = []
    for nid, deg in ranked:
        node = node_map.get(nid)
        if node:
            results.append({
                "file": node.file_path,
                "name": node.name,
                "imports": deg,
                "category": node.category,
            })
    return results


def get_impact(file_path: str) -> Dict:
    """Transitive dependents of a file (BFS on reversed import edges)."""
    graph = _get_graph()
    node = _node_by_path(graph, file_path)
    if not node:
        return {"error": f"File not found: {file_path}", "affected": []}

    rev = _build_reverse_imports(graph)
    node_map = {n.id: n for n in graph.nodes}

    visited: Set[str] = set()
    frontier = {node.id}
    while frontier:
        next_frontier: Set[str] = set()
        for nid in frontier:
            if nid in visited:
                continue
            visited.add(nid)
            for dep in rev.get(nid, set()):
                if dep not in visited:
                    next_frontier.add(dep)
        frontier = next_frontier

    visited.discard(node.id)
    affected = []
    for nid in visited:
        n = node_map.get(nid)
        if n and n.type == NodeType.FILE:
            affected.append(n.file_path)

    return {
        "file": file_path,
        "affected_count": len(affected),
        "affected": sorted(affected),
    }


def query_deps(file_path: str) -> Dict:
    """What this file imports and what imports this file."""
    graph = _get_graph()
    node = _node_by_path(graph, file_path)
    if not node:
        return {"error": f"File not found: {file_path}"}

    mod_map = _build_module_to_file_map(graph)
    node_map = {n.id: n for n in graph.nodes}

    imports_out = []  # internal files this file imports
    imported_by = []  # files that import this file

    for e in graph.edges:
        if e.type != EdgeType.IMPORTS:
            continue
        module = e.metadata.get("module", "")
        if not module:
            continue

        # Resolve module to file node
        target_id = mod_map.get(module) or mod_map.get(module.rsplit(".", 1)[-1])

        if e.source == node.id and target_id:
            t = node_map.get(target_id)
            if t and t.file_path != node.file_path:
                imports_out.append(t.file_path)
        elif target_id == node.id:
            s = node_map.get(e.source)
            if s:
                imported_by.append(s.file_path)

    return {
        "file": file_path,
        "imports": sorted(set(imports_out)),
        "imported_by": sorted(set(imported_by)),
    }


def get_changes_summary() -> str:
    """What changed since last tagged commit, with impact analysis."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return ""
        changed = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return ""

    if not changed:
        return "No changes since last commit."

    graph = _get_graph()
    lines = [f"Changed files ({len(changed)}):"]
    for f in changed[:10]:
        impact = get_impact(f)
        count = impact.get("affected_count", 0)
        marker = f" → affects {count} files" if count > 0 else ""
        lines.append(f"  {f}{marker}")
    if len(changed) > 10:
        lines.append(f"  ... +{len(changed) - 10} more")
    return "\n".join(lines)


def get_wake_up_summary() -> str:
    """
    Compact text summary for Section 4 injection (~400 tokens).

    Format:
        CODEBASE MAP (N py files, commit XXXXXXX)
          HOT FILES: sqlite_storage(23 imp), context_dna_client(18), ...
          CHAINS: agent_service→persistent_hook→sqlite_storage→learnings.db
          CHANGED: persistent_hook_structure.py→affects 12 files
          CLUSTERS: memory/(78), voice/(41), scripts/(23)
    """
    try:
        graph = _get_graph()
    except Exception:
        return ""

    stats = graph.stats
    version = graph.version or "unknown"

    # File count
    file_count = stats.get("node_types", {}).get("file", 0)

    lines = [f"CODEBASE MAP ({file_count} files, commit {version})"]

    # Hot files (top 8 for brevity)
    hot = get_hot_files(limit=8)
    if hot:
        parts = [f"{h['name']}({h['imports']})" for h in hot]
        lines.append(f"  HOT: {', '.join(parts)}")

    # Dependency chains — find longest import chains from hot files
    chains = _find_key_chains(graph, hot[:3])
    if chains:
        for chain in chains[:2]:
            lines.append(f"  CHAIN: {chain}")

    # Recent changes with impact
    changes = get_changes_summary()
    if changes and changes != "No changes since last commit.":
        # Extract just the impactful ones
        change_lines = changes.split("\n")[1:]  # skip header
        impactful = [l.strip() for l in change_lines if "affects" in l]
        if impactful:
            lines.append(f"  CHANGED: {'; '.join(impactful[:3])}")

    # Clusters
    clusters = _file_clusters(graph)
    if clusters:
        parts = [f"{k}/({v})" for k, v in list(clusters.items())[:6]]
        lines.append(f"  CLUSTERS: {', '.join(parts)}")

    # Edge summary
    edge_count = stats.get("total_edges", 0)
    import_count = stats.get("edge_types", {}).get("imports", 0)
    lines.append(f"  EDGES: {edge_count} total, {import_count} imports")

    return "\n".join(lines)


def _find_key_chains(graph: ArchGraph, hot_files: List[Dict], max_depth: int = 4) -> List[str]:
    """Find import chains starting from hot files (DFS, max depth)."""
    node_map = {n.id: n for n in graph.nodes}
    # Build forward imports: source → list of targets
    fwd: Dict[str, List[str]] = defaultdict(list)
    for e in graph.edges:
        if e.type == EdgeType.IMPORTS:
            fwd[e.source].append(e.target)

    # Find FILE nodes for hot files
    hot_nodes = []
    for h in hot_files:
        for n in graph.nodes:
            if n.type == NodeType.FILE and n.file_path == h.get("file"):
                hot_nodes.append(n)
                break

    chains = []
    for start in hot_nodes:
        # DFS to find longest chain
        best_chain = [start.name]
        stack = [(start.id, [start.name])]
        while stack:
            current, path = stack.pop()
            if len(path) >= max_depth:
                if len(path) > len(best_chain):
                    best_chain = path
                continue
            for target_id in fwd.get(current, []):
                target = node_map.get(target_id)
                if target and target.name not in path:
                    new_path = path + [target.name]
                    stack.append((target_id, new_path))
                    if len(new_path) > len(best_chain):
                        best_chain = new_path

        if len(best_chain) > 1:
            chains.append("→".join(best_chain))

    return chains


def refresh():
    """Trigger incremental graph rebuild (for post-commit hook / scheduler)."""
    global _graph_cache
    _graph_cache = None
    _get_graph(force_rebuild=False)  # uses incremental update via cache


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _cli():
    import json

    if len(sys.argv) < 2:
        print("Usage: python memory/codebase_map.py <command> [args]")
        print("Commands: summary, hot, deps <file>, impact <file>, changes")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "summary":
        print(get_wake_up_summary())

    elif cmd == "hot":
        hot = get_hot_files(limit=15)
        for i, h in enumerate(hot, 1):
            print(f"  {i:2d}. {h['file']} ({h['imports']} imports) [{h['category']}]")

    elif cmd == "deps":
        if len(sys.argv) < 3:
            print("Usage: python memory/codebase_map.py deps <file_path>")
            sys.exit(1)
        result = query_deps(sys.argv[2])
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"File: {result['file']}")
            print(f"\nImports ({len(result['imports'])}):")
            for f in result["imports"]:
                print(f"  → {f}")
            print(f"\nImported by ({len(result['imported_by'])}):")
            for f in result["imported_by"]:
                print(f"  ← {f}")

    elif cmd == "impact":
        if len(sys.argv) < 3:
            print("Usage: python memory/codebase_map.py impact <file_path>")
            sys.exit(1)
        result = get_impact(sys.argv[2])
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            print(f"Impact of {result['file']}: {result['affected_count']} files affected")
            for f in result["affected"]:
                print(f"  ← {f}")

    elif cmd == "changes":
        print(get_changes_summary())

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: summary, hot, deps <file>, impact <file>, changes")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
