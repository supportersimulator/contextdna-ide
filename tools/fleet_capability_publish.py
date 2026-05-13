#!/usr/bin/env python3
"""Post-commit hook helper — write our HEAD into NATS KV ``fleet.capability``.

Invoked by ``scripts/git-hooks/post-commit`` after every local commit.
Best-effort: a failed KV write or a NATS-down condition NEVER blocks
the commit (we exit 0). The fleet-upgrading protocol's gossip just
won't include us until the next successful publish.

Atomic-with-push behavior (3s neuro Q2 ruling): we only set
``pushed_to_origin: True`` after a successful ``git push origin <branch>``.
If push fails, we still write the entry but with ``pushed_to_origin: False``,
which causes peers to skip us during gossip.

Env overrides:
  NATS_URL            — default nats://127.0.0.1:4222
  MULTIFLEET_NODE_ID  — required to write to ``<node_id>/HEAD``
  FLEET_PUSH_ORIGIN   — set to ``0`` to skip the ``git push`` attempt
  FLEET_CAPABILITY_TEST_CMD — override DEFAULT_TEST_CMD
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("fleet_capability_publish")

# Allow running as a script: add multi-fleet/ to sys.path so multifleet.* imports work.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
_MF_PKG_PARENT = _REPO_ROOT / "multi-fleet"
if str(_MF_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_MF_PKG_PARENT))

try:
    from multifleet.fleet_upgrader import (  # noqa: E402
        CapabilityPayload,
        DEFAULT_BUCKET,
        DEFAULT_TEST_CMD,
        write_local_capability,
    )
except ImportError as e:
    logger.warning("multifleet.fleet_upgrader import failed: %s — exiting 0", e)
    sys.exit(0)


def _git(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        rc = subprocess.run(
            ["git", *args], cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        return rc.returncode, (rc.stdout or "") + (rc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 128, f"git failed: {e}"


def _build_payload(node_id: str) -> "CapabilityPayload | None":
    code, head = _git(["rev-parse", "HEAD"])
    if code != 0:
        logger.warning("rev-parse HEAD failed: %s", head)
        return None
    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip() if code == 0 else "main"

    # Optionally try to push. Atomic-with-write per neuro Q2.
    push_skip = os.environ.get("FLEET_PUSH_ORIGIN") == "0"
    pushed = False
    if not push_skip:
        code, push_out = _git(["push", "origin", branch], timeout=20)
        pushed = code == 0
        if not pushed:
            # If our push raced behind (e.g. operator ran `git push` first),
            # the commit IS on origin — verify by checking origin/<branch>
            # contains our HEAD. This is what peers actually care about.
            head_sha_code, head_sha = _git(["rev-parse", "HEAD"], timeout=5)
            if head_sha_code == 0:
                ancestor_code, _ = _git(
                    ["merge-base", "--is-ancestor", head_sha.strip(), f"origin/{branch}"],
                    timeout=5,
                )
                if ancestor_code == 0:
                    pushed = True
                    logger.info(
                        "git push reported failure but origin/%s already contains %s "
                        "— marking pushed_to_origin=True", branch, head_sha.strip()[:8],
                    )
            if not pushed:
                logger.warning(
                    "git push origin %s failed (advertise pushed_to_origin=False): %s",
                    branch, push_out.strip().splitlines()[-1:][0:1] or push_out[:120],
                )
    else:
        # Caller asked us not to push — still mark False so peers don't pull
        # a sha that may not exist on origin. (Hook-fired pushes only.)
        pushed = False

    code, dirty = _git(["status", "--porcelain"])
    dirty_count = len(dirty.splitlines()) if code == 0 else 0

    last_test_pass_ts = 0.0
    state_file = _REPO_ROOT / ".fleet-state" / "last_test_pass.ts"
    if state_file.exists():
        try:
            last_test_pass_ts = float(state_file.read_text().strip())
        except (OSError, ValueError):
            last_test_pass_ts = 0.0

    return CapabilityPayload(
        sha=head.strip(),
        branch=branch,
        pushed_to_origin=pushed,
        dirty_files_count=dirty_count,
        last_test_pass_ts=last_test_pass_ts,
        test_cmd=os.environ.get("FLEET_CAPABILITY_TEST_CMD", DEFAULT_TEST_CMD),
        updated_ts=time.time(),
        node_id=node_id,
    )


async def _publish(payload) -> bool:
    nats_url = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
    try:
        import nats  # noqa: PLC0415 — runtime-only dep; import lazily so absence doesn't fail commit
    except ImportError:
        logger.warning("nats-py not installed — skipping KV write")
        return False
    nc = await nats.connect(servers=[nats_url], connect_timeout=3)
    # Tight timeout on the whole KV write — capability gossip is best-effort,
    # and a partial cluster (mac2/mac3 offline) can hang JetStream API calls
    # for tens of seconds. Bound to 5s total.
    try:
        return await asyncio.wait_for(_publish_inner(nc, payload), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(
            "capability KV write timed out >5s (peer cluster likely degraded)"
        )
        return False
    finally:
        try:
            await nc.close()
        except Exception as _zsf_e:  # noqa: BLE001
            # EEE4/ZSF: NATS close is best-effort during shutdown; surface
            # for observability without raising into the caller's success path.
            logger.debug(
                "fleet.capability nc.close best-effort failed: %s: %s",
                type(_zsf_e).__name__, _zsf_e,
            )


async def _publish_inner(nc, payload) -> bool:
    try:
        ok = await write_local_capability(nc, DEFAULT_BUCKET, payload)
        if ok:
            logger.info(
                "fleet.capability KV updated: %s sha=%s pushed=%s",
                payload.node_id, payload.sha[:8], payload.pushed_to_origin,
            )
        return ok
    finally:
        await nc.close()


def _resolve_node_id() -> str:
    """Resolve node_id with this priority: env > fleet_config > empty.

    Hook fires from user shell which doesn't inherit the daemon's launchd
    plist env. Fall back to the same fleet_config source of truth so the
    hook works without sourcing extra env files.
    """
    env_val = os.environ.get("MULTIFLEET_NODE_ID")
    if env_val:
        return env_val
    try:
        from multifleet.fleet_config import get_node_id  # noqa: PLC0415
        cfg_val = get_node_id()
        if cfg_val:
            return cfg_val
    except (ImportError, Exception) as e:  # noqa: BLE001 — best-effort
        logger.debug("fleet_config.get_node_id failed: %s", e)
    return ""


def main() -> int:
    node_id = _resolve_node_id()
    if not node_id:
        # Soft-fail. Hook should NEVER break a commit just because the node
        # didn't set MULTIFLEET_NODE_ID yet.
        logger.warning(
            "node_id unresolved (env+fleet_config both empty) — skipping capability publish"
        )
        return 0
    payload = _build_payload(node_id)
    if payload is None:
        return 0
    try:
        asyncio.run(_publish(payload))
    except Exception as e:  # noqa: BLE001 — never break commit
        logger.warning("capability publish raised (commit unaffected): %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
