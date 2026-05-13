#!/usr/bin/env python3
"""
Synaptic's Codebase Vision - Understanding Code Structure

A lightweight code analyzer that extracts structural information from codebases:
- Function and class definitions
- Import statements
- File-level documentation
- Module relationships

Philosophy:
- Lightweight: Only extracts names and signatures, not full code
- Fast: Uses AST for Python, regex patterns for others
- Privacy-respecting: Never stores sensitive string literals
- Queryable: "Where is X defined?", "What imports Y?"

Storage: ~/.context-dna/codebase_vision.db
Integrates with: local_file_scanner.py (file discovery)

Usage:
    # Analyze a project
    python codebase_vision.py analyze /path/to/project

    # Find where a function is defined
    python codebase_vision.py find "function_name"

    # Show project structure
    python codebase_vision.py structure /path/to/project

    # Get file outline
    python codebase_vision.py outline /path/to/file.py
"""

import os
import sys
import ast
import re
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field
from enum import Enum
import json

# Setup logging
logger = logging.getLogger(__name__)


# =============================================================================
# CODE ENTITY TYPES
# =============================================================================

class EntityType(str, Enum):
    """Types of code entities we track."""
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    IMPORT = "import"
    CONSTANT = "constant"
    VARIABLE = "variable"
    MODULE = "module"
    DECORATOR = "decorator"


@dataclass
class CodeEntity:
    """A single code entity (function, class, import, etc.)."""
    name: str
    entity_type: EntityType
    file_path: str
    line_number: int
    signature: Optional[str] = None  # For functions: "def foo(a, b) -> int"
    parent: Optional[str] = None     # For methods: class name
    docstring: Optional[str] = None  # First line of docstring (truncated)
    decorators: List[str] = field(default_factory=list)


@dataclass
class FileOutline:
    """Structural outline of a single file."""
    path: str
    language: str
    module_docstring: Optional[str]
    imports: List[CodeEntity]
    classes: List[CodeEntity]
    functions: List[CodeEntity]
    constants: List[CodeEntity]
    analyzed_at: datetime


@dataclass
class ProjectStructure:
    """High-level structure of a project."""
    root_path: str
    name: str
    language_breakdown: Dict[str, int]  # Extension -> file count
    total_files: int
    total_functions: int
    total_classes: int
    entry_points: List[str]  # main.py, index.js, etc.
    key_modules: List[str]   # Most-imported files


# =============================================================================
# PYTHON AST ANALYZER
# =============================================================================

