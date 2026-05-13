#!/usr/bin/env python3
"""
Harmonizer Gate -- 7-category consistency check for agent outputs.

Before any agent output is accepted into the codebase, it must pass
the Harmonizer's quality gates. This prevents hallucinated code,
style drift, security issues, and logical inconsistencies.

Categories:
1. SYNTAX_VALID        -- Code parses without errors (AST check for Python, basic for others)
2. STYLE_CONSISTENT    -- Follows existing codebase patterns (naming, imports, structure)
3. SECURITY_SAFE       -- No injection vectors, credential exposure, unsafe operations
4. LOGIC_SOUND         -- No obvious logical errors, dead code, or unreachable paths
5. DEPENDENCY_SAFE     -- No new undeclared dependencies, no version conflicts
6. TEST_ALIGNED        -- Changes don't obviously break existing test expectations
7. ARCHITECTURE_ALIGNED -- Respects documented architecture patterns

Gate results: PASS, WARN, FAIL with explanations.
Overall: ACCEPT (all pass), REVIEW (has warnings), REJECT (any fail)

Usage:
    from memory.harmonizer import Harmonizer

    h = Harmonizer()
    result = await h.harmonize(code_string, language="python")
    print(result.overall_verdict)  # ACCEPT / REVIEW / REJECT

    # Single gate
    gate = await h.check_security(code_string)
    print(gate.verdict, gate.explanation)

Created: February 10, 2026
Purpose: Quality gate between agent outputs and codebase integration
"""
from __future__ import annotations

import ast
import enum
import importlib.metadata
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("context_dna.harmonizer")


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------

class HarmonizerCategory(enum.Enum):
    """The 7 consistency gate categories."""
    SYNTAX_VALID = "syntax_valid"
    STYLE_CONSISTENT = "style_consistent"
    SECURITY_SAFE = "security_safe"
    LOGIC_SOUND = "logic_sound"
    DEPENDENCY_SAFE = "dependency_safe"
    TEST_ALIGNED = "test_aligned"
    ARCHITECTURE_ALIGNED = "architecture_aligned"


class Verdict(enum.Enum):
    """Single-gate verdict."""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class OverallVerdict(enum.Enum):
    """Aggregate verdict across all gates."""
    ACCEPT = "accept"   # All gates passed
    REVIEW = "review"   # Has warnings, no failures
    REJECT = "reject"   # At least one failure


@dataclass
class GateResult:
    """Result of a single harmonizer gate check."""
    category: HarmonizerCategory
    verdict: Verdict
    explanation: str
    confidence: float  # 0.0 to 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "verdict": self.verdict.value,
            "explanation": self.explanation,
            "confidence": self.confidence,
        }


@dataclass
class HarmonizerResult:
    """Aggregated result from all 7 gates."""
    gate_results: List[GateResult]
    overall_verdict: OverallVerdict
    summary: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate_results": [g.to_dict() for g in self.gate_results],
            "overall_verdict": self.overall_verdict.value,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Security patterns (compiled once at module load)
# ---------------------------------------------------------------------------

