"""
Python AST Analyzer for Architectural Awareness

Uses Python's built-in ast module (ZERO external dependencies) to parse
Python files and extract structural information like classes, functions,
imports, and their relationships.
"""

import ast
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


class PythonASTAnalyzer:
    """
    Analyzes Python files using the AST module to extract
    structural information for the architecture graph.
    """

    def __init__(self, repo_root: str = None):
        """
        Initialize the analyzer.

        Args:
            repo_root: Root directory of the repository (for relative paths)
        """
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def analyze_file(self, file_path: str) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """
        Analyze a single Python file.

        Args:
            file_path: Path to the Python file

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

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            print(f"Warning: Syntax error in {file_path}: {e}")
            return nodes, edges

        # Make path relative to repo root
        rel_path = self._relative_path(file_path)

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
            },
        )
        nodes.append(file_node)

        # Extract imports
        import_nodes, import_edges = self._extract_imports(tree, rel_path, file_node.id)
        nodes.extend(import_nodes)
        edges.extend(import_edges)

        # Extract classes
        class_nodes, class_edges = self._extract_classes(tree, rel_path, file_node.id)
        nodes.extend(class_nodes)
        edges.extend(class_edges)

        # Extract top-level functions
        func_nodes, func_edges = self._extract_functions(tree, rel_path, file_node.id)
        nodes.extend(func_nodes)
        edges.extend(func_edges)

        # Extract constants (uppercase module-level variables)
        const_nodes, const_edges = self._extract_constants(tree, rel_path, file_node.id)
        nodes.extend(const_nodes)
        edges.extend(const_edges)

        return nodes, edges

    def _relative_path(self, file_path: str) -> str:
        """Convert absolute path to relative path from repo root."""
        try:
            return str(Path(file_path).relative_to(self.repo_root))
        except ValueError:
            return file_path

    def _extract_imports(
        self, tree: ast.AST, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract import statements."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # Create edge from file to imported module
                    # We don't create nodes for imports - they'll be resolved later
                    edges.append(
                        ArchEdge.create(
                            source=file_node_id,
                            target=f"module_{alias.name.replace('.', '_')}",
                            type=EdgeType.IMPORTS,
                            metadata={
                                "module": alias.name,
                                "alias": alias.asname,
                            },
                        )
                    )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    edges.append(
                        ArchEdge.create(
                            source=file_node_id,
                            target=f"module_{module.replace('.', '_')}_{alias.name}",
                            type=EdgeType.IMPORTS,
                            metadata={
                                "module": module,
                                "name": alias.name,
                                "alias": alias.asname,
                            },
                        )
                    )

        return nodes, edges

    def _extract_classes(
        self, tree: ast.AST, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract class definitions."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_node = ArchNode.create(
                    type=NodeType.CLASS,
                    name=node.name,
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    metadata={
                        "docstring": ast.get_docstring(node) or "",
                        "decorators": [self._decorator_name(d) for d in node.decorator_list],
                        "bases": [self._name_from_node(b) for b in node.bases],
                    },
                )
                nodes.append(class_node)

                # Edge: file contains class
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=class_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

                # Extract base class relationships
                for base in node.bases:
                    base_name = self._name_from_node(base)
                    if base_name:
                        edges.append(
                            ArchEdge.create(
                                source=class_node.id,
                                target=f"class_{base_name}",
                                type=EdgeType.EXTENDS,
                                metadata={"base_class": base_name},
                            )
                        )

                # Extract methods
                method_nodes, method_edges = self._extract_methods(
                    node, file_path, class_node.id
                )
                nodes.extend(method_nodes)
                edges.extend(method_edges)

        return nodes, edges

    def _extract_methods(
        self, class_node: ast.ClassDef, file_path: str, class_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract method definitions from a class."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for node in ast.iter_child_nodes(class_node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_async = isinstance(node, ast.AsyncFunctionDef)

                # Determine method type
                method_type = "method"
                if node.name.startswith("__") and node.name.endswith("__"):
                    method_type = "dunder"
                elif node.name.startswith("_"):
                    method_type = "private"

                # Check for property/staticmethod/classmethod decorators
                for dec in node.decorator_list:
                    dec_name = self._decorator_name(dec)
                    if dec_name in ("property", "staticmethod", "classmethod"):
                        method_type = dec_name

                method_node = ArchNode.create(
                    type=NodeType.FUNCTION,
                    name=f"{class_node.name}.{node.name}",
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    metadata={
                        "docstring": ast.get_docstring(node) or "",
                        "decorators": [self._decorator_name(d) for d in node.decorator_list],
                        "args": self._extract_args(node),
                        "is_async": is_async,
                        "method_type": method_type,
                    },
                )
                nodes.append(method_node)

                # Edge: class contains method
                edges.append(
                    ArchEdge.create(
                        source=class_node_id,
                        target=method_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

                # Extract function calls within the method
                call_edges = self._extract_calls(node, method_node.id)
                edges.extend(call_edges)

        return nodes, edges

    def _extract_functions(
        self, tree: ast.AST, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract top-level function definitions."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_async = isinstance(node, ast.AsyncFunctionDef)

                func_node = ArchNode.create(
                    type=NodeType.FUNCTION,
                    name=node.name,
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    metadata={
                        "docstring": ast.get_docstring(node) or "",
                        "decorators": [self._decorator_name(d) for d in node.decorator_list],
                        "args": self._extract_args(node),
                        "is_async": is_async,
                    },
                )
                nodes.append(func_node)

                # Edge: file contains function
                edges.append(
                    ArchEdge.create(
                        source=file_node_id,
                        target=func_node.id,
                        type=EdgeType.CONTAINS,
                    )
                )

                # Check for common patterns
                for dec in node.decorator_list:
                    dec_name = self._decorator_name(dec)
                    # FastAPI/Flask route decorators
                    if dec_name in ("get", "post", "put", "delete", "route", "api_route"):
                        func_node.type = NodeType.API_ROUTE
                    # Hook decorators
                    if "hook" in dec_name.lower():
                        func_node.type = NodeType.HOOK

                # Extract function calls
                call_edges = self._extract_calls(node, func_node.id)
                edges.extend(call_edges)

        return nodes, edges

    def _extract_constants(
        self, tree: ast.AST, file_path: str, file_node_id: str
    ) -> Tuple[List[ArchNode], List[ArchEdge]]:
        """Extract module-level constants (UPPERCASE names)."""
        nodes: List[ArchNode] = []
        edges: List[ArchEdge] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        const_node = ArchNode.create(
                            type=NodeType.CONSTANT,
                            name=target.id,
                            file_path=file_path,
                            line_start=node.lineno,
                            line_end=node.end_lineno or node.lineno,
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

    def _extract_calls(
        self, func_node: ast.AST, caller_id: str
    ) -> List[ArchEdge]:
        """Extract function/method calls within a function body."""
        edges: List[ArchEdge] = []
        seen_calls: Set[str] = set()

        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                call_name = self._call_name(node)
                if call_name and call_name not in seen_calls:
                    seen_calls.add(call_name)
                    edges.append(
                        ArchEdge.create(
                            source=caller_id,
                            target=f"function_{call_name.replace('.', '_')}",
                            type=EdgeType.CALLS,
                            metadata={"called_function": call_name},
                        )
                    )

        return edges

    def _decorator_name(self, decorator: ast.expr) -> str:
        """Extract decorator name."""
        if isinstance(decorator, ast.Name):
            return decorator.id
        elif isinstance(decorator, ast.Attribute):
            return f"{self._name_from_node(decorator.value)}.{decorator.attr}"
        elif isinstance(decorator, ast.Call):
            return self._decorator_name(decorator.func)
        return ""

    def _name_from_node(self, node: ast.expr) -> str:
        """Extract name from an AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            value_name = self._name_from_node(node.value)
            if value_name:
                return f"{value_name}.{node.attr}"
            return node.attr
        elif isinstance(node, ast.Subscript):
            return self._name_from_node(node.value)
        return ""

    def _call_name(self, node: ast.Call) -> str:
        """Extract the name of a function call."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            value_name = self._name_from_node(node.func.value)
            if value_name:
                return f"{value_name}.{node.func.attr}"
            return node.func.attr
        return ""

    def _extract_args(self, func: ast.FunctionDef) -> List[Dict[str, Any]]:
        """Extract function arguments with type hints."""
        args = []
        for arg in func.args.args:
            arg_info = {"name": arg.arg}
            if arg.annotation:
                arg_info["type"] = self._annotation_to_str(arg.annotation)
            args.append(arg_info)
        return args

    def _annotation_to_str(self, annotation: ast.expr) -> str:
        """Convert type annotation to string."""
        if isinstance(annotation, ast.Name):
            return annotation.id
        elif isinstance(annotation, ast.Constant):
            return str(annotation.value)
        elif isinstance(annotation, ast.Subscript):
            base = self._annotation_to_str(annotation.value)
            if isinstance(annotation.slice, ast.Tuple):
                args = ", ".join(self._annotation_to_str(e) for e in annotation.slice.elts)
            else:
                args = self._annotation_to_str(annotation.slice)
            return f"{base}[{args}]"
        elif isinstance(annotation, ast.Attribute):
            return self._name_from_node(annotation)
        return ""


def analyze_python_file(file_path: str, repo_root: str = None) -> Tuple[List[ArchNode], List[ArchEdge]]:
    """
    Convenience function to analyze a single Python file.

    Args:
        file_path: Path to the Python file
        repo_root: Root directory of the repository

    Returns:
        Tuple of (nodes, edges) extracted from the file
    """
    analyzer = PythonASTAnalyzer(repo_root)
    return analyzer.analyze_file(file_path)


if __name__ == "__main__":
    # Test with this file
    import json

    nodes, edges = analyze_python_file(__file__)
    print(f"Found {len(nodes)} nodes and {len(edges)} edges")
    print("\nNodes:")
    for node in nodes[:5]:
        print(f"  {node.type.value}: {node.name}")
    print("\nEdges:")
    for edge in edges[:5]:
        print(f"  {edge.type.value}: {edge.source} -> {edge.target}")