class PythonAnalyzer:
    """AST-based analyzer for Python files."""

    def analyze(self, file_path: Path) -> Optional[FileOutline]:
        """Analyze a Python file and extract its structure."""
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(file_path))
        except (SyntaxError, UnicodeDecodeError):
            return None

        outline = FileOutline(
            path=str(file_path),
            language="python",
            module_docstring=ast.get_docstring(tree),
            imports=[],
            classes=[],
            functions=[],
            constants=[],
            analyzed_at=datetime.now()
        )

        # Truncate module docstring
        if outline.module_docstring:
            outline.module_docstring = outline.module_docstring[:200]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    outline.imports.append(CodeEntity(
                        name=alias.name,
                        entity_type=EntityType.IMPORT,
                        file_path=str(file_path),
                        line_number=node.lineno
                    ))

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    outline.imports.append(CodeEntity(
                        name=f"{module}.{alias.name}" if module else alias.name,
                        entity_type=EntityType.IMPORT,
                        file_path=str(file_path),
                        line_number=node.lineno
                    ))

            elif isinstance(node, ast.ClassDef):
                docstring = ast.get_docstring(node)
                decorators = [self._get_decorator_name(d) for d in node.decorator_list]

                outline.classes.append(CodeEntity(
                    name=node.name,
                    entity_type=EntityType.CLASS,
                    file_path=str(file_path),
                    line_number=node.lineno,
                    signature=self._class_signature(node),
                    docstring=docstring[:100] if docstring else None,
                    decorators=decorators
                ))

                # Extract methods
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                        method_docstring = ast.get_docstring(item)
                        method_decorators = [self._get_decorator_name(d) for d in item.decorator_list]

                        outline.functions.append(CodeEntity(
                            name=item.name,
                            entity_type=EntityType.METHOD,
                            file_path=str(file_path),
                            line_number=item.lineno,
                            signature=self._function_signature(item),
                            parent=node.name,
                            docstring=method_docstring[:100] if method_docstring else None,
                            decorators=method_decorators
                        ))

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only top-level functions (not methods)
                if self._is_top_level(tree, node):
                    docstring = ast.get_docstring(node)
                    decorators = [self._get_decorator_name(d) for d in node.decorator_list]

                    outline.functions.append(CodeEntity(
                        name=node.name,
                        entity_type=EntityType.FUNCTION,
                        file_path=str(file_path),
                        line_number=node.lineno,
                        signature=self._function_signature(node),
                        docstring=docstring[:100] if docstring else None,
                        decorators=decorators
                    ))

            elif isinstance(node, ast.Assign):
                # Top-level constants (UPPER_CASE names)
                if self._is_top_level(tree, node):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id.isupper():
                            outline.constants.append(CodeEntity(
                                name=target.id,
                                entity_type=EntityType.CONSTANT,
                                file_path=str(file_path),
                                line_number=node.lineno
                            ))

        return outline

    def _is_top_level(self, tree: ast.Module, node: ast.AST) -> bool:
        """Check if node is at module level."""
        return node in tree.body

    def _function_signature(self, node: ast.FunctionDef) -> str:
        """Extract function signature."""
        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                arg_str += f": {ast.unparse(arg.annotation)}"
            args.append(arg_str)

        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        sig = f"{prefix} {node.name}({', '.join(args)})"

        if node.returns:
            sig += f" -> {ast.unparse(node.returns)}"

        return sig

    def _class_signature(self, node: ast.ClassDef) -> str:
        """Extract class signature with bases."""
        bases = [ast.unparse(b) for b in node.bases]
        if bases:
            return f"class {node.name}({', '.join(bases)})"
        return f"class {node.name}"

    def _get_decorator_name(self, node: ast.expr) -> str:
        """Get decorator name from AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Call):
            return self._get_decorator_name(node.func)
        elif isinstance(node, ast.Attribute):
            return f"{self._get_decorator_name(node.value)}.{node.attr}"
        return "unknown"


# =============================================================================
# JAVASCRIPT/TYPESCRIPT ANALYZER (Regex-based)
# =============================================================================

class JavaScriptAnalyzer:
    """Regex-based analyzer for JavaScript/TypeScript."""

    FUNCTION_PATTERNS = [
        # function name()
        re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)', re.MULTILINE),
        # const name = function
        re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function\s*\(', re.MULTILINE),
        # const name = () =>
        re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>', re.MULTILINE),
        # const name = async () =>
        re.compile(r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*async\s+\([^)]*\)\s*=>', re.MULTILINE),
    ]

    CLASS_PATTERN = re.compile(r'^(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?\s*\{', re.MULTILINE)

    IMPORT_PATTERNS = [
        re.compile(r'^import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE),
        re.compile(r'^import\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE),
        re.compile(r'require\([\'"]([^\'"]+)[\'"]\)', re.MULTILINE),
    ]

    def analyze(self, file_path: Path) -> Optional[FileOutline]:
        """Analyze a JavaScript/TypeScript file."""
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
        except UnicodeDecodeError:
            return None

        ext = file_path.suffix.lower()
        lang = "typescript" if ext in (".ts", ".tsx") else "javascript"

        outline = FileOutline(
            path=str(file_path),
            language=lang,
            module_docstring=self._extract_file_comment(source),
            imports=[],
            classes=[],
            functions=[],
            constants=[],
            analyzed_at=datetime.now()
        )

        # Find imports
        for pattern in self.IMPORT_PATTERNS:
            for match in pattern.finditer(source):
                outline.imports.append(CodeEntity(
                    name=match.group(1),
                    entity_type=EntityType.IMPORT,
                    file_path=str(file_path),
                    line_number=source[:match.start()].count('\n') + 1
                ))

        # Find classes
        for match in self.CLASS_PATTERN.finditer(source):
            class_name = match.group(1)
            extends = match.group(2)
            sig = f"class {class_name}"
            if extends:
                sig += f" extends {extends}"

            outline.classes.append(CodeEntity(
                name=class_name,
                entity_type=EntityType.CLASS,
                file_path=str(file_path),
                line_number=source[:match.start()].count('\n') + 1,
                signature=sig
            ))

        # Find functions
        for pattern in self.FUNCTION_PATTERNS:
            for match in pattern.finditer(source):
                func_name = match.group(1)
                # Skip if already found (patterns can overlap)
                if not any(f.name == func_name for f in outline.functions):
                    outline.functions.append(CodeEntity(
                        name=func_name,
                        entity_type=EntityType.FUNCTION,
                        file_path=str(file_path),
                        line_number=source[:match.start()].count('\n') + 1,
                        signature=match.group(0)[:100]  # Truncate long signatures
                    ))

        return outline

    def _extract_file_comment(self, source: str) -> Optional[str]:
        """Extract file-level JSDoc comment."""
        match = re.match(r'/\*\*\s*\n(.*?)\*/', source, re.DOTALL)
        if match:
            return match.group(1).strip()[:200]
        return None


# =============================================================================
# CODEBASE VISION DATABASE
# =============================================================================

class CodebaseVision:
    """
    Synaptic's vision of codebase structure.

    Stores and queries code entity information for understanding
    project architecture and answering structural questions.
    """

    ANALYZERS = {
        ".py": PythonAnalyzer(),
        ".js": JavaScriptAnalyzer(),
        ".ts": JavaScriptAnalyzer(),
        ".jsx": JavaScriptAnalyzer(),
        ".tsx": JavaScriptAnalyzer(),
    }

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize with SQLite storage."""
        if db_path is None:
            config_dir = Path.home() / ".context-dna"
            config_dir.mkdir(exist_ok=True)
            db_path = config_dir / "codebase_vision.db"

        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        try:
            from memory.db_utils import connect_wal
            conn = connect_wal(str(self.db_path))

            conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                file_path TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                signature TEXT,
                parent TEXT,
                docstring TEXT,
                decorators TEXT,
                project_root TEXT,
                analyzed_at TEXT NOT NULL,
                UNIQUE(file_path, name, entity_type, line_number)
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                language TEXT NOT NULL,
                module_docstring TEXT,
                import_count INTEGER,
                class_count INTEGER,
                function_count INTEGER,
                constant_count INTEGER,
                project_root TEXT,
                analyzed_at TEXT NOT NULL,
                content_hash TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                root_path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                total_files INTEGER,
                total_functions INTEGER,
                total_classes INTEGER,
                language_breakdown TEXT,
                entry_points TEXT,
                analyzed_at TEXT NOT NULL
            );

            -- Indexes for fast queries
            CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
            CREATE INDEX IF NOT EXISTS idx_entities_file ON entities(file_path);
            CREATE INDEX IF NOT EXISTS idx_entities_project ON entities(project_root);
            CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_root);
        """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error initializing database: {e}")

    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection."""
        try:
            from memory.db_utils import connect_wal
            return connect_wal(str(self.db_path))
        except Exception as e:
            logger.error(f"Error getting connection: {e}")
            return None

    def analyze_file(self, file_path: Path, project_root: Optional[Path] = None) -> Optional[FileOutline]:
        """Analyze a single file and store results."""
        ext = file_path.suffix.lower()
        analyzer = self.ANALYZERS.get(ext)

        if not analyzer:
            return None

        outline = analyzer.analyze(file_path)
        if not outline:
            return None

        # Store in database
        conn = self._get_conn()

        # Calculate content hash for change detection
        try:
            content = file_path.read_bytes()
            content_hash = hashlib.md5(content).hexdigest()
        except (OSError, PermissionError):
            content_hash = None

        # Check if file unchanged
        existing = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?",
            (str(file_path),)
        ).fetchone()

        if existing and existing["content_hash"] == content_hash:
            conn.close()
            return outline  # Unchanged, skip reprocessing

        # Store file record
        conn.execute("""
            INSERT OR REPLACE INTO files
            (path, language, module_docstring, import_count, class_count,
             function_count, constant_count, project_root, analyzed_at, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(file_path),
            outline.language,
            outline.module_docstring,
            len(outline.imports),
            len(outline.classes),
            len(outline.functions),
            len(outline.constants),
            str(project_root) if project_root else None,
            outline.analyzed_at.isoformat(),
            content_hash
        ))

        # Clear old entities for this file
        conn.execute("DELETE FROM entities WHERE file_path = ?", (str(file_path),))

        # Store entities
        all_entities = outline.imports + outline.classes + outline.functions + outline.constants

        for entity in all_entities:
            conn.execute("""
                INSERT OR REPLACE INTO entities
                (name, entity_type, file_path, line_number, signature,
                 parent, docstring, decorators, project_root, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entity.name,
                entity.entity_type.value,
                entity.file_path,
                entity.line_number,
                entity.signature,
                entity.parent,
                entity.docstring,
                json.dumps(entity.decorators) if entity.decorators else None,
                str(project_root) if project_root else None,
                outline.analyzed_at.isoformat()
            ))

        conn.commit()
        conn.close()
        return outline

    def analyze_project(
        self,
        project_root: Path,
        max_files: int = 1000,
        progress_callback=None
    ) -> ProjectStructure:
        """Analyze an entire project."""
        project_root = project_root.resolve()

        # Collect files
        files_by_ext: Dict[str, List[Path]] = {}
        for ext in self.ANALYZERS.keys():
            pattern = f"**/*{ext}"
            for f in project_root.glob(pattern):
                if self._should_skip(f):
                    continue
                files_by_ext.setdefault(ext, []).append(f)

        # Analyze files
        total_functions = 0
        total_classes = 0
        file_count = 0

        all_files = []
        for files in files_by_ext.values():
            all_files.extend(files)

        # Limit to max_files
        all_files = all_files[:max_files]

        for i, file_path in enumerate(all_files):
            outline = self.analyze_file(file_path, project_root)
            if outline:
                total_functions += len(outline.functions)
                total_classes += len(outline.classes)
                file_count += 1

            if progress_callback and i % 50 == 0:
                progress_callback(i, str(file_path))

        # Detect entry points
        entry_points = []
        for pattern in ["main.py", "index.js", "index.ts", "app.py", "server.py", "cli.py"]:
            for f in project_root.glob(f"**/{pattern}"):
                if not self._should_skip(f):
                    entry_points.append(str(f.relative_to(project_root)))

        # Store project record
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO projects
            (root_path, name, total_files, total_functions, total_classes,
             language_breakdown, entry_points, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(project_root),
            project_root.name,
            file_count,
            total_functions,
            total_classes,
            json.dumps({ext: len(files) for ext, files in files_by_ext.items()}),
            json.dumps(entry_points[:10]),  # Top 10
            datetime.now().isoformat()
        ))
        conn.commit()
        conn.close()

        return ProjectStructure(
            root_path=str(project_root),
            name=project_root.name,
            language_breakdown={ext: len(files) for ext, files in files_by_ext.items()},
            total_files=file_count,
            total_functions=total_functions,
            total_classes=total_classes,
            entry_points=entry_points[:10],
            key_modules=[]  # TODO: Calculate from import graph
        )

    def _should_skip(self, path: Path) -> bool:
        """Check if path should be skipped."""
        skip_dirs = {
            "node_modules", "__pycache__", ".git", "venv", ".venv",
            "env", "dist", "build", ".tox", ".pytest_cache", ".mypy_cache",
            "site-packages", ".eggs", "*.egg-info"
        }

        for part in path.parts:
            if part in skip_dirs or part.endswith(".egg-info"):
                return True

        return False

    def find_definition(self, name: str, entity_type: Optional[str] = None) -> List[Dict]:
        """Find where a function/class/etc is defined."""
        conn = self._get_conn()

        query = "SELECT * FROM entities WHERE name LIKE ?"
        params = [f"%{name}%"]

        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)

        query += " ORDER BY name, file_path LIMIT 50"

        results = conn.execute(query, params).fetchall()
        conn.close()

        return [dict(r) for r in results]

    def find_usages(self, name: str, project_root: Optional[str] = None) -> List[Dict]:
        """Find where a name is imported/used."""
        conn = self._get_conn()

        query = "SELECT * FROM entities WHERE entity_type = 'import' AND name LIKE ?"
        params = [f"%{name}%"]

        if project_root:
            query += " AND project_root = ?"
            params.append(project_root)

        query += " LIMIT 50"

        results = conn.execute(query, params).fetchall()
        conn.close()

        return [dict(r) for r in results]

    def get_file_outline(self, file_path: str) -> Dict:
        """Get structural outline of a file."""
        conn = self._get_conn()

        file_info = conn.execute(
            "SELECT * FROM files WHERE path = ?",
            (file_path,)
        ).fetchone()

        if not file_info:
            conn.close()
            return {}

        entities = conn.execute(
            "SELECT * FROM entities WHERE file_path = ? ORDER BY line_number",
            (file_path,)
        ).fetchall()

        conn.close()

        return {
            "file": dict(file_info),
            "entities": [dict(e) for e in entities]
        }

    def get_project_summary(self, project_root: str) -> Optional[Dict]:
        """Get summary of analyzed project."""
        conn = self._get_conn()

        project = conn.execute(
            "SELECT * FROM projects WHERE root_path = ?",
            (project_root,)
        ).fetchone()

        if not project:
            conn.close()
            return None

        # Get top entities
        top_classes = conn.execute("""
            SELECT name, file_path, signature FROM entities
            WHERE project_root = ? AND entity_type = 'class'
            ORDER BY name LIMIT 20
        """, (project_root,)).fetchall()

        top_functions = conn.execute("""
            SELECT name, file_path, parent FROM entities
            WHERE project_root = ? AND entity_type IN ('function', 'method')
            ORDER BY name LIMIT 30
        """, (project_root,)).fetchall()

        conn.close()

        result = dict(project)
        result["language_breakdown"] = json.loads(result["language_breakdown"])
        result["entry_points"] = json.loads(result["entry_points"])
        result["top_classes"] = [dict(c) for c in top_classes]
        result["top_functions"] = [dict(f) for f in top_functions]

        return result

    def get_stats(self) -> Dict:
        """Get overall stats."""
        conn = self._get_conn()

        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

        by_type = conn.execute("""
            SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type
        """).fetchall()

        by_lang = conn.execute("""
            SELECT language, COUNT(*) FROM files GROUP BY language
        """).fetchall()

        conn.close()

        return {
            "files_analyzed": file_count,
            "entities_tracked": entity_count,
            "projects_indexed": project_count,
            "by_entity_type": {r[0]: r[1] for r in by_type},
            "by_language": {r[0]: r[1] for r in by_lang},
            "db_size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0
        }


