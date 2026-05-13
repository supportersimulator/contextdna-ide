#!/usr/bin/env python3
"""
Fleet Nerve Cloud Channel (P0) — RemoteTrigger-based cloud dispatch.

Spawns an isolated Claude Code session in Anthropic's cloud via the
RemoteTrigger API. Used as the highest-priority channel when:
  - Explicitly requested by the user/caller
  - All 7 local channels (P1-P7) have failed
  - The task is cloud-specific (no local machine needed)

Requires: ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN env var.

Usage:
  from tools.fleet_nerve_cloud import send_cloud

  result = await send_cloud(
      prompt="Run tests on main branch and report failures",
      repo_url="git@github.com:org/repo.git",
      target_branch="main",
      payload={"from": "mac1", "type": "task", "body": "..."},
  )
  print(result)
  # {"delivered": True, "channel": "P0_cloud", "trigger_id": "...", "run_id": "..."}
"""

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("fleet.cloud")

REMOTE_TRIGGER_API = "https://api.anthropic.com/v1/triggers"
DEFAULT_REPO_URL = "git@github.com:supportersimulator/er-simulator-superrepo.git"
DEFAULT_BRANCH = "main"


def _get_auth_token() -> Optional[str]:
    """Get auth token from env. Checks ANTHROPIC_API_KEY first, then CLAUDE_CODE_OAUTH_TOKEN."""
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


def _build_trigger_prompt(
    prompt: str,
    payload: Optional[dict] = None,
    repo_url: str = DEFAULT_REPO_URL,
    target_branch: str = DEFAULT_BRANCH,
) -> str:
    """Build the full prompt that the cloud agent will receive.

    Includes: original task, fleet payload context, repo/branch info,
    and instructions for the cloud agent to execute and report back.
    """
    parts = [
        f"You are a cloud fleet worker for the er-simulator-superrepo fleet.",
        f"",
        f"## Repository",
        f"- URL: {repo_url}",
        f"- Branch: {target_branch}",
        f"",
        f"## Task",
        f"{prompt}",
    ]

    if payload:
        parts.extend([
            f"",
            f"## Fleet Message Context",
            f"```json",
            f"{json.dumps(payload, indent=2, default=str)}",
            f"```",
        ])

    parts.extend([
        f"",
        f"## Instructions",
        f"1. Work in the repo checkout provided.",
        f"2. Complete the task described above.",
        f"3. Summarize what you did and any results at the end.",
        f"4. If you create changes, commit them to a branch named cloud/<task-id>.",
    ])

    return "\n".join(parts)


def _api_request(method: str, url: str, data: Optional[dict], token: str) -> dict:
    """Make an authenticated API request to the RemoteTrigger API."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"RemoteTrigger API error {e.code}: {error_body}")
        raise RuntimeError(f"RemoteTrigger API {e.code}: {error_body}") from e
    except urllib.error.URLError as e:
        logger.error(f"RemoteTrigger API connection error: {e.reason}")
        raise RuntimeError(f"RemoteTrigger API connection error: {e.reason}") from e


def _create_trigger(prompt: str, token: str, repo_url: str, target_branch: str) -> dict:
    """Create a new RemoteTrigger.

    Returns the trigger object with id, status, etc.
    """
    payload = {
        "prompt": prompt,
        "repo": {
            "url": repo_url,
            "branch": target_branch,
        },
    }
    return _api_request("POST", REMOTE_TRIGGER_API, payload, token)


def _run_trigger(trigger_id: str, token: str) -> dict:
    """Run an existing trigger immediately.

    Returns the run object with run_id, status, etc.
    """
    url = f"{REMOTE_TRIGGER_API}/{trigger_id}/runs"
    return _api_request("POST", url, {}, token)


def _get_run_status(trigger_id: str, run_id: str, token: str) -> dict:
    """Check the status of a trigger run."""
    url = f"{REMOTE_TRIGGER_API}/{trigger_id}/runs/{run_id}"
    return _api_request("GET", url, None, token)


async def send_cloud(
    prompt: str,
    repo_url: str = DEFAULT_REPO_URL,
    target_branch: str = DEFAULT_BRANCH,
    payload: Optional[dict] = None,
    wait_for_completion: bool = False,
    timeout_s: float = 300.0,
) -> dict:
    """Send a task to a cloud agent via RemoteTrigger API.

    Args:
        prompt: The task description for the cloud agent.
        repo_url: Git repository URL for the cloud checkout.
        target_branch: Branch to check out in the cloud.
        payload: Optional fleet message payload for context.
        wait_for_completion: If True, poll until the run completes.
        timeout_s: Max seconds to wait if wait_for_completion is True.

    Returns:
        dict with keys: delivered, channel, trigger_id, run_id, status, error
    """
    token = _get_auth_token()
    if not token:
        logger.warning("P0 cloud: no auth token (set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)")
        return {
            "delivered": False,
            "channel": "P0_cloud",
            "error": "No auth token. Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.",
        }

    full_prompt = _build_trigger_prompt(prompt, payload, repo_url, target_branch)

    try:
        # Step 1: Create the trigger
        trigger = _create_trigger(full_prompt, token, repo_url, target_branch)
        trigger_id = trigger.get("id")
        if not trigger_id:
            return {
                "delivered": False,
                "channel": "P0_cloud",
                "error": f"Trigger creation returned no ID: {trigger}",
            }
        logger.info(f"P0 cloud: created trigger {trigger_id}")

        # Step 2: Run the trigger
        run = _run_trigger(trigger_id, token)
        run_id = run.get("id") or run.get("run_id")
        logger.info(f"P0 cloud: started run {run_id} for trigger {trigger_id}")

        result = {
            "delivered": True,
            "channel": "P0_cloud",
            "trigger_id": trigger_id,
            "run_id": run_id,
            "status": run.get("status", "started"),
        }

        # Step 3: Optionally wait for completion
        if wait_for_completion and run_id:
            import asyncio
            import time

            start = time.monotonic()
            while time.monotonic() - start < timeout_s:
                await asyncio.sleep(5)
                try:
                    status = _get_run_status(trigger_id, run_id, token)
                    run_status = status.get("status", "unknown")
                    result["status"] = run_status
                    if run_status in ("completed", "failed", "cancelled"):
                        result["output"] = status.get("output", "")
                        if run_status != "completed":
                            result["error"] = status.get("error", f"Run {run_status}")
                        break
                except Exception as e:
                    logger.debug(f"P0 cloud: status poll error: {e}")
            else:
                result["status"] = "timeout"
                result["error"] = f"Timed out after {timeout_s}s"

        return result

    except Exception as e:
        logger.error(f"P0 cloud send failed: {e}")
        return {
            "delivered": False,
            "channel": "P0_cloud",
            "error": str(e),
        }


def send_cloud_sync(
    prompt: str,
    repo_url: str = DEFAULT_REPO_URL,
    target_branch: str = DEFAULT_BRANCH,
    payload: Optional[dict] = None,
) -> dict:
    """Synchronous wrapper for send_cloud (fire-and-forget, no waiting).

    Convenience for callers that don't have an async event loop.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context — create a task (caller must await)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                send_cloud(prompt, repo_url, target_branch, payload),
            )
            return future.result(timeout=60)
    else:
        return asyncio.run(
            send_cloud(prompt, repo_url, target_branch, payload)
        )
