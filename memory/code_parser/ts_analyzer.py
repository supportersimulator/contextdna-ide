"""
TypeScript/JavaScript Analyzer for Architectural Awareness

Uses regex-based parsing (ZERO external dependencies) to extract structural
information from TypeScript and JavaScript files. While not as comprehensive
as a full AST parser, this approach handles common patterns in React/Next.js
codebases effectively.
"""

import re
import os
import sys
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memory.code_parser.models import (
    NodeType,
    EdgeType,
    ArchNode,
    ArchEdge,
)


class TypeScriptAnalyzer:
    """
    Analyzes TypeScript/JavaScript files using regex patterns to extract
    structural information for the architecture graph.
    """

    # Regex patterns for various code structures
    PATTERNS = {
        # Import statements
        "import_default": re.compile(
            r"import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]",
            re.MULTILINE
        ),
        "import_named": re.compile(
            r"import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]",
            re.MULTILINE
        ),
        "import_all": re.compile(
            r"import\s+\*\s+as\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]",
            re.MULTILINE
        ),
        "require": re.compile(
            r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(['\"]([^'\"]+)['\"]\)",
            re.MULTILINE
        ),

        # Function definitions
        "function": re.compile(
            r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)",
            re.MULTILINE
        ),
        "arrow_function": re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",
            re.MULTILINE
        ),
        "arrow_function_short": re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\w+\s*=>",
            re.MULTILINE
        ),

        # Class definitions
        "class": re.compile(
            r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([^{]+))?",
            re.MULTILINE
        ),

        # Interface/Type definitions
        "interface": re.compile(
            r"^(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+([^{]+))?",
            re.MULTILINE
        ),
        "type_alias": re.compile(
            r"^(?:export\s+)?type\s+(\w+)\s*=",
            re.MULTILINE
        ),

        # React components
        "react_component_function": re.compile(
            r"^(?:export\s+)?(?:default\s+)?function\s+([A-Z]\w+)\s*\(",
            re.MULTILINE
        ),
        "react_component_arrow": re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+([A-Z]\w+)\s*(?::\s*(?:React\.)?FC[^=]*)?=\s*(?:\([^)]*\)|[^=])\s*=>",
            re.MULTILINE
        ),

        # Hooks (custom hooks start with 'use')
        "custom_hook": re.compile(
            r"^(?:export\s+)?(?:const|function)\s+(use[A-Z]\w+)",
            re.MULTILINE
        ),

        # API routes (Next.js patterns)
        "api_handler": re.compile(
            r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?:handler|GET|POST|PUT|DELETE|PATCH)\s*\(",
            re.MULTILINE
        ),
        "api_route_export": re.compile(
            r"^export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH)\s*\(",
            re.MULTILINE
        ),

        # Constants
        "constant": re.compile(
            r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]+)\s*=",
            re.MULTILINE
        ),

        # Method calls (for edge detection)
        "method_call": re.compile(
            r"(?:await\s+)?(\w+(?:\.\w+)*)\s*\(",
        ),
    }

    def __init__(self, repo_root: str = None):
        """
        Initialize the analyzer.

        Args:
            repo_root: Root directory of the repository (for relative paths)
        """
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def analyze_file(self, file_path: str) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """
        Analyze a single TypeScript/JavaScript file.

        Args:
            file_path: Path to the TS/JS file

        Returns:
            Tuple of (nodes, edges) extracted from the file
        """
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
        except (IOError, UnicodeDecodeError) as e:
            print(f"Warning: Could not read {file_path}: {e}")
            return nodes, edges

        # Make path relative to repo root
        rel_path = self._relative_path(file_path)

        # Remove comments to avoid false positives
        source_no_comments = self._remove_comments(source)

        # Create file node
        file_node = ArchNode.create(
            type=NodeType.FILE,
            name=Path(file_path).name,
            file_path=rel_path,
            line_start=1,
            line_end=len(source.splitlines()),
            metadata={
                "size_bytes": len(source),
                "lines": len(source.splitlines()),
                "extension": Path(file_path).suffix,
            },
        )
        nodes.append(file_node)

        # Extract imports
        import_edges = self._extract_imports(source_no_comments, file_node.id)
        edges.extend(import_edges)

        # Extract classes
        class_nodes, class_edges = self._extract_classes(
            source, source_no_comments, rel_path, file_node.id
        )
        nodes.extend(class_nodes)
        edges.extend(class_edges)

        # Extract interfaces
        interface_nodes, interface_edges = self._extract_interfaces(
            source, rel_path, file_node.id
        )
        nodes.extend(interface_nodes)
        edges.extend(interface_edges)

        # Extract React components
        component_nodes, component_edges = self._extract_components(
            source, source_no_comments, rel_path, file_node.id
        )
        nodes.extend(component_nodes)
        edges.extend(component_edges)

        # Extract hooks
        hook_nodes, hook_edges = self._extract_hooks(
            source, source_no_comments, rel_path, file_node.id
        )
        nodes.extend(hook_nodes)
        edges.extend(hook_edges)

        # Extract regular functions (that aren't components or hooks)
        func_nodes, func_edges = self._extract_functions(
            source, source_no_comments, rel_path, file_node.id,
            exclude_names={n.name for n in component_nodes + hook_nodes}
        )
        nodes.extend(func_nodes)
        edges.extend(func_edges)

        # Extract API routes
        api_nodes, api_edges = self._extract_api_routes(
            source, rel_path, file_node.id
        )
        nodes.extend(api_nodes)
        edges.extend(api_edges)

        # Extract constants
        const_nodes, const_edges = self._extract_constants(
            source, rel_path, file_node.id
        )
        nodes.extend(const_nodes)
        edges.extend(const_edges)

        return nodes, edges

    def _relative_path(self, file_path: str) -> str:
        """Convert absolute path to relative path from repo root."""
        try:
            return str(Path(file_path).relative_to(self.repo_root))
        except ValueError:
            return file_path

    def _remove_comments(self, source: str) -> str:
        """Remove comments from source code."""
        # Remove single-line comments
        source = re.sub(r"//.*$", "", source, flags=re.MULTILINE)
        # Remove multi-line comments
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        return source

    def _find_line_number(self, source: str, match_pos: int) -> int:
        """Find line number for a position in source."""
        return source[:match_pos].count("\n") + 1

    def _extract_imports(self, source: str, file_node_id: str) -> List[ArchEdge]:
        """Extract import statements."""
        edges: List[ArchEdge] = []
        seen_imports: Set[str] = set()

        # Default imports
        for match in self.PATTERNS["import_default"].finditer(source):
            module = match.group(2)
            if module not in seen_imports:
                seen_imports.add(module)
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=f"module_{module.replace('/', '_').replace('.', '_').replace('@', '')}",
                        type=EdgeType.IMPORTS,
                        metadata={"module": module, "import_type": "default"},
                    )
                )

        # Named imports
        for match in self.PATTERNS["import_named"].finditer(source):
            module = match.group(2)
            names = [n.strip().split(" as ")[0] for n in match.group(1).split(",")]
            if module not in seen_imports:
                seen_imports.add(module)
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=f"module_{module.replace('/', '_').replace('.', '_').replace('@', '')}",
                        type=EdgeType.IMPORTS,
                        metadata={"module": module, "names": names, "import_type": "named"},
                    )
                )

        # Namespace imports
        for match in self.PATTERNS["import_all"].finditer(source):
            module = match.group(2)
            if module not in seen_imports:
                seen_imports.add(module)
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=f"module_{module.replace('/', '_').replace('.', '_').replace('@', '')}",
                        type=EdgeType.IMPORTS,
                        metadata={"module": module, "alias": match.group(1), "import_type": "namespace"},
                    )
                )

        # Require statements
        for match in self.PATTERNS["require"].finditer(source):
            module = match.group(2)
            if module not in seen_imports:
                seen_imports.add(module)
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=f"module_{module.replace('/', '_').replace('.', '_').replace('@', '')}",
                        type=EdgeType.IMPORTS,
                        metadata={"module": module, "import_type": "require"},
                    )
                )

        return edges

    def _extract_classes(
        self, source: str, source_no_comments: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract class definitions."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for match in self.PATTERNS["class"].finditer(source_no_comments):
            class_name = match.group(1)
            extends = match.group(2)
            implements = match.group(3)

            line = self._find_line_number(source, match.start())

            class_node = ArchNode.create(
                type=NodeType.CLASS,
                name=class_name,
                file_path=file_path,
                line_start=line,
                metadata={
                    "extends": extends,
                    "implements": [i.strip() for i in implements.split(",")] if implements else [],
                },
            )
            nodes.append(class_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=class_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

            if extends:
                edges.append(
                    ArchEdge.create(
                        source=class_node.id,
                        target=f"class_{extends}",
                        type=EdgeType.EXTENDS,
                    )
                )

        return nodes, edges

    def _extract_interfaces(
        self, source: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract interface definitions."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for match in self.PATTERNS["interface"].finditer(source):
            interface_name = match.group(1)
            extends = match.group(2)

            line = self._find_line_number(source, match.start())

            interface_node = ArchNode.create(
                type=NodeType.MODULE,  # Using MODULE for interfaces
                name=interface_name,
                file_path=file_path,
                line_start=line,
                metadata={
                    "kind": "interface",
                    "extends": [e.strip() for e in extends.split(",")] if extends else [],
                },
            )
            nodes.append(interface_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=interface_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

        # Type aliases
        for match in self.PATTERNS["type_alias"].finditer(source):
            type_name = match.group(1)
            line = self._find_line_number(source, match.start())

            type_node = ArchNode.create(
                type=NodeType.MODULE,
                name=type_name,
                file_path=file_path,
                line_start=line,
                metadata={"kind": "type_alias"},
            )
            nodes.append(type_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=type_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

        return nodes, edges

    def _extract_components(
        self, source: str, source_no_comments: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract React components (functions starting with uppercase)."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []
        seen_names: Set[str] = set()

        # Function components
        for match in self.PATTERNS["react_component_function"].finditer(source_no_comments):
            name = match.group(1)
            if name not in seen_names:
                seen_names.add(name)
                line = self._find_line_number(source, match.start())

                component_node = ArchNode.create(
                    type=NodeType.COMPONENT,
                    name=name,
                    file_path=file_path,
                    line_start=line,
                    metadata={"component_type": "function"},
                )
                nodes.append(component_node)

                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=component_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

        # Arrow function components
        for match in self.PATTERNS["react_component_arrow"].finditer(source_no_comments):
            name = match.group(1)
            if name not in seen_names:
                seen_names.add(name)
                line = self._find_line_number(source, match.start())

                component_node = ArchNode.create(
                    type=NodeType.COMPONENT,
                    name=name,
                    file_path=file_path,
                    line_start=line,
                    metadata={"component_type": "arrow"},
                )
                nodes.append(component_node)

                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=component_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

        return nodes, edges

    def _extract_hooks(
        self, source: str, source_no_comments: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract custom React hooks (functions starting with 'use')."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for match in self.PATTERNS["custom_hook"].finditer(source_no_comments):
            hook_name = match.group(1)
            line = self._find_line_number(source, match.start())

            hook_node = ArchNode.create(
                type=NodeType.HOOK,
                name=hook_name,
                file_path=file_path,
                line_start=line,
            )
            nodes.append(hook_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=hook_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

        return nodes, edges

    def _extract_functions(
        self, source: str, source_no_comments: str, file_path: str, file_node_id: str,
        exclude_names: Set[str] = None
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract regular functions."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []
        exclude_names = exclude_names or set()
        seen_names: Set[str] = set()

        # Regular functions
        for match in self.PATTERNS["function"].finditer(source_no_comments):
            name = match.group(1)
            if name not in exclude_names and name not in seen_names:
                seen_names.add(name)
                line = self._find_line_number(source, match.start())

                func_node = ArchNode.create(
                    type=NodeType.FUNCTION,
                    name=name,
                    file_path=file_path,
                    line_start=line,
                    metadata={"function_type": "regular"},
                )
                nodes.append(func_node)

                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=func_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

        # Arrow functions
        for pattern in ["arrow_function", "arrow_function_short"]:
            for match in self.PATTERNS[pattern].finditer(source_no_comments):
                name = match.group(1)
                # Skip if starts with uppercase (component) or 'use' (hook)
                if name[0].isupper() or name.startswith("use"):
                    continue
                if name not in exclude_names and name not in seen_names:
                    seen_names.add(name)
                    line = self._find_line_number(source, match.start())

                    func_node = ArchNode.create(
                        type=NodeType.FUNCTION,
                        name=name,
                        file_path=file_path,
                        line_start=line,
                        metadata={"function_type": "arrow"},
                    )
                    nodes.append(func_node)

                    edges.append(
                        ArchEdge.create(
                            source=file_node_id,
                            target=func_node.id,
                            type=EdgeType.CONTAINS,
                        )
                    )

        return nodes, edges

    def _extract_api_routes(
        self, source: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract API route handlers (Next.js App Router pattern)."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        # Check if this is likely an API route file
        if "/api/" not in file_path and "/route." not in file_path:
            return nodes, edges

        for match in self.PATTERNS["api_route_export"].finditer(source):
            method = match.group(1)
            line = self._find_line_number(source, match.start())

            api_node = ArchNode.create(
                type=NodeType.API_ROUTE,
                name=f"{method}",
                file_path=file_path,
                line_start=line,
                metadata={"http_method": method},
            )
            nodes.append(api_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=api_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

        return nodes, edges

    def _extract_constants(
        self, source: str, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract module-level constants (UPPERCASE names)."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for match in self.PATTERNS["constant"].finditer(source):
            const_name = match.group(1)
            line = self._find_line_number(source, match.start())

            const_node = ArchNode.create(
                type=NodeType.CONSTANT,
                name=const_name,
                file_path=file_path,
                line_start=line,
            )
            nodes.append(const_node)

            edges.append(
                ArchEdge.create(
                    source=file_node_id,
                    target=const_node.id,
                    type=EdgeType.CONTAINS,
                )
            )

        return nodes, edges


def analyze_typescript_file(file_path: str, repo_root: str = None) -> Tuple[List[ArchNode], List[ArchEdge]]:
    """
    Convenience function to analyze a single TypeScript/JavaScript file.

    Args:
        file_path: Path to the TS/JS file
        repo_root: Root directory of the repository

    Returns:
        Tuple of (nodes, edges) extracted from the file
    """
    analyzer = TypeScriptAnalyzer(repo_root)
    return analyzer.analyze_file(file_path)


if __name__ == "__main__":
    # Test with a sample file
    import json

    repo_root = Path(__file__).resolve().parent.parent.parent
    test_file = str(repo_root / "admin.contextdna.io/components/dashboard/views/injection-focus-view.tsx")
    if os.path.exists(test_file):
        nodes, edges = analyze_typescript_file(test_file)
        print(f"Found {len(nodes)} nodes and {len(edges)} edges")
        print("\nNodes:")
        for node in nodes[:10]:
            print(f"  {node.type.value}: {node.name}")
        print("\nEdges:")
        for edge in edges[:10]:
            print(f"  {edge.type.value}: {edge.source[:20]}... -> {edge.target[:20]}...")
    else:
        print(f"Test file not found: {test_file}")
