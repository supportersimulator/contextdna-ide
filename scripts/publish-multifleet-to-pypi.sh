#!/usr/bin/env bash
# publish-multifleet-to-pypi.sh — PyPI publish for project `multifleet`.
#
# Pulls PyPI API token from AWS Secrets Manager (/ersim/prod/pypi/multifleet-token),
# rebuilds sdist + wheel from multi-fleet/, runs twine check, then uploads.
#
# Aborts if the stored secret still equals the placeholder value. Aaron must
# first write the real token to that secret via:
#
#   aws secretsmanager put-secret-value \
#     --secret-id /ersim/prod/pypi/multifleet-token \
#     --secret-string 'pypi-AgEI...' \
#     --region us-west-2
#
# After that, this script is the 1-command publish operation.
#
# Idempotent: rebuilds dist/ from scratch each run; twine refuses to re-upload
# an already-published version, which is the correct behavior.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PKG_DIR="${REPO_ROOT}/multi-fleet"

SECRET_ID="/ersim/prod/pypi/multifleet-token"
AWS_REGION="${AWS_REGION:-us-west-2}"
PLACEHOLDER="REPLACE_WITH_PYPI_API_TOKEN_SCOPED_TO_multifleet_PROJECT"

PYTHON_BIN="${PYTHON_BIN:-/usr/local/bin/python3}"

log()  { printf '[publish] %s\n' "$*"; }
die()  { printf '[publish] ERROR: %s\n' "$*" >&2; exit 1; }

# --- sanity checks ---
[ -d "${PKG_DIR}" ]       || die "multi-fleet/ not found at ${PKG_DIR}"
[ -f "${PKG_DIR}/pyproject.toml" ] || die "pyproject.toml missing under ${PKG_DIR}"
command -v aws >/dev/null || die "aws CLI not on PATH"
command -v twine >/dev/null || die "twine not on PATH (pip install twine)"
"${PYTHON_BIN}" -c 'import build' >/dev/null 2>&1 \
    || die "python 'build' module missing (${PYTHON_BIN} -m pip install build)"

# --- fetch token from Secrets Manager ---
log "fetching PyPI token from ${SECRET_ID} (${AWS_REGION})"
TWINE_TOKEN="$(aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${AWS_REGION}" \
    --query SecretString \
    --output text)" \
    || die "could not read ${SECRET_ID} — check AWS creds & region"

[ -n "${TWINE_TOKEN}" ] || die "secret ${SECRET_ID} is empty"

if [ "${TWINE_TOKEN}" = "${PLACEHOLDER}" ]; then
    die "secret ${SECRET_ID} still holds placeholder value; write the real PyPI token first"
fi

case "${TWINE_TOKEN}" in
    pypi-*) : ;;  # expected prefix for PyPI API tokens
    *) die "token does not look like a PyPI API token (expected pypi-... prefix)" ;;
esac

# --- rebuild dist/ ---
log "cleaning ${PKG_DIR}/dist ${PKG_DIR}/build"
(
    cd "${PKG_DIR}"
    rm -rf dist build
    find . -maxdepth 2 -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
)

log "building sdist + wheel via ${PYTHON_BIN} -m build"
(
    cd "${PKG_DIR}"
    "${PYTHON_BIN}" -m build
)

log "running twine check"
twine check "${PKG_DIR}"/dist/*

# --- upload ---
log "uploading to PyPI (stdin-fed credentials, never cmdline)"
# TWINE_USERNAME=__token__ + TWINE_PASSWORD=<token> is the documented auth flow
# for PyPI API tokens. Token is injected via env here (scoped to this invocation)
# and never echoed or written to disk.
TWINE_USERNAME=__token__ TWINE_PASSWORD="${TWINE_TOKEN}" \
    twine upload --non-interactive "${PKG_DIR}"/dist/*

log "upload complete — https://pypi.org/project/multifleet/"
