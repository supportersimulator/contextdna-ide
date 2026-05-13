#!/usr/bin/env python3
"""
GRAPH TRAVERSAL & RIPPLE CHAIN ANALYSIS — P3 Enhancement

Purpose:
  Trace dependency ripple effects through the codebase graph:
  1. Identify files affected by a change (L1/L2/L3 ripple)
  2. Detect critical path hubs (high-impact nodes)
  3. Suggest test targets for code changes
  4. Provide risk assessment based on connectivity

Architecture:
  - Loads cached architecture graph (13K nodes, 58K edges)
  - BFS traversal for distance-based ripple calculation
  - Hub detection: nodes with >50 outgoing edges = critical infrastructure
  - Risk scoring based on hub count, distance, and node types

Output Format (for Section 3 injection):
  Changed: file.py (199 edges, CRITICAL HUB)
  L1 ripple: 12 files directly affected
  L2 ripple: 28 files indirectly affected
  L3 ripple: 47 files at distance 3
  RISK: HIGH (touches 3 critical hubs)
  SUGGEST TEST: test_file1.py, test_file2.py
"""

import json
import logging
from collections import deque, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Hub detection threshold
HUB_THRESHOLD = 50  # Nodes with >50 outgoing edges are hubs
CRITICAL_HUB_THRESHOLD = 100  # Nodes with >100 outgoing are critical hubs


class GraphCache:
    """Lazy-load and cache the architecture graph."""

    _instance: Optional["GraphCache"] = None
    _graph: Optional[Dict[str, Any]] = None

    @classmethod
    def get(cls) -> Optional[Dict[str, Any]]:
        """Get or load the graph cache."""
        if cls._graph is None:
            cls._load()
        return cls._graph

    @classmethod
    def _load(cls):
        """Load graph from cache file."""
        try:
            cache_path = (
                Path.home()
                / "dev/er-simulator-superrepo/context-dna/infra/.architecture_graph_cache.json"
            )
            if cache_path.exists() and cache_path.stat().st_size > 0:
                with open(cache_path) as f:
                    data = json.load(f)
                    cls._graph = data.get("graph")
                    logger.debug(
                        f"Loaded graph: {len(cls._graph['nodes'])} nodes, "
                        f"{len(cls._graph.get('edges', []))} edges"
                    )
        except Exception as e:
            logger.error(f"Failed to load graph cache: {e}")


