#!/usr/bin/env bash
# Deploy (or update) the iam-key-rotator Lambda + execution role.
#
# Idempotent: safe to re-run. Updates code and policy if they already exist.
#
# Usage:
#   ./deploy.sh
#
# Requires: AWS credentials with iam:*, lambda:*, logs:* on the target account.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-west-2}"
ROLE_NAME="iam-key-rotator-role"
FN_NAME="iam-key-rotator"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo "[deploy] account=$ACCOUNT_ID region=$REGION"

# ---- 1. Execution role ------------------------------------------------------
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "[deploy] creating role $ROLE_NAME"
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "file://$HERE/trust-policy.json" \
        --description "Execution role for SM IAM key rotator Lambda" \
        --tags Key=project,Value=ersim Key=managed-by,Value=atlas-fleet >/dev/null
else
    echo "[deploy] role $ROLE_NAME exists; updating trust policy"
    aws iam update-assume-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-document "file://$HERE/trust-policy.json"
fi

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "iam-key-rotator-policy" \
    --policy-document "file://$HERE/policy.json"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo "[deploy] role_arn=$ROLE_ARN"

# ---- 2. Package -------------------------------------------------------------
rm -f "$HERE/rotator.zip"
(cd "$HERE" && zip -q rotator.zip rotator.py)

# ---- 3. Function ------------------------------------------------------------
if ! aws lambda get-function --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
    # IAM propagation race: give the new role a moment to be usable by Lambda.
    sleep 8
    aws lambda create-function \
        --function-name "$FN_NAME" \
        --runtime python3.12 \
        --role "$ROLE_ARN" \
        --handler rotator.lambda_handler \
        --zip-file "fileb://$HERE/rotator.zip" \
        --timeout 120 \
        --memory-size 256 \
        --region "$REGION" \
        --description "SM rotator for IAM access keys under /ersim/prod/iam/*" \
        --tags project=ersim,managed-by=atlas-fleet >/dev/null
else
    aws lambda update-function-code \
        --function-name "$FN_NAME" \
        --zip-file "fileb://$HERE/rotator.zip" \
        --region "$REGION" >/dev/null
    aws lambda wait function-updated --function-name "$FN_NAME" --region "$REGION"
    aws lambda update-function-configuration \
        --function-name "$FN_NAME" \
        --role "$ROLE_ARN" \
        --timeout 120 \
        --memory-size 256 \
        --region "$REGION" >/dev/null
fi

# ---- 4. SM invoke permission (idempotent) -----------------------------------
if ! aws lambda get-policy --function-name "$FN_NAME" --region "$REGION" 2>/dev/null \
        | grep -q '"Sid":"sm-rotate"'; then
    aws lambda add-permission \
        --function-name "$FN_NAME" \
        --principal secretsmanager.amazonaws.com \
        --action lambda:InvokeFunction \
        --statement-id sm-rotate \
        --region "$REGION" >/dev/null
fi

FN_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FN_NAME}"
echo "[deploy] function_arn=$FN_ARN"
echo "[deploy] done"