# =============================================================================
# CLI
# =============================================================================

def _progress(count: int, path: str):
    """Progress callback."""
    print(f"\r  Analyzed {count} files... {path[:50]:<50}", end="", flush=True)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Synaptic's Codebase Vision",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command")

    # Analyze command
    analyze_p = subparsers.add_parser("analyze", help="Analyze a project")
    analyze_p.add_argument("path", type=Path, help="Project root path")
    analyze_p.add_argument("--max-files", type=int, default=1000)

    # Find command
    find_p = subparsers.add_parser("find", help="Find entity definition")
    find_p.add_argument("name", help="Entity name to find")
    find_p.add_argument("--type", choices=["function", "class", "method", "import"])

    # Outline command
    outline_p = subparsers.add_parser("outline", help="Get file outline")
    outline_p.add_argument("path", type=Path, help="File path")

    # Structure command
    struct_p = subparsers.add_parser("structure", help="Show project structure")
    struct_p.add_argument("path", type=Path, help="Project root path")

    # Stats command
    subparsers.add_parser("stats", help="Show overall stats")

    args = parser.parse_args()
    vision = CodebaseVision()

    if args.command == "analyze":
        print(f"🔍 Analyzing {args.path}...")
        result = vision.analyze_project(args.path, args.max_files, _progress)
        print(f"\n\n✅ Analysis complete!")
        print(f"   Files:     {result.total_files}")
        print(f"   Functions: {result.total_functions}")
        print(f"   Classes:   {result.total_classes}")
        print(f"   Languages: {result.language_breakdown}")

    elif args.command == "find":
        results = vision.find_definition(args.name, args.type)
        if not results:
            print(f"No definition found for '{args.name}'")
        else:
            print(f"Found {len(results)} matches for '{args.name}':\n")
            for r in results:
                location = f"{r['file_path']}:{r['line_number']}"
                sig = r.get('signature', r['name'])
                print(f"  [{r['entity_type']:8}] {sig}")
                print(f"            {location}\n")

    elif args.command == "outline":
        # Analyze file first if needed
        path = args.path.resolve()
        vision.analyze_file(path)
        outline = vision.get_file_outline(str(path))

        if not outline:
            print(f"Could not analyze {path}")
        else:
            f = outline["file"]
            print(f"📄 {path.name} ({f['language']})")
            if f.get("module_docstring"):
                print(f"   {f['module_docstring'][:80]}...")
            print()

            entities = outline["entities"]
            imports = [e for e in entities if e["entity_type"] == "import"]
            classes = [e for e in entities if e["entity_type"] == "class"]
            functions = [e for e in entities if e["entity_type"] in ("function", "method")]

            if imports:
                print(f"  Imports ({len(imports)}):")
                for imp in imports[:10]:
                    print(f"    {imp['name']}")
                if len(imports) > 10:
                    print(f"    ... and {len(imports) - 10} more")
                print()

            if classes:
                print(f"  Classes ({len(classes)}):")
                for cls in classes:
                    print(f"    {cls['signature'] or cls['name']}")
                print()

            if functions:
                print(f"  Functions ({len(functions)}):")
                for fn in functions:
                    parent = f" ({fn['parent']})" if fn.get("parent") else ""
                    print(f"    {fn['name']}{parent}")
                print()

    elif args.command == "structure":
        result = vision.get_project_summary(str(args.path.resolve()))
        if not result:
            print(f"Project not analyzed yet. Run: codebase_vision.py analyze {args.path}")
        else:
            print(f"🏗️ {result['name']}")
            print(f"   Files:     {result['total_files']}")
            print(f"   Functions: {result['total_functions']}")
            print(f"   Classes:   {result['total_classes']}")
            print(f"\n  Languages:")
            for lang, count in result['language_breakdown'].items():
                print(f"    {lang}: {count} files")
            print(f"\n  Entry Points:")
            for ep in result['entry_points']:
                print(f"    {ep}")

    elif args.command == "stats":
        stats = vision.get_stats()
        print("📊 Codebase Vision Statistics\n")
        print(f"  Files Analyzed:   {stats['files_analyzed']}")
        print(f"  Entities Tracked: {stats['entities_tracked']}")
        print(f"  Projects Indexed: {stats['projects_indexed']}")
        print(f"  DB Size:          {stats['db_size_bytes'] / 1024:.1f} KB")
        print("\n  By Entity Type:")
        for etype, count in stats['by_entity_type'].items():
            print(f"    {etype:12} {count:>6}")
        print("\n  By Language:")
        for lang, count in stats['by_language'].items():
            print(f"    {lang:12} {count:>6}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
