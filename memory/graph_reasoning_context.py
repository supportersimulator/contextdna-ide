#!/usr/bin/env python3
"""
GRAPH REASONING CONTEXT — P4: LLM-Driven Creative Dependency Analysis

Philosophy:
  "Allow the thinking to do the thinking"
  - Give LLM the full graph structure
  - Empower creative reasoning without prescriptive algorithms
  - Evidence pipeline tracks which patterns work best
  - System evolves toward most effective reasoning patterns

Purpose:
  Prepare graph context for LLM reasoning with thinking mode enabled.
  LLM uses semantic analysis to:
  - Understand dependency relationships creatively
  - Weigh importance (critical vs. cosmetic) dynamically
  - Explore uncertainty
  - Organize domain knowledge in novel ways
  - Suggest study approaches based on contextual reasoning

Integration:
  ButlerDeepQuery → GetGraphReasoningContext → Qwen3 with Thinking Mode → Evidence loop
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class GraphReasoningContext:
    """Prepare graph context for LLM creative reasoning."""

    def __init__(self):
        self.storage_dir = Path.home() / ".context-dna"
        self.storage_dir.mkdir(exist_ok=True)

    def get_reasoning_context(
        self, include_gaps: bool = True, max_hubs: int = 25, include_full_graph: bool = False
    ) -> Dict[str, Any]:
        """
        Prepare structured context for LLM reasoning with thinking mode.

        Args:
          include_gaps: Include identified knowledge gaps from introspection
          max_hubs: Maximum number of hub nodes to include (critical infrastructure)
          include_full_graph: If True, include full 13K-node structure; else compressed

        Returns:
          Structured context ready for LLM reasoning with thinking mode
        """
        context = {
            "timestamp": datetime.now().isoformat(),
            "philosophy": "Creative LLM reasoning with evidence-based evolution",
            "thinking_mode_enabled": True,
            "graph_structure": self._get_graph_structure(include_full_graph, max_hubs),
        }

        # Add knowledge gaps if requested
        if include_gaps:
            try:
                from memory.introspection_engine import KnowledgeCoverageAuditor

                auditor = KnowledgeCoverageAuditor()
                coverage = auditor.audit_coverage()
                context["knowledge_gaps"] = coverage.get("gaps", [])
                context["coverage_summary"] = {
                    "total_files": coverage.get("total_files", 0),
                    "total_learnings": coverage.get("total_learnings", 0),
                    "domains": coverage.get("domains", {}),
                }
            except Exception as e:
                logger.warning(f"Could not fetch knowledge gaps: {e}")
                context["knowledge_gaps"] = []

        return context

    def _get_graph_structure(
        self, include_full: bool = False, max_hubs: int = 25
    ) -> Dict[str, Any]:
        """
        Get graph structure for LLM reasoning.

        Args:
          include_full: Include full 13K-node graph (expensive, reasoning-intensive)
          max_hubs: Maximum hub nodes to include

        Returns:
          Structured graph representation
        """
        try:
            graph_cache = self._load_graph_cache()
            if not graph_cache:
                return {"status": "unavailable"}

            structure = {
                "total_nodes": len(graph_cache.get("nodes", [])),
                "total_edges": len(graph_cache.get("edges", [])),
                "node_types": self._analyze_node_types(graph_cache),
                "edge_types": self._analyze_edge_types(graph_cache),
                "critical_hubs": self._get_critical_hubs(graph_cache, max_hubs),
            }

            # Option: Include full graph if requested
            if include_full:
                structure["full_graph"] = graph_cache
            else:
                # Compressed representation for efficient LLM reasoning
                structure["topology_summary"] = self._get_topology_summary(graph_cache)

            return structure

        except Exception as e:
            logger.error(f"Failed to analyze graph structure: {e}")
            return {"status": "error", "message": str(e)}

    def _load_graph_cache(self) -> Optional[Dict[str, Any]]:
        """Load architecture graph cache."""
        try:
            cache_path = (
                Path.home()
                / "dev/er-simulator-superrepo/context-dna/infra/.architecture_graph_cache.json"
            )
            if cache_path.exists():
                with open(cache_path) as f:
                    data = json.load(f)
                    return data.get("graph")
        except Exception as e:
            logger.error(f"Failed to load graph cache: {e}")

        return None

    def _analyze_node_types(self, graph: Dict[str, Any]) -> Dict[str, int]:
        """Count nodes by type."""
        types = {}
        for node in graph.get("nodes", []):
            node_type = node.get("type", "unknown")
            types[node_type] = types.get(node_type, 0) + 1
        return types

    def _analyze_edge_types(self, graph: Dict[str, Any]) -> Dict[str, int]:
        """Count edges by type."""
        types = {}
        for edge in graph.get("edges", []):
            edge_type = edge.get("type", "unknown")
            types[edge_type] = types.get(edge_type, 0) + 1
        return types

    def _get_critical_hubs(
        self, graph: Dict[str, Any], max_count: int = 25
    ) -> List[Dict[str, Any]]:
        """
        Identify critical hub nodes (high connectivity).

        Hub = >50 outgoing edges
        Critical Hub = >100 outgoing edges
        """
        edges = graph.get("edges", [])

        # Count outgoing edges per node
        outgoing_counts = {}
        for edge in edges:
            source = edge.get("source")
            if source:
                outgoing_counts[source] = outgoing_counts.get(source, 0) + 1

        # Find nodes matching node objects
        node_map = {node.get("id"): node for node in graph.get("nodes", [])}

        # Extract hubs (>50 edges)
        hubs = []
        for node_id, count in sorted(outgoing_counts.items(), key=lambda x: -x[1]):
            if count >= 50:
                node = node_map.get(node_id)
                if node:
                    hubs.append(
                        {
                            "id": node_id,
                            "file_path": node.get("file_path", "unknown"),
                            "type": node.get("type", "unknown"),
                            "outgoing_edges": count,
                            "criticality": "CRITICAL" if count >= 100 else "HUB",
                        }
                    )

                if len(hubs) >= max_count:
                    break

        return hubs

    def _get_topology_summary(self, graph: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get compressed topology summary for efficient LLM reasoning.

        Represents graph structure without full node/edge enumeration.
        """
        edges = graph.get("edges", [])

        # Connectivity metrics
        in_degree = {}
        out_degree = {}
        edge_distribution = {}

        for edge in edges:
            edge_type = edge.get("type", "unknown")
            source = edge.get("source")
            target = edge.get("target")

            out_degree[source] = out_degree.get(source, 0) + 1
            in_degree[target] = in_degree.get(target, 0) + 1
            edge_distribution[edge_type] = edge_distribution.get(edge_type, 0) + 1

        return {
            "connectivity_distribution": {
                "avg_out_degree": sum(out_degree.values()) / max(len(out_degree), 1),
                "avg_in_degree": sum(in_degree.values()) / max(len(in_degree), 1),
                "max_connectivity": max(out_degree.values()) if out_degree else 0,
            },
            "edge_distribution": edge_distribution,
            "graph_density_estimate": len(edges) / max(len(graph.get("nodes", [])) ** 2, 1),
        }

    def generate_reasoning_prompt(self, context: Dict[str, Any]) -> str:
        """
        Generate prompt for LLM thinking mode analysis.

        Presents evidence and open questions. Lets LLM reason naturally.
        No prescribed steps, no output format requirements.
        """
        prompt = f"""
# Architecture Analysis: Learning Map for the System

You're seeing the dependency structure of a large codebase:
- {context['graph_structure'].get('total_nodes', 'unknown')} components
- {context['graph_structure'].get('total_edges', 'unknown')} relationships
- Node types: {json.dumps(context['graph_structure'].get('node_types', {}))}
- Relationship types: {json.dumps(context['graph_structure'].get('edge_types', {}))}

## Critical Infrastructure (Most Connected Components)
{self._format_hubs(context['graph_structure'].get('critical_hubs', []))}

## Knowledge Gaps (Domains with Few Learnings)
{self._format_gaps(context.get('knowledge_gaps', []))}

## Questions for Your Analysis

Given this map, think through whatever seems important:

- **What's foundational here?** Which domains form the base that everything else depends on?
- **Where are the learning bottlenecks?** If we filled one knowledge gap, which other questions would it answer?
- **What patterns emerge from the structure?** Are certain domains always connected? Do any form natural clusters?
- **How would you prioritize learning?** What sequence would build understanding incrementally?
- **What looks fragile or over-connected?** Which hubs carry too much responsibility?
- **What's unclear?** What would make you more confident about these relationships?
- **What surprises you?** Any unexpected connections or gaps?

## Your Thinking Process

Use multi-step reasoning as it's helpful to you. Explore the structure. Follow interesting threads.
Let patterns emerge naturally.

Share what you discover in whatever way makes sense — you decide how to organize your insights.
(Your thinking process itself is valuable information for system learning.)
"""
        return prompt

    def _format_hubs(self, hubs: List[Dict[str, Any]]) -> str:
        """Format hub nodes for prompt."""
        if not hubs:
            return "No critical hubs detected."

        lines = []
        for hub in hubs[:10]:  # Top 10
            lines.append(
                f"  • {hub['file_path']} ({hub['outgoing_edges']} edges, "
                f"{hub['criticality']})"
            )

        return "\n".join(lines)

    def _format_gaps(self, gaps: List[Dict[str, Any]]) -> str:
        """Format knowledge gaps for prompt."""
        if not gaps:
            return "No significant gaps identified."

        lines = []
        for gap in gaps[:10]:  # Top 10
            lines.append(f"  • {gap['domain']}: {gap['gap_score']:.0%} gap " f"({gap['files']} files, {gap['learnings']} learnings, {gap['priority']})")

        return "\n".join(lines)