_SECURITY_PATTERNS: List[tuple[re.Pattern, str, str]] = [
    # (pattern, description, severity: fail/warn)
    (re.compile(r"""['"]sk-[a-zA-Z0-9_-]{10,}['"]"""), "Hardcoded OpenAI API key", "fail"),
    (re.compile(r"""['"]AKIA[A-Z0-9]{16}['"]"""), "Hardcoded AWS access key ID", "fail"),
    (re.compile(r"""['"][a-f0-9]{40}['"]"""), "Possible hardcoded secret (40-char hex)", "warn"),
    (re.compile(r"""['"]ghp_[a-zA-Z0-9]{36}['"]"""), "Hardcoded GitHub personal access token", "fail"),
    (re.compile(r"""['"]gho_[a-zA-Z0-9]{36}['"]"""), "Hardcoded GitHub OAuth token", "fail"),
    (re.compile(r"""['"]xoxb-[a-zA-Z0-9-]+['"]"""), "Hardcoded Slack bot token", "fail"),
    (re.compile(r"""password\s*=\s*['"][^'"]{4,}['"]""", re.IGNORECASE), "Hardcoded password", "fail"),
    (re.compile(r"""\beval\s*\("""), "Use of eval()", "fail"),
    (re.compile(r"""\bexec\s*\("""), "Use of exec()", "fail"),
    (re.compile(r"""subprocess\.\w+\(.*shell\s*=\s*True""", re.DOTALL), "subprocess with shell=True", "fail"),
    (re.compile(r"""\bos\.system\s*\("""), "Use of os.system() -- prefer subprocess", "fail"),
    (re.compile(r"""\bpickle\.loads?\s*\("""), "pickle.load(s) on potentially untrusted data", "warn"),
    (re.compile(r"""['"]%s['"].*%|\.format\(.*\).*(?:SELECT|INSERT|UPDATE|DELETE|DROP)""", re.IGNORECASE),
     "Possible SQL injection via string formatting", "fail"),
    (re.compile(r"""f['"].*\{.*\}.*(?:SELECT|INSERT|UPDATE|DELETE|DROP)""", re.IGNORECASE),
     "Possible SQL injection via f-string", "fail"),
    (re.compile(r"""\+\s*['"]?\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b""", re.IGNORECASE),
     "SQL string concatenation", "warn"),
    (re.compile(r"""(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b.*\+\s*\w+""", re.IGNORECASE),
     "SQL string concatenation", "warn"),
    (re.compile(r"""__import__\s*\("""), "Dynamic import via __import__()", "warn"),
    (re.compile(r"""yaml\.load\s*\([^)]*\)(?!\s*,\s*Loader)"""), "yaml.load without safe Loader", "warn"),
    (re.compile(r"""tempfile\.mk[sd]temp\b(?!.*suffix)"""), "Insecure temp file creation", "warn"),
]

# Style patterns
_PYTHON_NAMING_PATTERNS = {
    "snake_case_function": re.compile(r"""def\s+([a-z_][a-z0-9_]*)\s*\("""),
    "snake_case_variable": re.compile(r"""^(\s*)([a-z_][a-z0-9_]*)\s*=""", re.MULTILINE),
    "PascalCase_class": re.compile(r"""class\s+([A-Z][a-zA-Z0-9]*)\s*[:(]"""),
    "UPPER_CASE_constant": re.compile(r"""^([A-Z_][A-Z0-9_]*)\s*=""", re.MULTILINE),
}

_CAMEL_CASE_FUNC = re.compile(r"""def\s+([a-z]+[A-Z][a-zA-Z0-9]*)\s*\(""")


# ---------------------------------------------------------------------------
# Installed packages cache (lazy-loaded once)
# ---------------------------------------------------------------------------

_installed_packages: Optional[set[str]] = None


def _get_installed_packages() -> set[str]:
    """Return the set of installed package names (normalized to lowercase)."""
    global _installed_packages
    if _installed_packages is None:
        _installed_packages = set()
        for dist in importlib.metadata.distributions():
            name = dist.metadata.get("Name", "")
            if name:
                _installed_packages.add(name.lower().replace("-", "_"))
                _installed_packages.add(name.lower().replace("_", "-"))
                _installed_packages.add(name.lower())
    return _installed_packages


# Standard library modules (covers Python 3.9-3.14)
_STDLIB_MODULES = set(
    "abc aifc argparse array ast asynchat asyncio asyncore atexit audioop base64 bdb binascii "
    "binhex bisect builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop "
    "collections colorsys compileall concurrent configparser contextlib contextvars copy copyreg "
    "cProfile crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils "
    "doctest email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch fractions "
    "ftplib functools gc getopt getpass gettext glob grp gzip hashlib heapq hmac html http "
    "idlelib imaplib imghdr imp importlib inspect io ipaddress itertools json keyword lib2to3 "
    "linecache locale logging lzma mailbox mailcap marshal math mimetypes mmap modulefinder "
    "multiprocessing netrc nis nntplib numbers operator optparse os ossaudiodev pathlib pdb "
    "pickle pickletools pipes pkgutil platform plistlib poplib posix posixpath pprint profile "
    "pstats pty pwd py_compile pyclbr pydoc queue quopri random re readline reprlib resource "
    "rlcompleter runpy sched secrets select selectors shelve shlex shutil signal site smtpd "
    "smtplib sndhdr socket socketserver spwd sqlite3 ssl stat statistics string stringprep "
    "struct subprocess sunau symtable sys sysconfig syslog tabnanny tarfile telnetlib tempfile "
    "termios test textwrap threading time timeit tkinter token tokenize tomllib trace traceback "
    "tracemalloc tty turtle turtledemo types typing unicodedata unittest urllib uu uuid venv "
    "warnings wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile "
    "zipimport zlib _thread __future__".split()
)


