#!/usr/bin/env python3
"""ZSF bare-except scanner.

Walks every tracked .py file (or a passed root) and flags the bare-swallow
anti-pattern:

    except:                    # noqa
        pass
    except Exception:          # noqa: BLE001
        pass

That pattern violates the Zero Silent Failures invariant — exceptions must be
observable. The exception types ``KeyboardInterrupt`` / ``SystemExit`` /
``StopIteration`` / ``GeneratorExit`` are NOT covered here (they are correct
to swallow in many control-flow contexts).

Output format (one violation per line, deterministic order):

    <relative-path>:<line>:<exc-type-or-bare>

A violation is ``<path>:<line>:<exc>`` only on the actual ``except`` clause's
line number. Comparing against the allowlist drops line numbers (so legitimate
refactors don't fail the gate) — same pattern as
``check-mf-extraction-contract.sh``.

A line carrying an explicit ``# zsf-allow`` comment (or ``# noqa: BLE001``
that already lives on the ``except`` line, audited) is treated as
intentionally allowed.

Exit codes:
    0 — scan completed (caller compares output vs allowlist)
    2 — internal error (read/parse failed) — surfaced to stderr; never silent
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple


# Top-level dirs that are ALWAYS excluded — third-party / generated / vendored.
# These trees would never be ZSF-audited regardless: they're not "our" code.
EXCLUDED_PREFIXES = (
    ".venv/",
    "venv/",
    "venv.nosync/",
    "venv.nosync.repo-root/",
    "node_modules/",
    "build/",
    "dist/",
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    "memory.bak.",        # memory.bak.YYYYMMDD/ legacy snapshots
    ".fleet-status/",     # generated state
    ".multifleet/",       # generated state
    ".projectdna/",       # generated state
    "google-drive-code/", # mirrored remote content
)

# Per-instructions: ~/Downloads is NEVER scanned (read-only v6 source).
# ``Path.is_relative_to`` not available on 3.8 — string check is portable.
DOWNLOADS_PREFIX = str(Path.home() / "Downloads") + os.sep


def _excluded(rel: str) -> bool:
    if any(rel.startswith(p) for p in EXCLUDED_PREFIXES):
        return True
    # Substring excludes for nested venvs / caches — these can sit at depth.
    parts = rel.split(os.sep)
    for p in parts:
        if p in {".venv", "venv", "venv.nosync", "node_modules", "__pycache__", ".pytest_cache",
                 "build", "dist"}:
            return True
    return False


def list_tracked_py(root: Path) -> List[Path]:
    """Prefer git (tracked + others-not-ignored) — fast & deterministic.

    The gate must catch NEW files that haven't been committed yet (CI gate
    runs at submit time, not after merge), so we union ``ls-files`` (tracked)
    with ``ls-files --others --exclude-standard`` (untracked but
    not-gitignored). This keeps generated/build trees out (.gitignore covers
    them) while still catching freshly-added unstaged sources.

    Falls back to os.walk if git is unavailable or root is outside a worktree.
    """
    seen: set = set()
    paths: List[Path] = []
    git_ok = False
    for git_args in (
        ["git", "-C", str(root), "ls-files", "*.py"],
        ["git", "-C", str(root), "ls-files", "--others",
         "--exclude-standard", "*.py"],
    ):
        try:
            out = subprocess.run(
                git_args, capture_output=True, text=True, timeout=30, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if out.returncode != 0:
            continue
        git_ok = True
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line or line in seen:
                continue
            if _excluded(line):
                continue
            p = root / line
            if p.is_file() and p.suffix == ".py":
                seen.add(line)
                paths.append(p)
    if git_ok:
        return paths
    # Fallback walk — slower but always available.
    paths = []
    for dirpath, dirs, files in os.walk(root):
        # Prune excluded dirs in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs
                   if d not in {".venv", "venv", "node_modules", "__pycache__",
                                ".git", "build", "dist", ".pytest_cache",
                                ".mypy_cache"}]
        for f in files:
            if not f.endswith(".py"):
                continue
            full = Path(dirpath) / f
            rel = full.relative_to(root).as_posix()
            if _excluded(rel):
                continue
            paths.append(full)
    return paths


def _is_bare_swallow(handler: ast.ExceptHandler) -> Tuple[bool, str]:
    """Return (is_violation, exc_type_str).

    Pattern: ``except:`` or ``except Exception:`` whose body is a single ``pass``.
    Also flags empty bodies (impossible — Python requires a stmt — but be safe).
    """
    t = handler.type
    is_bare = t is None
    is_broad_exc = isinstance(t, ast.Name) and t.id == "Exception"
    if not (is_bare or is_broad_exc):
        return False, ""
    body = handler.body
    if not body:
        return True, ("Exception" if is_broad_exc else "<bare>")
    # Single pass = the canonical anti-pattern.
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return True, ("Exception" if is_broad_exc else "<bare>")
    # Body has only Pass / Ellipsis / docstring — still a swallow.
    # ast.Constant covers strings, numbers, AND `...` (since Python 3.8 the
    # parser emits Constant(value=Ellipsis) — ast.Ellipsis is the deprecated
    # alias). One isinstance check covers all inert literal bodies.
    only_inert = all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in body
    )
    if only_inert:
        return True, ("Exception" if is_broad_exc else "<bare>")
    return False, ""


def _line_has_zsf_allow(src_lines: List[str], lineno: int) -> bool:
    """Check the ``except`` line and the line above for an explicit allow tag.

    Two accepted markers:
        # zsf-allow                — explicitly audited, leave alone
        # noqa: BLE001             — pre-existing ruff allowlist (audited)
    """
    for off in (0, -1):
        idx = lineno - 1 + off
        if 0 <= idx < len(src_lines):
            line = src_lines[idx]
            if "zsf-allow" in line or "noqa: BLE001" in line:
                return True
    return False


def scan(root: Path) -> Tuple[List[str], List[str]]:
    """Return (violations, errors).

    Each violation: ``<rel-posix-path>:<lineno>:<exc-type-str>``.
    """
    violations: List[str] = []
    errors: List[str] = []
    for path in list_tracked_py(root):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            errors.append(f"read-fail:{path}:{exc}")
            continue
        try:
            tree = ast.parse(src, str(path))
        except SyntaxError as exc:
            errors.append(f"parse-fail:{path}:{exc}")
            continue
        src_lines = src.splitlines()
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            hit, exc_str = _is_bare_swallow(node)
            if not hit:
                continue
            if _line_has_zsf_allow(src_lines, node.lineno):
                continue
            violations.append(f"{rel}:{node.lineno}:{exc_str}")
    violations.sort()
    return violations, errors


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    root = Path(args[0]).resolve() if args else Path.cwd()
    if not root.is_dir():
        print(f"[zsf-scan] FAIL: {root} is not a directory", file=sys.stderr)
        return 2
    # Block accidental scans of ~/Downloads (constraint).
    root_str = str(root) + os.sep
    if root_str.startswith(DOWNLOADS_PREFIX):
        print(f"[zsf-scan] FAIL: refusing to scan under ~/Downloads", file=sys.stderr)
        return 2
    violations, errors = scan(root)
    if errors:
        for err in errors:
            print(f"[scan-error] {err}", file=sys.stderr)
        # Errors invalidate the scan — caller cannot prove no violation in the
        # files we couldn't parse. Hard fail (ZSF: surface, never silent).
        return 2
    for v in violations:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
