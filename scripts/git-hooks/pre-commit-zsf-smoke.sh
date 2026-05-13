#!/usr/bin/env bash
# pre-commit-zsf-smoke.sh — RACE Q3
#
# Catches two regression classes at commit time (faster feedback than the
# pre-publish gate at race/m4):
#
#   1. Zero-Silent-Failures (ZSF): bare `except Exception: pass` patterns in
#      memory/ or multi-fleet/multifleet/ Python files.
#   2. Import-error regressions: NameError / ImportError surfaced by a quick
#      `python3 -c 'import <module>'` smoke against each modified module.
#
# Plus shellcheck on staged shell scripts (skipped if shellcheck is absent —
# never blocks on tooling-not-installed).
#
# Budget: <2s on a typical commit. Designed to be composed with other
# pre-commit hooks via a wrapper.
#
# Bypass (operator escape hatch): git commit --no-verify

set -uo pipefail
# NOTE: do not use -e; many checks below intentionally tolerate non-zero exits.

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 0

STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
[ -z "$STAGED" ] && exit 0

FAIL=0

# --------------------------------------------------------------------------
# 1. AST scanner: bare `except Exception: pass` in tracked Python paths.
# --------------------------------------------------------------------------
PY_TARGETS=$(echo "$STAGED" | grep -E '^(memory/|multi-fleet/multifleet/).*\.py$' || true)

if [ -n "$PY_TARGETS" ]; then
  PY_BIN="python3"
  if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
    PY_BIN="$REPO_ROOT/.venv/bin/python3"
  fi

  ZSF_OUT=$("$PY_BIN" - "$PY_TARGETS" <<'PYEOF'
import ast
import sys

files = [f for f in sys.argv[1].split("\n") if f.strip()]
hits = []

for path in files:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        hits.append(f"{path}:{exc.lineno or 0}: SyntaxError ({exc.msg})")
        continue
    except FileNotFoundError:
        continue

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        if not body:
            continue
        # Pattern 1: `except [Exception]: pass` (single Pass stmt).
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            etype = "bare except" if node.type is None else (
                ast.unparse(node.type) if hasattr(ast, "unparse") else "Exception"
            )
            hits.append(f"{path}:{node.lineno}: silent `except {etype}: pass`")
        # Pattern 2: ellipsis literal as the sole body — also silent.
        elif (
            len(body) == 1
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and body[0].value.value is Ellipsis
        ):
            hits.append(f"{path}:{node.lineno}: silent `except: ...`")

for h in hits:
    print(h)
sys.exit(1 if hits else 0)
PYEOF
  )
  ZSF_RC=$?
  if [ "$ZSF_RC" -ne 0 ]; then
    echo "BLOCKED: Zero-Silent-Failures violation in staged files:"
    echo "$ZSF_OUT" | sed 's/^/  /'
    echo
    echo "  Fix: log the exception (logger.exception/print) and re-raise or"
    echo "  record to an observable channel. See CLAUDE.md \"ZERO SILENT"
    echo "  FAILURES\" invariant."
    echo
    FAIL=1
  fi

  # ----------------------------------------------------------------------
  # 2. Import-smoke: each modified module is import-checked once. We only
  #    flag NameError / ImportError / SyntaxError raised at import time —
  #    runtime exceptions inside guarded `if __name__ == '__main__'` blocks
  #    are not exercised, so this is fast and conservative.
  # ----------------------------------------------------------------------
  IMPORT_OUT=$("$PY_BIN" - "$PY_TARGETS" <<'PYEOF'
import importlib.util
import sys
import os

repo_root = os.getcwd()
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
# multi-fleet/ has its own package root (multifleet/ lives inside it).
_mf_root = os.path.join(repo_root, "multi-fleet")
if os.path.isdir(_mf_root) and _mf_root not in sys.path:
    sys.path.insert(0, _mf_root)

files = [f for f in sys.argv[1].split("\n") if f.strip()]
hits = []

for path in files:
    if not os.path.exists(path):
        continue
    # Build a synthetic module name from the path so we don't collide with
    # already-loaded modules. We don't actually need the spec to resolve a
    # package — exec_module on a file spec is enough for a smoke check.
    mod_name = "__zsf_smoke__" + path.replace("/", "_").replace(".", "_")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (NameError, ImportError, SyntaxError) as exc:
        # Skip relative-import errors — package-internal files (e.g. multifleet/*)
        # require their parent package context; spec_from_file_location can't
        # provide it. These are packaging constraints, not regressions.
        if "relative import" in str(exc):
            continue
        hits.append(f"{path}: {type(exc).__name__}: {exc}")
    except Exception:
        # Other runtime errors at import time (e.g. side-effect __init__ that
        # talks to a network) are out of scope — we only gate on the regression
        # classes the prepublish gate already catches.
        pass

for h in hits:
    print(h)
sys.exit(1 if hits else 0)
PYEOF
  )
  IMP_RC=$?
  if [ "$IMP_RC" -ne 0 ]; then
    echo "BLOCKED: import-smoke regression in staged files:"
    echo "$IMPORT_OUT" | sed 's/^/  /'
    echo
    echo "  Fix the NameError/ImportError before committing. Run locally:"
    echo "    python3 -c 'import <module>'"
    echo
    FAIL=1
  fi
fi

# --------------------------------------------------------------------------
# 3. shellcheck on staged .sh files (skip silently if absent).
# --------------------------------------------------------------------------
if command -v shellcheck >/dev/null 2>&1; then
  SH_TARGETS=$(echo "$STAGED" | grep -E '\.sh$' || true)
  if [ -n "$SH_TARGETS" ]; then
    SC_FAIL=0
    SC_OUT=""
    while IFS= read -r f; do
      [ -f "$f" ] || continue
      if ! out=$(shellcheck -S error "$f" 2>&1); then
        SC_OUT="$SC_OUT$out"$'\n'
        SC_FAIL=1
      fi
    done <<< "$SH_TARGETS"
    if [ "$SC_FAIL" -eq 1 ]; then
      echo "BLOCKED: shellcheck errors in staged shell scripts:"
      echo "$SC_OUT" | sed 's/^/  /'
      FAIL=1
    fi
  fi
fi

if [ "$FAIL" -eq 1 ]; then
  echo "Pre-commit ZSF/import-smoke gate failed."
  echo "Bypass (emergency only): git commit --no-verify"
  exit 1
fi

exit 0
