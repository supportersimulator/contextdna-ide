"""
Data Models for Architectural Awareness System

These dataclasses represent the nodes and edges of the architecture graph
that powers the mind map visualization in the Context DNA dashboard.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Any
from datetime import datetime
import hashlib


class NodeType(str, Enum):
    """Types of nodes in the architecture graph."""
    FILE = "file"
    CLASS = "class"
    FUNCTION = "function"
    SERVICE = "service"
    COMPONENT = "component"
    MODULE = "module"
    API_ROUTE = "api_route"
    HELPER = "helper"
    HOOK = "hook"
    CONSTANT = "constant"


class EdgeType(str, Enum):
    """Types of relationships between nodes."""
    IMPORTS = "imports"
    CALLS = "calls"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    USES = "uses"
    WRAPS = "wraps"
    DATA_FLOW = "data_flow"
    CONTAINS = "contains"  # Parent-child relationship (file contains class)
    EXPORTS = "exports"


@dataclass
class ArchNode:
    """
    A node in the architecture graph representing a code element.

    Attributes:
        id: Unique identifier (hash of file_path + name + type)
        type: The type of code element (file, class, function, etc.)
        name: The name of the code element
        file_path: Path to the file containing this element
        line_start: Starting line number in the file
        line_end: Ending line number in the file
        category: Category from knowledge_graph.py (e.g., "Infrastructure/AWS")
        metadata: Additional information (docstring, params, return type, etc.)
    """
    id: str
    type: NodeType
    name: str
    file_path: str
    line_start: int
    line_end: int = 0
    category: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        type: NodeType,
        name: str,
        file_path: str,
        line_start: int,
        line_end: int = 0,
        category: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ArchNode":
        """Factory method that generates a unique ID automatically."""
        # Create deterministic ID from key attributes
        id_source = f"{file_path}:{type.value}:{name}:{line_start}"
        node_id = hashlib.sha256(id_source.encode()).hexdigest()[:12]

        return cls(
            id=f"{type.value}_{node_id}",
            type=type,
            name=name,
            file_path=file_path,
            line_start=line_start,
            line_end=line_end or line_start,
            category=category,
            metadata=metadata or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "category": self.category,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchNode":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            type=NodeType(data["type"]),
            name=data["name"],
            file_path=data["file_path"],
            line_start=data["line_start"],
            line_end=data.get("line_end", data["line_start"]),
            category=data.get("category", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ArchEdge:
    """
    An edge in the architecture graph representing a relationship.

    Attributes:
        id: Unique identifier (hash of source + target + type)
        source: ID of the source node
        target: ID of the target node
        type: The type of relationship
        metadata: Additional information (call count, import alias, etc.)
    """
    id: str
    source: str  # Node ID
    target: str  # Node ID
    type: EdgeType
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        source: str,
        target: str,
        type: EdgeType,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ArchEdge":
        """Factory method that generates a unique ID automatically."""
        id_source = f"{source}:{target}:{type.value}"
        edge_id = hashlib.sha256(id_source.encode()).hexdigest()[:12]

        return cls(
            id=f"edge_{edge_id}",
            source=source,
            target=target,
            type=type,
            metadata=metadata or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "type": self.type.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchEdge":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            source=data["source"],
            target=data["target"],
            type=EdgeType(data["type"]),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ArchGraph:
    """
    The complete architecture graph.

    Attributes:
        nodes: List of nodes in the graph
        edges: List of edges (relationships) in the graph
        timestamp: When this graph was generated
        version: Git commit hash or version identifier
        changed_nodes: IDs of nodes that changed since last update
        stats: Summary statistics about the graph
    """
    nodes: List[ArchNode]
    edges: List[ArchEdge]
    timestamp: str = ""
    version: str = ""
    changed_nodes: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Set timestamp if not provided."""
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

        # Compute stats if not provided
        if not self.stats:
            self.stats = self.compute_stats()

    def compute_stats(self) -> Dict[str, Any]:
        """Compute summary statistics about the graph."""
        node_types = {}
        edge_types = {}
        categories = {}

        for node in self.nodes:
            node_types[node.type.value] = node_types.get(node.type.value, 0) + 1
            if node.category:
                cat_root = node.category.split("/")[0]
                categories[cat_root] = categories.get(cat_root, 0) + 1

        for edge in self.edges:
            edge_types[edge.type.value] = edge_types.get(edge.type.value, 0) + 1

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_types": node_types,
            "edge_types": edge_types,
            "categories": categories,
        }

    def get_node(self, node_id: str) -> Optional[ArchNode]:
        """Get a node by ID."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_subgraph(self, center_node_id: str, depth: int = 2) -> "ArchGraph":
        """
        Get a subgraph centered on a specific node.

        Args:
            center_node_id: The node to center on
            depth: How many edges away to include

        Returns:
            A new ArchGraph containing the subgraph
        """
        included_nodes = {center_node_id}

        # BFS to find nodes within depth
        frontier = {center_node_id}
        for _ in range(depth):
            new_frontier = set()
            for edge in self.edges:
                if edge.source in frontier:
                    new_frontier.add(edge.target)
                    included_nodes.add(edge.target)
                if edge.target in frontier:
                    new_frontier.add(edge.source)
                    included_nodes.add(edge.source)
            frontier = new_frontier

        # Filter nodes and edges
        sub_nodes = [n for n in self.nodes if n.id in included_nodes]
        sub_edges = [
            e for e in self.edges
            if e.source in included_nodes and e.target in included_nodes
        ]

        return ArchGraph(
            nodes=sub_nodes,
            edges=sub_edges,
            timestamp=self.timestamp,
            version=self.version,
            changed_nodes=[n for n in self.changed_nodes if n in included_nodes],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "timestamp": self.timestamp,
            "version": self.version,
            "changed_nodes": self.changed_nodes,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchGraph":
        """Create from dictionary."""
        return cls(
            nodes=[ArchNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[ArchEdge.from_dict(e) for e in data.get("edges", [])],
            timestamp=data.get("timestamp", ""),
            version=data.get("version", ""),
            changed_nodes=data.get("changed_nodes", []),
            stats=data.get("stats", {}),
        )

    def to_react_flow(self) -> Dict[str, Any]:
        """
        Convert to React Flow format for frontend visualization.

        Returns:
            Dictionary with 'nodes' and 'edges' in React Flow format
        """
        rf_nodes = []
        rf_edges = []

        # Node colors by category
        category_colors = {
            "Infrastructure": "#3b82f6",  # Blue
            "Voice_Pipeline": "#8b5cf6",  # Purple
            "Frontend": "#22c55e",        # Green
            "Backend": "#f97316",         # Orange
            "Memory_System": "#eab308",   # Yellow
            "Protocols": "#ec4899",       # Pink
            "Gotchas": "#ef4444",         # Red
        }

        for node in self.nodes:
            # Get color from category
            cat_root = node.category.split("/")[0] if node.category else ""
            color = category_colors.get(cat_root, "#6b7280")  # Default gray

            rf_nodes.append({
                "id": node.id,
                "type": node.type.value,
                "data": {
                    "label": node.name,
                    "nodeType": node.type.value,
                    "category": node.category,
                    "filePath": node.file_path,
                    "lineStart": node.line_start,
                    "lineEnd": node.line_end,
                    "metadata": node.metadata,
                    "color": color,
                },
                "position": {"x": 0, "y": 0},  # Layout calculated on frontend
            })

        for edge in self.edges:
            rf_edges.append({
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "type": edge.type.value,
                "data": {
                    "edgeType": edge.type.value,
                    "metadata": edge.metadata,
                },
                "animated": edge.type in [EdgeType.CALLS, EdgeType.DATA_FLOW],
            })

        return {
            "nodes": rf_nodes,
            "edges": rf_edges,
        }


# Type alias for diff tracking
GraphDiff = Dict[str, List[str]]  # {"added": [], "removed": [], "modified": []}
