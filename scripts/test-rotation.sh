#!/usr/bin/env bash
# test-rotation.sh — end-to-end rotation smoke test using a throwaway IAM user.
#
# 1. Create a disposable IAM user (ersim-rotation-test-<ts>) + first access key.
# 2. Create a matching Secrets Manager secret (/ersim/prod/iam/ersim-rotation-test-<ts>).
# 3. Attach iam-key-rotator Lambda and trigger one rotation.
# 4. Poll until rotation completes, verify a fresh key is present and works.
# 5. Destroy the user, the secret, and all keys.
#
# Exits non-zero (and still tries to clean up) on any failure.
#
# Requires: aws cli logged in as a principal with iam:*/secretsmanager:*/sts:* in us-west-2.

set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:iam-key-rotator"

TS="$(date +%s)"
USER="ersim-rotation-test-${TS}"
SECRET_NAME="/ersim/prod/iam/${USER}"
TMP="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
    echo "[cleanup] tearing down ${USER} and ${SECRET_NAME}"
    set +e
    # Disable + delete all keys owned by the user.
    for k in $(aws iam list-access-keys --user-name "$USER" \
                --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
        aws iam update-access-key --user-name "$USER" --access-key-id "$k" --status Inactive >/dev/null 2>&1
        aws iam delete-access-key --user-name "$USER" --access-key-id "$k" >/dev/null 2>&1
    done
    aws iam delete-user --user-name "$USER" >/dev/null 2>&1
    aws secretsmanager delete-secret --secret-id "$SECRET_NAME" \
        --force-delete-without-recovery --region "$REGION" >/dev/null 2>&1
    rm -rf "$TMP"
}

fail() {
    echo "[FAIL] $1" >&2
    exit 1
}

echo "[setup] creating IAM user $USER"
aws iam create-user --user-name "$USER" \
    --tags Key=purpose,Value=rotation-smoke-test >/dev/null

echo "[setup] minting initial access key"
aws iam create-access-key --user-name "$USER" --output json > "$TMP/key.json"
INITIAL_AK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["AccessKey"]["AccessKeyId"])' "$TMP/key.json")"
python3 - "$TMP/key.json" "$USER" > "$TMP/secret.json" <<'PY'
import json, sys, datetime as d
k = json.load(open(sys.argv[1]))["AccessKey"]
print(json.dumps({
    "access_key_id": k["AccessKeyId"],
    "secret_access_key": k["SecretAccessKey"],
    "user": sys.argv[2],
    "rotated": d.date.today().isoformat(),
}))
PY

echo "[setup] creating secret $SECRET_NAME"
aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --secret-string "file://$TMP/secret.json" \
    --region "$REGION" \
    --tags Key=purpose,Value=rotation-smoke-test >/dev/null

echo "[setup] attaching rotation Lambda"
aws secretsmanager rotate-secret \
    --secret-id "$SECRET_NAME" \
    --rotation-lambda-arn "$LAMBDA_ARN" \
    --rotation-rules AutomaticallyAfterDays=30 \
    --region "$REGION" >/dev/null

echo "[test] waiting ~45s for IAM propagation before forcing rotation"
sleep 15

echo "[test] forcing immediate rotation"
aws secretsmanager rotate-secret \
    --secret-id "$SECRET_NAME" \
    --region "$REGION" >/dev/null

echo "[test] polling for rotation completion"
for i in $(seq 1 30); do
    sleep 5
    STATE="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" \
        --query 'VersionIdsToStages' --output json)"
    if ! echo "$STATE" | grep -q AWSPENDING; then
        # No AWSPENDING stage means rotation settled (success or never started).
        NEW_AK="$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region "$REGION" \
            --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_key_id"])')"
        if [ "$NEW_AK" != "$INITIAL_AK" ]; then
            echo "[test] rotation succeeded: $INITIAL_AK -> $NEW_AK"
            break
        fi
    fi
    echo "[test] ..still rotating (attempt $i)"
done

NEW_AK="$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region "$REGION" \
    --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_key_id"])')"
NEW_SK="$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region "$REGION" \
    --query SecretString --output text | python3 -c 'import sys,json; print(json.load(sys.stdin)["secret_access_key"])')"

[ "$NEW_AK" != "$INITIAL_AK" ] || fail "secret still holds original key $INITIAL_AK"

echo "[verify] sts get-caller-identity with rotated key"
AWS_ACCESS_KEY_ID="$NEW_AK" AWS_SECRET_ACCESS_KEY="$NEW_SK" \
    aws sts get-caller-identity --region "$REGION" --output json > "$TMP/ident.json" \
    || fail "rotated key failed sts get-caller-identity"

IDENT_USER="$(python3 -c 'import json,sys; arn=json.load(open(sys.argv[1]))["Arn"]; print(arn.rsplit("/",1)[-1])' "$TMP/ident.json")"
[ "$IDENT_USER" = "$USER" ] || fail "rotated key identity mismatch: got $IDENT_USER expected $USER"

echo "[PASS] rotation round-trip verified for $USER"