# ---------------------------------------------------------------------------
# Harmonizer
# ---------------------------------------------------------------------------

class Harmonizer:
    """7-gate consistency checker for agent-produced code.

    Each gate runs independently and returns a GateResult.
    The harmonize() method runs all 7 and produces an aggregate verdict.
    LLM-based gates (logic, architecture) degrade gracefully when LLM is offline.
    """

    def __init__(self) -> None:
        pass

    # -- Gate 1: Syntax Valid --

    async def check_syntax(self, code: str, language: str = "python") -> GateResult:
        """Verify code parses without syntax errors."""
        cat = HarmonizerCategory.SYNTAX_VALID
        if not code.strip():
            return GateResult(cat, Verdict.WARN, "Empty code block", 1.0)

        if language == "python":
            try:
                ast.parse(code)
                return GateResult(cat, Verdict.PASS, "Python AST parse successful", 1.0)
            except SyntaxError as e:
                return GateResult(
                    cat, Verdict.FAIL,
                    f"Syntax error at line {e.lineno}: {e.msg}",
                    1.0,
                )
        elif language in ("javascript", "js", "typescript", "ts"):
            issues = []
            # Basic bracket/paren/brace balance check
            opens = {"(": ")", "[": "]", "{": "}"}
            stack = []
            in_string = None
            prev_char = ""
            for i, ch in enumerate(code):
                if in_string:
                    if ch == in_string and prev_char != "\\":
                        in_string = None
                elif ch in ("'", '"', "`"):
                    in_string = ch
                elif ch in opens:
                    stack.append((opens[ch], i))
                elif ch in (")", "]", "}"):
                    if not stack:
                        issues.append(f"Unmatched '{ch}' at position {i}")
                    elif stack[-1][0] != ch:
                        issues.append(f"Mismatched bracket at position {i}: expected '{stack[-1][0]}', got '{ch}'")
                    else:
                        stack.pop()
                prev_char = ch
            if stack:
                issues.append(f"{len(stack)} unclosed bracket(s)")
            if issues:
                return GateResult(cat, Verdict.FAIL, "; ".join(issues), 0.8)
            return GateResult(cat, Verdict.PASS, "JS/TS bracket balance OK", 0.7)
        else:
            return GateResult(cat, Verdict.WARN, f"No syntax checker for language '{language}'", 0.3)

    # -- Gate 2: Style Consistent --

    async def check_style(self, code: str, context_files: Optional[List[str]] = None) -> GateResult:
        """Check naming conventions and import patterns."""
        cat = HarmonizerCategory.STYLE_CONSISTENT
        issues = []
        lines = code.split("\n")

        # Check for camelCase function names (should be snake_case in Python)
        camel_funcs = _CAMEL_CASE_FUNC.findall(code)
        if camel_funcs:
            issues.append(f"camelCase function names (use snake_case): {', '.join(camel_funcs[:5])}")

        # Check for mixed indentation (tabs + spaces)
        has_tabs = any(line.startswith("\t") for line in lines if line.strip())
        has_spaces = any(re.match(r"^ {2,}", line) for line in lines if line.strip())
        if has_tabs and has_spaces:
            issues.append("Mixed tabs and spaces indentation")

        # Check for wildcard imports
        wildcard_imports = re.findall(r"from\s+\S+\s+import\s+\*", code)
        if wildcard_imports:
            issues.append(f"Wildcard import(s): {'; '.join(wildcard_imports[:3])}")

        # Check for very long lines (>120 chars)
        long_lines = [i + 1 for i, line in enumerate(lines) if len(line) > 120]
        if len(long_lines) > 5:
            issues.append(f"{len(long_lines)} lines exceed 120 chars (first: line {long_lines[0]})")

        # Check for missing docstrings on classes and functions
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if not (node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)):
                        # Only warn for public definitions (not private/dunder)
                        if not node.name.startswith("_"):
                            issues.append(f"Missing docstring on public {type(node).__name__[:-3].lower()} '{node.name}'")
        except SyntaxError:
            pass  # Syntax checking is gate 1's job

        if not issues:
            return GateResult(cat, Verdict.PASS, "Style checks passed", 0.8)
        elif any("camelCase" in i or "Mixed tabs" in i or "Wildcard" in i for i in issues):
            return GateResult(cat, Verdict.WARN, "; ".join(issues[:5]), 0.8)
        else:
            return GateResult(cat, Verdict.WARN, "; ".join(issues[:5]), 0.6)

    # -- Gate 3: Security Safe --

    async def check_security(self, code: str) -> GateResult:
        """Scan for security anti-patterns using regex matching."""
        cat = HarmonizerCategory.SECURITY_SAFE
        findings_fail = []
        findings_warn = []

        for pattern, description, severity in _SECURITY_PATTERNS:
            matches = pattern.findall(code)
            if matches:
                entry = f"{description} ({len(matches)} occurrence{'s' if len(matches) > 1 else ''})"
                if severity == "fail":
                    findings_fail.append(entry)
                else:
                    findings_warn.append(entry)

        if findings_fail:
            all_findings = findings_fail + findings_warn
            return GateResult(
                cat, Verdict.FAIL,
                "Security issues: " + "; ".join(all_findings[:8]),
                0.9,
            )
        elif findings_warn:
            return GateResult(
                cat, Verdict.WARN,
                "Security warnings: " + "; ".join(findings_warn[:5]),
                0.8,
            )
        return GateResult(cat, Verdict.PASS, "No security issues detected", 0.85)

    # -- Gate 4: Logic Sound --

    async def check_logic(self, code: str) -> GateResult:
        """LLM-based logic check with heuristic fallback."""
        cat = HarmonizerCategory.LOGIC_SOUND

        # Heuristic checks first (always run)
        heuristic_issues = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return GateResult(cat, Verdict.WARN, "Cannot check logic: syntax error", 0.3)

        for node in ast.walk(tree):
            # Unreachable code after return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for i, stmt in enumerate(node.body):
                    if isinstance(stmt, ast.Return) and i < len(node.body) - 1:
                        next_stmt = node.body[i + 1]
                        if not isinstance(next_stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            heuristic_issues.append(
                                f"Unreachable code after return in '{node.name}' at line {stmt.lineno}"
                            )

            # Bare except
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                heuristic_issues.append(f"Bare except at line {node.lineno} -- catch specific exceptions")

            # Mutable default arguments
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default is not None and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        heuristic_issues.append(
                            f"Mutable default argument in '{node.name}' at line {node.lineno}"
                        )

            # Comparison to None using == instead of is
            if isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant) and comparator.value is None:
                        heuristic_issues.append(f"Use 'is None' instead of '== None' at line {node.lineno}")

        # Attempt LLM-based deep logic check
        llm_analysis = None
        try:
            from memory.llm_priority_queue import butler_query

            truncated = code[:3000] if len(code) > 3000 else code
            prompt = (
                "Analyze this code for logical errors. Focus on:\n"
                "- Dead code or unreachable paths\n"
                "- Off-by-one errors\n"
                "- Incorrect boolean logic\n"
                "- Resource leaks (unclosed files/connections)\n"
                "- Race conditions\n\n"
                "If no issues found, say 'NO_ISSUES'. Otherwise list each issue on one line.\n\n"
                f"```\n{truncated}\n```"
            )
            llm_analysis = butler_query(
                "You are a code logic reviewer. Be concise. Only report real bugs, not style issues.",
                prompt,
                profile="coding",
            )
        except Exception as e:
            logger.debug(f"LLM logic check unavailable: {e}")

        # Combine results
        all_issues = list(heuristic_issues)
        llm_available = llm_analysis is not None and "NO_ISSUES" not in (llm_analysis or "")
        if llm_available and llm_analysis:
            # Filter out noise lines
            llm_lines = [
                line.strip() for line in llm_analysis.strip().split("\n")
                if line.strip() and not line.strip().startswith("#")
            ]
            if llm_lines:
                all_issues.append(f"LLM analysis: {'; '.join(llm_lines[:3])}")

        if not all_issues:
            confidence = 0.85 if llm_analysis is not None else 0.5
            return GateResult(cat, Verdict.PASS, "No logic issues found", confidence)

        # Heuristic issues alone are warnings; LLM-confirmed are stronger warnings
        severity = Verdict.WARN
        confidence = 0.7 if llm_analysis is not None else 0.5
        if any("Unreachable" in i for i in all_issues):
            confidence = 0.8

        return GateResult(cat, severity, "; ".join(all_issues[:5]), confidence)

    # -- Gate 5: Dependency Safe --

    async def check_dependencies(self, code: str) -> GateResult:
        """Scan imports and verify against installed packages."""
        cat = HarmonizerCategory.DEPENDENCY_SAFE

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return GateResult(cat, Verdict.WARN, "Cannot check deps: syntax error", 0.3)

        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])

        if not imported_modules:
            return GateResult(cat, Verdict.PASS, "No imports found", 1.0)

        installed = _get_installed_packages()
        unknown = []

        for mod in sorted(imported_modules):
            if mod in _STDLIB_MODULES:
                continue
            # Check if installed (try several normalized forms)
            mod_lower = mod.lower()
            if mod_lower in installed:
                continue
            if mod_lower.replace("_", "-") in installed:
                continue
            if mod_lower.replace("-", "_") in installed:
                continue
            # Relative imports (memory, context_dna, etc.) from this project
            if mod in ("memory", "context_dna", "acontext", "backend"):
                continue
            unknown.append(mod)

        if not unknown:
            return GateResult(cat, Verdict.PASS, f"All {len(imported_modules)} imports resolved", 0.9)

        return GateResult(
            cat, Verdict.WARN,
            f"Unrecognized imports (may be project-local or missing): {', '.join(unknown[:10])}",
            0.6,
        )

    # -- Gate 6: Test Aligned --

    async def check_tests(self, code: str, test_context: Optional[str] = None) -> GateResult:
        """Heuristic check for test compatibility."""
        cat = HarmonizerCategory.TEST_ALIGNED
        issues = []

        # Check if code modifies common test fixtures/interfaces
        modified_signatures = re.findall(r"def\s+(\w+)\s*\(([^)]*)\)", code)

        if test_context:
            # Cross-reference modified functions against test expectations
            for func_name, _ in modified_signatures:
                if func_name in test_context:
                    # Check if the test calls this function with specific args
                    test_calls = re.findall(
                        rf"{func_name}\s*\(([^)]*)\)", test_context
                    )
                    if test_calls:
                        issues.append(
                            f"Modified function '{func_name}' is called in tests -- verify signature compatibility"
                        )

        # Check for removed or renamed public functions (comparing against test_context)
        if test_context:
            test_referenced_funcs = set(re.findall(r"(\w+)\s*\(", test_context))
            code_defined_funcs = set(name for name, _ in modified_signatures)
            # Only flag if test references a function and code defines at least one function
            if code_defined_funcs:
                missing_in_code = test_referenced_funcs - code_defined_funcs - {"self", "print", "assert", "len", "range", "str", "int", "float", "list", "dict", "set", "type", "isinstance", "hasattr", "getattr"}
                # Not actionable without more context -- skip this check

        # Check for global state mutations that could affect tests
        global_mutations = re.findall(r"^\s*global\s+(\w+)", code, re.MULTILINE)
        if global_mutations:
            issues.append(f"Global state mutations ({', '.join(global_mutations[:3])}) may affect test isolation")

        # Check for monkey-patching
        monkey_patches = re.findall(r"\b\w+\.\w+\s*=\s*(?:lambda|def\b|Mock|patch)", code)
        if monkey_patches:
            issues.append("Possible monkey-patching detected -- may affect tests")

        if not issues:
            confidence = 0.7 if test_context else 0.4
            return GateResult(cat, Verdict.PASS, "No test compatibility issues detected", confidence)

        return GateResult(cat, Verdict.WARN, "; ".join(issues[:5]), 0.6)

    # -- Gate 7: Architecture Aligned --

    async def check_architecture(self, code: str, architecture_context: Optional[str] = None) -> GateResult:
        """LLM-based architecture alignment check with heuristic fallback."""
        cat = HarmonizerCategory.ARCHITECTURE_ALIGNED
        issues = []

        # Heuristic: check for known anti-patterns in this codebase
        if "localhost" in code and "127.0.0.1" not in code:
            # Codebase convention: use 127.0.0.1 to avoid IPv6 resolution issues
            if re.search(r"""['"]localhost['"]""", code):
                issues.append("Use '127.0.0.1' instead of 'localhost' (IPv6 resolution issues on macOS)")

        # Check for sqlite3 connection without proper cleanup
        if "sqlite3.connect" in code:
            if "finally" not in code and "with" not in code:
                issues.append("sqlite3.connect without try/finally -- connections may leak")

        # Check for SQLiteStorage() direct instantiation (must use singleton)
        if "SQLiteStorage()" in code and "get_sqlite_storage" not in code:
            issues.append("Direct SQLiteStorage() -- use get_sqlite_storage() singleton")

        # LLM-based architecture check
        llm_analysis = None
        if architecture_context:
            try:
                from memory.llm_priority_queue import butler_query

                truncated_code = code[:2500] if len(code) > 2500 else code
                truncated_arch = architecture_context[:1500] if len(architecture_context) > 1500 else architecture_context
                prompt = (
                    "Compare this code against the architecture constraints below.\n"
                    "Report ONLY violations. If the code is aligned, say 'ALIGNED'.\n\n"
                    f"Architecture constraints:\n{truncated_arch}\n\n"
                    f"Code:\n```\n{truncated_code}\n```"
                )
                llm_analysis = butler_query(
                    "You are an architecture reviewer. Be concise. Only report real violations.",
                    prompt,
                    profile="coding",
                )
            except Exception as e:
                logger.debug(f"LLM architecture check unavailable: {e}")

        if llm_analysis and "ALIGNED" not in llm_analysis:
            llm_lines = [
                line.strip() for line in llm_analysis.strip().split("\n")
                if line.strip() and not line.strip().startswith("#")
            ]
            if llm_lines:
                issues.append(f"LLM: {'; '.join(llm_lines[:3])}")

        if not issues:
            confidence = 0.8 if llm_analysis is not None else 0.5
            return GateResult(cat, Verdict.PASS, "Architecture alignment OK", confidence)

        # Architecture violations from heuristics are warnings; LLM adds confidence
        confidence = 0.75 if llm_analysis is not None else 0.6
        return GateResult(cat, Verdict.WARN, "; ".join(issues[:5]), confidence)

    # -- Aggregate --

    async def harmonize(
        self,
        code: str,
        language: str = "python",
        context: Optional[Dict[str, Any]] = None,
    ) -> HarmonizerResult:
        """Run all 7 gates and produce an aggregate verdict.

        Args:
            code: The source code to check.
            language: Programming language (python, js, ts).
            context: Optional dict with keys:
                context_files -- list of related file paths for style checking
                test_context -- test code as string for test alignment
                architecture_context -- architecture docs as string

        Returns:
            HarmonizerResult with per-gate results and overall verdict.
        """
        ctx = context or {}

        results = []
        results.append(await self.check_syntax(code, language))
        results.append(await self.check_style(code, ctx.get("context_files")))
        results.append(await self.check_security(code))
        results.append(await self.check_logic(code))
        results.append(await self.check_dependencies(code))
        results.append(await self.check_tests(code, ctx.get("test_context")))
        results.append(await self.check_architecture(code, ctx.get("architecture_context")))

        # Determine overall verdict
        has_fail = any(r.verdict == Verdict.FAIL for r in results)
        has_warn = any(r.verdict == Verdict.WARN for r in results)

        if has_fail:
            overall = OverallVerdict.REJECT
        elif has_warn:
            overall = OverallVerdict.REVIEW
        else:
            overall = OverallVerdict.ACCEPT

        # Build summary
        passed = sum(1 for r in results if r.verdict == Verdict.PASS)
        warned = sum(1 for r in results if r.verdict == Verdict.WARN)
        failed = sum(1 for r in results if r.verdict == Verdict.FAIL)
        summary = f"{passed} passed, {warned} warnings, {failed} failed -> {overall.value.upper()}"

        if has_fail:
            fail_cats = [r.category.value for r in results if r.verdict == Verdict.FAIL]
            summary += f". Failures: {', '.join(fail_cats)}"

        return HarmonizerResult(
            gate_results=results,
            overall_verdict=overall,
            summary=summary,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

def create_router() -> "APIRouter":
    """Create the FastAPI router for the Harmonizer API."""
    from fastapi import APIRouter
    from pydantic import BaseModel, Field

    router = APIRouter(prefix="/v1/harmonizer", tags=["harmonizer"])

    class HarmonizeRequest(BaseModel):
        code: str = Field(..., description="Source code to check")
        language: str = Field("python", description="Programming language")
        context: Optional[Dict[str, Any]] = Field(None, description="Optional context for deeper checks")

    class GateResultResponse(BaseModel):
        category: str
        verdict: str
        explanation: str
        confidence: float

    class HarmonizeResponse(BaseModel):
        gate_results: List[GateResultResponse]
        overall_verdict: str
        summary: str
        timestamp: float

    class HealthResponse(BaseModel):
        status: str
        categories: List[str]
        llm_available: bool

    @router.post("/check", response_model=HarmonizeResponse)
    async def check_code(req: HarmonizeRequest) -> HarmonizeResponse:
        """Run all 7 harmonizer gates on the provided code."""
        h = Harmonizer()
        result = await h.harmonize(req.code, req.language, req.context)
        return HarmonizeResponse(
            gate_results=[
                GateResultResponse(**g.to_dict()) for g in result.gate_results
            ],
            overall_verdict=result.overall_verdict.value,
            summary=result.summary,
            timestamp=result.timestamp,
        )

    @router.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """Harmonizer health check."""
        llm_available = False
        try:
            from memory.llm_priority_queue import get_queue_stats
            stats = get_queue_stats()
            llm_available = stats.get("total_requests", 0) >= 0
        except Exception:
            pass

        return HealthResponse(
            status="healthy",
            categories=[c.value for c in HarmonizerCategory],
            llm_available=llm_available,
        )

    return router


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO)

    async def _main():
        h = Harmonizer()

        if len(sys.argv) > 1 and sys.argv[1] == "test":
            # Self-test with sample code
            test_code = '''
import os
import json
from memory.llm_priority_queue import butler_query

API_KEY = "sk-proj-abc123def456ghi789"

class MyHelper:
    def __init__(self):
        self.conn = sqlite3.connect("test.db")

    def fetchData(self, query):
        result = os.system(f"echo {query}")
        return eval(result)

    def process(self, items=[]):
        if items == None:
            return
        return items
'''
            result = await h.harmonize(test_code)
            print(f"\nOverall: {result.overall_verdict.value.upper()}")
            print(f"Summary: {result.summary}\n")
            for gate in result.gate_results:
                icon = {"pass": "+", "warn": "~", "fail": "!"}[gate.verdict.value]
                print(f"  [{icon}] {gate.category.value}: {gate.verdict.value} ({gate.confidence:.0%})")
                print(f"      {gate.explanation}")
            return

        if len(sys.argv) > 1:
            # Read file from argument
            filepath = sys.argv[1]
            try:
                with open(filepath) as f:
                    code = f.read()
            except FileNotFoundError:
                print(f"File not found: {filepath}")
                sys.exit(1)

            lang = "python" if filepath.endswith(".py") else "javascript"
            result = await h.harmonize(code, language=lang)
            print(f"\nHarmonizer: {filepath}")
            print(f"Overall: {result.overall_verdict.value.upper()}")
            print(f"Summary: {result.summary}\n")
            for gate in result.gate_results:
                icon = {"pass": "+", "warn": "~", "fail": "!"}[gate.verdict.value]
                print(f"  [{icon}] {gate.category.value}: {gate.verdict.value} ({gate.confidence:.0%})")
                if gate.verdict != Verdict.PASS:
                    print(f"      {gate.explanation}")
            return

        print("Usage:")
        print("  python harmonizer.py test        -- run self-test with sample code")
        print("  python harmonizer.py <file.py>   -- check a file")

    asyncio.run(_main())
