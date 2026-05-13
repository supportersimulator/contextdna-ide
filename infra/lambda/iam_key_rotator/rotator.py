"""
IAM access-key rotator Lambda for AWS Secrets Manager.

Implements the standard four-step SM rotation protocol
(createSecret, setSecret, testSecret, finishSecret) for secrets
whose payload is an IAM access-key pair stored as JSON:

    {
        "access_key_id":   "AKIA...",
        "secret_access_key": "...",
        "user":            "<iam-user-name>",
        "rotated":         "YYYY-MM-DD"
    }

The rotator creates a fresh access key for the IAM user recorded in the
secret, promotes it to AWSCURRENT, and deactivates + deletes the previous
key. Because IAM caps each user at two simultaneous keys, if the user is
already at the limit and an extra non-current key exists, that non-current
key is purged before a new key is minted.

Trigger: Secrets Manager scheduled rotation.
Event shape (from SM):
    { "SecretId": "<arn>", "ClientRequestToken": "<uuid>", "Step": "createSecret|setSecret|testSecret|finishSecret" }
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Grace period between activating the new key and deleting the old key.
# Set via env so we can override for tests without re-deploying.
OLD_KEY_GRACE_SECONDS = int(os.environ.get("OLD_KEY_GRACE_SECONDS", "30"))


def _sm() -> Any:
    return boto3.client("secretsmanager")


def _iam() -> Any:
    return boto3.client("iam")


def lambda_handler(event: dict, context: Any) -> dict:
    secret_id = event["SecretId"]
    token = event["ClientRequestToken"]
    step = event["Step"]

    logger.info("rotation step=%s secret=%s token=%s", step, secret_id, token)

    sm = _sm()
    meta = sm.describe_secret(SecretId=secret_id)
    if not meta.get("RotationEnabled", False):
        raise ValueError(f"Rotation is not enabled for {secret_id}")

    versions = meta.get("VersionIdsToStages", {})
    if token not in versions:
        raise ValueError(f"Token {token} has no stage on secret {secret_id}")

    stages = versions[token]
    if "AWSCURRENT" in stages:
        logger.info("token %s already AWSCURRENT; nothing to do", token)
        return {"status": "noop"}
    if "AWSPENDING" not in stages:
        raise ValueError(f"Token {token} not staged as AWSPENDING for {secret_id}")

    if step == "createSecret":
        _create_secret(sm, secret_id, token)
    elif step == "setSecret":
        _set_secret(sm, secret_id, token)
    elif step == "testSecret":
        _test_secret(sm, secret_id, token)
    elif step == "finishSecret":
        _finish_secret(sm, secret_id, token)
    else:
        raise ValueError(f"Invalid step: {step}")

    return {"status": "ok", "step": step}


def _get_current_payload(sm: Any, secret_id: str) -> dict:
    resp = sm.get_secret_value(SecretId=secret_id, VersionStage="AWSCURRENT")
    return json.loads(resp["SecretString"])


def _create_secret(sm: Any, secret_id: str, token: str) -> None:
    """Mint a new IAM access key and stage it as AWSPENDING."""
    try:
        sm.get_secret_value(SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING")
        logger.info("AWSPENDING already exists for %s; skipping createSecret", token)
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    current = _get_current_payload(sm, secret_id)
    user = current["user"]
    current_key_id = current["access_key_id"]

    iam = _iam()

    # Honor 2-key limit: if we're already at 2 keys, drop any non-current key.
    listed = iam.list_access_keys(UserName=user).get("AccessKeyMetadata", [])
    if len(listed) >= 2:
        for k in listed:
            if k["AccessKeyId"] != current_key_id:
                logger.warning(
                    "2-key cap hit for %s; deleting stale non-current key %s",
                    user,
                    k["AccessKeyId"],
                )
                try:
                    iam.update_access_key(
                        UserName=user,
                        AccessKeyId=k["AccessKeyId"],
                        Status="Inactive",
                    )
                except ClientError:
                    pass
                iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])

    new = iam.create_access_key(UserName=user)["AccessKey"]
    new_payload = {
        "access_key_id": new["AccessKeyId"],
        "secret_access_key": new["SecretAccessKey"],
        "user": user,
        "rotated": _dt.date.today().isoformat(),
        "previous_access_key_id": current_key_id,
    }

    sm.put_secret_value(
        SecretId=secret_id,
        ClientRequestToken=token,
        SecretString=json.dumps(new_payload),
        VersionStages=["AWSPENDING"],
    )
    logger.info("createSecret: staged new key %s for user %s", new["AccessKeyId"], user)


def _set_secret(sm: Any, secret_id: str, token: str) -> None:
    """No-op: IAM is the authoritative store; the key already exists from createSecret."""
    logger.info("setSecret: no external service to configure for IAM keys")


def _test_secret(sm: Any, secret_id: str, token: str) -> None:
    """Verify the AWSPENDING key works by calling sts:GetCallerIdentity."""
    pending = json.loads(
        sm.get_secret_value(SecretId=secret_id, VersionId=token, VersionStage="AWSPENDING")[
            "SecretString"
        ]
    )

    # IAM is eventually consistent; retry sts with linear backoff.
    last_err: Exception | None = None
    for attempt in range(8):
        try:
            sts = boto3.client(
                "sts",
                aws_access_key_id=pending["access_key_id"],
                aws_secret_access_key=pending["secret_access_key"],
            )
            ident = sts.get_caller_identity()
            logger.info("testSecret ok: arn=%s attempt=%d", ident.get("Arn"), attempt)
            return
        except ClientError as exc:
            last_err = exc
            logger.info("testSecret attempt %d failed: %s", attempt, exc)
            time.sleep(2 + attempt)

    raise RuntimeError(f"testSecret failed after retries: {last_err}")


def _finish_secret(sm: Any, secret_id: str, token: str) -> None:
    """Promote AWSPENDING to AWSCURRENT, deactivate and delete the old IAM key."""
    meta = sm.describe_secret(SecretId=secret_id)
    current_version = None
    for version_id, stages in meta["VersionIdsToStages"].items():
        if "AWSCURRENT" in stages:
            current_version = version_id
            break

    if current_version == token:
        logger.info("token %s already AWSCURRENT", token)
        return

    # Capture the soon-to-be-retired access key BEFORE we flip stages.
    old_access_key_id: str | None = None
    old_user: str | None = None
    if current_version:
        try:
            old = json.loads(
                sm.get_secret_value(
                    SecretId=secret_id,
                    VersionId=current_version,
                    VersionStage="AWSCURRENT",
                )["SecretString"]
            )
            old_access_key_id = old.get("access_key_id")
            old_user = old.get("user")
        except ClientError as exc:
            logger.warning("could not fetch outgoing AWSCURRENT: %s", exc)

    sm.update_secret_version_stage(
        SecretId=secret_id,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )
    logger.info("promoted token %s to AWSCURRENT", token)

    if old_access_key_id and old_user:
        time.sleep(OLD_KEY_GRACE_SECONDS)
        iam = _iam()
        try:
            iam.update_access_key(
                UserName=old_user,
                AccessKeyId=old_access_key_id,
                Status="Inactive",
            )
            iam.delete_access_key(UserName=old_user, AccessKeyId=old_access_key_id)
            logger.info("retired old IAM key %s for user %s", old_access_key_id, old_user)
        except ClientError as exc:
            logger.error("failed to retire old IAM key %s: %s", old_access_key_id, exc)
            raise