# ============================================================================
# CLI / TESTING
# ============================================================================


def main():
    """Test graph reasoning context generation."""
    import logging

    logging.basicConfig(level=logging.INFO)

    print("\n" + "=" * 80)
    print("GRAPH REASONING CONTEXT — P4 TESTING")
    print("=" * 80)

    ctx = GraphReasoningContext()

    # Get reasoning context
    print("\n📊 Fetching reasoning context...")
    context = ctx.get_reasoning_context(
        include_gaps=True, max_hubs=20, include_full_graph=False
    )

    print(f"\nGraph Structure:")
    print(f"  Total Nodes: {context['graph_structure'].get('total_nodes', 'unknown')}")
    print(f"  Total Edges: {context['graph_structure'].get('total_edges', 'unknown')}")
    print(f"  Node Types: {json.dumps(context['graph_structure'].get('node_types', {}), indent=4)}")

    if context.get("coverage_summary"):
        summary = context["coverage_summary"]
        print(f"\nCoverage Summary:")
        print(f"  Total Files: {summary.get('total_files', 0)}")
        print(f"  Total Learnings: {summary.get('total_learnings', 0)}")

    if context.get("knowledge_gaps"):
        print(f"\nTop Gaps:")
        for gap in context["knowledge_gaps"][:5]:
            print(f"  • {gap['domain']}: {gap['gap_score']:.0%} gap (Priority: {gap['priority']})")

    # Generate reasoning prompt
    print(f"\n💡 Generating LLM reasoning prompt...")
    prompt = ctx.generate_reasoning_prompt(context)
    print(f"Prompt length: {len(prompt)} characters")
    print(f"\nSample prompt (first 500 chars):\n{prompt[:500]}...")


if __name__ == "__main__":
    main()
