#!/usr/bin/env bash
# assume-admin.sh — MFA-gated break-glass admin shell for ER Simulator AWS account.
#
# Purpose:
#   Replaces the old atlas-temp-admin standing access key with a role-assumption
#   pattern. You remain the low-privilege terraform-ersim-prod user by default;
#   this script elevates you to AdminAccess-Role for 1 hour with MFA.
#
# Usage:
#   eval $(./scripts/assume-admin.sh)
#
# Then, within that shell, you have admin credentials for 3600 seconds.
# Open a new shell (or unset AWS_* vars) to drop back to base identity.
#
# Requirements:
#   - AWS CLI v2 configured for the terraform-ersim-prod user (default profile).
#   - Region us-west-2 (or AWS_REGION env).
#   - An MFA device registered against your IAM user.
#   - Secrets Manager secret /ersim/iam/admin-role-arn populated.
#
# Idempotent: safe to re-run. Each call produces a fresh 1h session.

set -euo pipefail

SECRET_ID="/ersim/iam/admin-role-arn"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
DURATION=3600

log() { printf '# %s\n' "$*" >&2; }
die() { printf 'assume-admin: %s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || die "aws CLI not found on PATH"
command -v jq  >/dev/null 2>&1 || die "jq not found on PATH (brew install jq)"

# 1. Fetch role ARN from Secrets Manager (no inline secrets in repo).
log "Fetching role ARN from Secrets Manager (${SECRET_ID}) in ${REGION}..."
ROLE_ARN=$(AWS_PAGER="" aws secretsmanager get-secret-value \
    --secret-id "${SECRET_ID}" \
    --region "${REGION}" \
    --query SecretString \
    --output text 2>/dev/null) \
  || die "could not read ${SECRET_ID}. Do you have sts:GetSecretValue and is the secret populated?"

[[ "${ROLE_ARN}" =~ ^arn:aws:iam::[0-9]+:role/.+ ]] \
  || die "unexpected secret payload (not a role ARN): ${ROLE_ARN}"

log "Role: ${ROLE_ARN}"

# 2. Resolve MFA serial. Auto-detect from caller's IAM user if possible; fall back
#    to an interactive prompt so Aaron can paste a hardware token ARN.
CALLER_ARN=$(AWS_PAGER="" aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)
MFA_SERIAL_DEFAULT=""
if [[ "${CALLER_ARN}" =~ :user/(.+)$ ]]; then
    USER_NAME="${BASH_REMATCH[1]}"
    MFA_SERIAL_DEFAULT=$(AWS_PAGER="" aws iam list-mfa-devices \
        --user-name "${USER_NAME}" \
        --query 'MFADevices[0].SerialNumber' \
        --output text 2>/dev/null || true)
    [[ "${MFA_SERIAL_DEFAULT}" == "None" ]] && MFA_SERIAL_DEFAULT=""
fi

if [[ -n "${MFA_SERIAL_DEFAULT}" ]]; then
    printf 'MFA serial [%s]: ' "${MFA_SERIAL_DEFAULT}" >&2
else
    printf 'MFA serial: ' >&2
fi
read -r MFA_SERIAL </dev/tty
MFA_SERIAL="${MFA_SERIAL:-${MFA_SERIAL_DEFAULT}}"
[[ -n "${MFA_SERIAL}" ]] || die "MFA serial required (arn:aws:iam::ACCT:mfa/USER or hardware serial)"

printf 'MFA code: ' >&2
read -r MFA_CODE </dev/tty
[[ "${MFA_CODE}" =~ ^[0-9]{6}$ ]] \
  || die "MFA code must be 6 digits"

# 3. Assume role with MFA, producing temporary credentials valid for 1 hour.
SESSION_NAME="admin-$(date +%s)"
log "Calling sts:AssumeRole (session=${SESSION_NAME}, duration=${DURATION}s)..."

CRED_JSON=$(AWS_PAGER="" aws sts assume-role \
    --role-arn "${ROLE_ARN}" \
    --role-session-name "${SESSION_NAME}" \
    --serial-number "${MFA_SERIAL}" \
    --token-code "${MFA_CODE}" \
    --duration-seconds "${DURATION}" \
    --region "${REGION}" \
    --output json) \
  || die "sts:AssumeRole failed (bad MFA code? trust policy mismatch? role missing?)"

AK=$(printf '%s' "${CRED_JSON}" | jq -r '.Credentials.AccessKeyId')
SK=$(printf '%s' "${CRED_JSON}" | jq -r '.Credentials.SecretAccessKey')
ST=$(printf '%s' "${CRED_JSON}" | jq -r '.Credentials.SessionToken')
EX=$(printf '%s' "${CRED_JSON}" | jq -r '.Credentials.Expiration')

[[ -n "${AK}" && -n "${SK}" && -n "${ST}" && "${AK}" != "null" ]] \
  || die "assume-role returned empty credentials"

log "Success. Credentials expire: ${EX}"
log "Wrap this script with: eval \$(./scripts/assume-admin.sh)"

# 4. Emit export statements on stdout so eval(1) loads them into the caller shell.
printf 'export AWS_ACCESS_KEY_ID=%q\n'     "${AK}"
printf 'export AWS_SECRET_ACCESS_KEY=%q\n' "${SK}"
printf 'export AWS_SESSION_TOKEN=%q\n'     "${ST}"
printf 'export AWS_REGION=%q\n'            "${REGION}"
printf 'export AWS_ADMIN_EXPIRES=%q\n'     "${EX}"
