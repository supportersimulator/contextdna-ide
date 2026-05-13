"""
Code Parser Module for Architectural Awareness

This module provides AST parsing and graph building capabilities for
visualizing codebase architecture in the Context DNA dashboard.

Components:
- models.py: Data types (ArchNode, ArchEdge, ArchGraph)
- ast_analyzer.py: Python AST parsing
- ts_analyzer.py: TypeScript/JavaScript parsing
- graph_builder.py: Orchestrates parsers and builds architecture graph
- file_watcher.py: Watches for file changes to trigger graph updates
"""

from .models import (
    NodeType,
    EdgeType,
    ArchNode,
    ArchEdge,
    ArchGraph,
)
from .graph_builder import (
    ArchitectureGraphBuilder,
    build_architecture_graph,
)
from .file_watcher import (
    ArchitectureWatcher,
    ArchitectureWatcherWithGraphUpdate,
    watch_architecture,
)

__all__ = [
    # Models
    "NodeType",
    "EdgeType",
    "ArchNode",
    "ArchEdge",
    "ArchGraph",
    # Graph Builder
    "ArchitectureGraphBuilder",
    "build_architecture_graph",
    # File Watcher
    "ArchitectureWatcher",
    "ArchitectureWatcherWithGraphUpdate",
    "watch_architecture",
]