class RippleChainAnalyzer:
    """Analyze dependency ripples through the codebase graph."""

    def __init__(self):
        self.graph = GraphCache.get()
        if not self.graph:
            logger.warning("Graph cache unavailable - ripple analysis will be limited")

    def trace_ripple_chain(
        self, file_path: str, max_depth: int = 3
    ) -> Dict[str, Any]:
        """
        Trace ripple effects when a file changes.

        Args:
            file_path: Path to changed file (e.g., 'memory/persistent_hook_structure.py')
            max_depth: Maximum ripple depth to analyze (1-3)

        Returns:
            {
                'file_path': str,
                'node_id': str or None,
                'direct_edges': int,
                'is_hub': bool,
                'ripple_by_depth': {
                    1: [list of affected files],
                    2: [list of files at distance 2],
                    3: [list of files at distance 3]
                },
                'critical_hubs_affected': list,
                'risk_score': float (0.0-1.0),
                'risk_level': 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
            }
        """
        if not self.graph:
            return self._fallback_ripple(file_path)

        # Find node for this file
        node = self._find_file_node(file_path)
        if not node:
            return self._fallback_ripple(file_path)

        node_id = node["id"]
        edges = self.graph.get("edges", [])

        # Count outgoing edges (impact measure)
        outgoing = sum(1 for e in edges if e["source"] == node_id)
        is_hub = outgoing > HUB_THRESHOLD
        is_critical_hub = outgoing > CRITICAL_HUB_THRESHOLD

        # BFS to find all affected nodes at each depth
        ripple_by_depth = self._bfs_ripple(node_id, max_depth)

        # Find critical hubs in the ripple
        critical_hubs = self._find_critical_hubs_in_ripple(
            ripple_by_depth, max_depth
        )

        # Calculate risk
        risk_score, risk_level = self._calculate_risk(
            len(ripple_by_depth.get(1, [])),
            len(critical_hubs),
            is_critical_hub,
        )

        return {
            "file_path": file_path,
            "node_id": node_id,
            "direct_edges": outgoing,
            "is_hub": is_hub,
            "is_critical_hub": is_critical_hub,
            "ripple_by_depth": {
                depth: [self._get_file_path(nid) for nid in nodes]
                for depth, nodes in ripple_by_depth.items()
            },
            "critical_hubs_affected": critical_hubs,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "total_affected": sum(len(nodes) for nodes in ripple_by_depth.values()),
        }

    def get_critical_path_risk(self, changed_files: List[str]) -> Dict[str, Any]:
        """
        Assess risk of multiple file changes.

        Args:
            changed_files: List of changed file paths

        Returns:
            {
                'files_analyzed': int,
                'total_affected_files': int,
                'critical_hubs_touched': int,
                'combined_risk_score': float,
                'combined_risk_level': str,
                'recommendations': [list of strings]
            }
        """
        analyses = [self.trace_ripple_chain(f) for f in changed_files]

        # Combine metrics
        total_affected = sum(a.get("total_affected", 0) for a in analyses)
        hub_count = sum(
            len(a.get("critical_hubs_affected", [])) for a in analyses
        )

        risk_scores = [a.get("risk_score", 0.5) for a in analyses]
        combined_score = sum(risk_scores) / len(risk_scores) if risk_scores else 0.5

        # Risk level determination
        if combined_score >= 0.8 or hub_count >= 3:
            risk_level = "CRITICAL"
        elif combined_score >= 0.6 or hub_count >= 2:
            risk_level = "HIGH"
        elif combined_score >= 0.4 or hub_count >= 1:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        # Recommendations
        recommendations = self._generate_recommendations(
            analyses, risk_level, total_affected
        )

        return {
            "files_analyzed": len(changed_files),
            "total_affected_files": total_affected,
            "critical_hubs_touched": hub_count,
            "combined_risk_score": combined_score,
            "combined_risk_level": risk_level,
            "recommendations": recommendations,
        }

    def suggest_test_targets(self, changed_files: List[str]) -> List[str]:
        """
        Suggest which test files to run based on changed files.

        Args:
            changed_files: List of changed file paths

        Returns:
            List of test files that should be run
        """
        all_affected = set()
        for f in changed_files:
            ripple = self.trace_ripple_chain(f)
            for nodes in ripple.get("ripple_by_depth", {}).values():
                all_affected.update(nodes)

        # Find test files that match affected modules
        test_suggestions = []
        for test_file in self._find_test_files():
            # Extract module name from test file
            # e.g., test_scheduler.py -> module 'scheduler'
            module_name = test_file.replace("test_", "").replace(".py", "")
            if any(module_name in affected for affected in all_affected):
                test_suggestions.append(test_file)

        return sorted(set(test_suggestions))[:10]  # Top 10 suggestions

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _find_file_node(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Find a node in the graph matching the file path."""
        if not self.graph:
            return None

        for node in self.graph.get("nodes", []):
            if node.get("type") == "file" and file_path in node.get("file_path", ""):
                return node
        return None

    def _bfs_ripple(
        self, start_node_id: str, max_depth: int
    ) -> Dict[int, Set[str]]:
        """BFS to find all nodes affected by a change.

        When file X changes, files that IMPORT from X are affected (reverse deps).
        Edge direction: source -> target means "source imports target"
        So we need edges where target = X, and collect source nodes (they import X).
        """
        if not self.graph:
            return {}

        edges = self.graph.get("edges", [])
        ripple = defaultdict(set)

        # Build adjacency list: files that depend on each file (reverse imports)
        # If A imports B: source=A, target=B
        # So if B changes: find all source where target=B (files importing B)
        dependents = defaultdict(list)  # dependents[B] = [A, C, ...] files importing B
        for edge in edges:
            if edge.get("type") in ["imports", "calls", "extends", "implements"]:
                dependents[edge["target"]].append(edge["source"])

        # BFS: start from changed node, follow dependent edges
        visited = {start_node_id}
        queue = deque([(start_node_id, 0)])
        total_affected = 0

        while queue and total_affected < 500:  # Limit to prevent explosion
            node_id, depth = queue.popleft()
            if depth > 0:
                ripple[depth].add(node_id)
                total_affected += 1
            if depth < max_depth:
                for dep_id in dependents.get(node_id, []):
                    if dep_id not in visited:
                        visited.add(dep_id)
                        queue.append((dep_id, depth + 1))

        return {d: set(nodes) for d, nodes in ripple.items()}

    def _find_critical_hubs_in_ripple(
        self, ripple_by_depth: Dict[int, Set[str]], max_depth: int
    ) -> List[str]:
        """Identify critical hub nodes in the ripple chain."""
        if not self.graph:
            return []

        edges = self.graph.get("edges", [])
        hubs = []

        # Collect all affected nodes
        affected = set()
        for nodes in ripple_by_depth.values():
            affected.update(nodes)

        # Find which are hubs
        for node_id in affected:
            outgoing = sum(1 for e in edges if e["source"] == node_id)
            if outgoing > CRITICAL_HUB_THRESHOLD:
                hubs.append(self._get_file_path(node_id))

        return hubs

    def _calculate_risk(
        self, l1_count: int, hub_count: int, is_critical_hub: bool
    ) -> Tuple[float, str]:
        """Calculate risk score and level."""
        score = 0.0

        # Factor 1: L1 ripple size (0.3 max)
        if l1_count > 50:
            score += 0.3
        elif l1_count > 20:
            score += 0.2
        elif l1_count > 10:
            score += 0.1

        # Factor 2: Critical hub involvement (0.4 max)
        if is_critical_hub:
            score += 0.4
        elif hub_count > 0:
            score += min(0.2, hub_count * 0.1)

        # Factor 3: Multi-hub cascades (0.3 max)
        if hub_count > 2:
            score += 0.3
        elif hub_count > 1:
            score += 0.15

        score = min(1.0, score)

        # Map to risk level
        if score >= 0.8:
            level = "CRITICAL"
        elif score >= 0.6:
            level = "HIGH"
        elif score >= 0.4:
            level = "MEDIUM"
        else:
            level = "LOW"

        return score, level

    def _generate_recommendations(
        self, analyses: List[Dict[str, Any]], risk_level: str, total_affected: int
    ) -> List[str]:
        """Generate risk mitigation recommendations."""
        recommendations = []

        if risk_level == "CRITICAL":
            recommendations.append("🚨 CRITICAL RISK: Run full test suite before merge")
            recommendations.append("Review all affected critical paths")
            recommendations.append("Consider feature flag or gradual rollout")
        elif risk_level == "HIGH":
            recommendations.append("⚠️  HIGH RISK: Run primary test suite")
            recommendations.append(f"Monitor {total_affected}+ affected files")
        elif risk_level == "MEDIUM":
            recommendations.append("Run targeted tests for changed modules")
        else:
            recommendations.append("✅ LOW RISK: Standard testing sufficient")

        # Add specific recommendations
        if total_affected > 100:
            recommendations.append(f"Large blast radius: {total_affected} files affected")
        if any(a.get("is_critical_hub") for a in analyses):
            recommendations.append("Alert: Changes to critical infrastructure hub")

        return recommendations

    def _get_file_path(self, node_id: str) -> str:
        """Get file path for a node."""
        if not self.graph:
            return node_id

        for node in self.graph.get("nodes", []):
            if node.get("id") == node_id:
                return node.get("file_path", node_id)
        return node_id

    def _find_test_files(self) -> List[str]:
        """Find all test files in the codebase."""
        if not self.graph:
            return []

        test_files = set()
        for node in self.graph.get("nodes", []):
            path = node.get("file_path", "")
            if "test" in path and path.endswith(".py"):
                test_files.add(path.split("/")[-1])

        return list(test_files)

    def _fallback_ripple(self, file_path: str) -> Dict[str, Any]:
        """Fallback when graph is unavailable."""
        return {
            "file_path": file_path,
            "node_id": None,
            "direct_edges": 0,
            "is_hub": False,
            "is_critical_hub": False,
            "ripple_by_depth": {},
            "critical_hubs_affected": [],
            "risk_score": 0.5,
            "risk_level": "UNKNOWN",
            "total_affected": 0,
            "note": "Graph cache unavailable - using fallback",
        }


# ============================================================================
# CLI / TESTING
# ============================================================================


def main():
    """Test graph traversal."""
    import logging

    logging.basicConfig(level=logging.INFO)

    analyzer = RippleChainAnalyzer()

    # Test cases
    test_files = [
        "memory/persistent_hook_structure.py",
        "memory/agent_service.py",
        "memory/lite_scheduler.py",
    ]

    print("\n" + "=" * 80)
    print("RIPPLE CHAIN ANALYSIS — P3 TESTING")
    print("=" * 80)

    for file_path in test_files:
        print(f"\n📊 Analyzing: {file_path}")
        result = analyzer.trace_ripple_chain(file_path, max_depth=3)

        print(f"  Direct edges: {result['direct_edges']}")
        print(f"  Is hub: {result['is_hub']}")
        print(f"  Is critical hub: {result['is_critical_hub']}")
        print(f"  Risk level: {result['risk_level']} ({result['risk_score']:.1%})")
        print(f"  Total affected: {result['total_affected']} files")

        for depth in sorted(result["ripple_by_depth"].keys()):
            files = result["ripple_by_depth"][depth]
            print(f"  L{depth} ripple: {len(files)} files")
            if len(files) <= 3:
                for f in files:
                    print(f"    - {f}")
            else:
                for f in files[:2]:
                    print(f"    - {f}")
                print(f"    ... and {len(files) - 2} more")

    # Multi-file analysis
    print(f"\n📈 Combined analysis: {test_files}")
    combined = analyzer.get_critical_path_risk(test_files)
    print(f"  Total affected: {combined['total_affected_files']} files")
    print(f"  Critical hubs touched: {combined['critical_hubs_touched']}")
    print(f"  Risk level: {combined['combined_risk_level']}")
    for rec in combined["recommendations"]:
        print(f"  • {rec}")

    # Test suggestions
    print(f"\n🧪 Suggested tests:")
    tests = analyzer.suggest_test_targets(test_files)
    for test in tests[:5]:
        print(f"  • {test}")


if __name__ == "__main__":
    main()
