#!/usr/bin/env bash
# mf-test-fast.sh — multi-fleet "CI/PR" test tier (BB3/CC3 fix, 2026-05-07).
#
# Runs the default multi-fleet pytest suite — benchmark suite excluded by
# default (configured in `multi-fleet/pyproject.toml` `[tool.pytest.ini_options]`),
# 30s per-test timeout via `pytest-timeout~=2.3`.
#
# See:
#   multi-fleet/tests/RUN.md
#   .fleet/audits/2026-05-07-CC3-pytest-hang-fix-applied.md
#
# Other tiers:
#   Pre-commit (≤30s): pytest -m "not benchmark and not integration"
#   Nightly (~5min):   pytest --timeout=60 --timeout-method=signal
#   Release (~10min):  pytest -m benchmark --timeout=600
#
# Usage:
#   bash scripts/mf-test-fast.sh            # default suite
#   bash scripts/mf-test-fast.sh -k arbiter # filter by name
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MF_DIR="${REPO_ROOT}/multi-fleet"
VENV_PY="${MF_DIR}/venv.nosync/bin/python"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "[mf-test-fast] venv not found at ${VENV_PY}" >&2
  echo "[mf-test-fast] expected: multi-fleet/venv.nosync/bin/python" >&2
  exit 2
fi

# Verify pytest-timeout is installed (BB3/CC3 fix). ZSF — surface failure.
if ! "${VENV_PY}" -c 'import pytest_timeout' 2>/dev/null; then
  echo "[mf-test-fast] pytest-timeout not installed — running:" >&2
  echo "[mf-test-fast]   pip install 'pytest-timeout~=2.3'" >&2
  "${MF_DIR}/venv.nosync/bin/pip" install 'pytest-timeout~=2.3'
fi

cd "${MF_DIR}"
exec "${VENV_PY}" -m pytest -q "$@"
