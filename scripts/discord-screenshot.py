#!/usr/bin/env python3
"""Screenshot-to-Discord: capture screen and post to fleet Discord channel.

Usage:
    python3 scripts/discord-screenshot.py                    # Full screen
    python3 scripts/discord-screenshot.py --caption "look"   # With caption
    python3 scripts/discord-screenshot.py --file /path/to.png # Existing image
    python3 scripts/discord-screenshot.py --interactive       # Region select

Requires FLEET_DISCORD_CHANNEL_ID env var or keychain entry.
Bot token from keychain (DISCORD_BOT_TOKEN) or env var.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("discord-screenshot")

DISCORD_API = "https://discord.com/api/v10"


def get_bot_token() -> str:
    """Get Discord bot token from env or keychain."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        return token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "fleet", "-s", "DISCORD_BOT_TOKEN", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_channel_id() -> str:
    return os.environ.get("FLEET_DISCORD_CHANNEL_ID", "")


def capture_screenshot(interactive: bool = False) -> str:
    """Capture screenshot using macOS screencapture. Returns path to PNG."""
    path = os.path.join(tempfile.gettempdir(), f"fleet-screenshot-{int(time.time())}.png")
    cmd = ["screencapture"]
    if interactive:
        cmd.append("-i")  # interactive region select
    else:
        cmd.append("-x")  # silent (no shutter sound)
    cmd.append(path)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"screencapture failed: {result.stderr}")
    if not Path(path).exists():
        raise RuntimeError("Screenshot cancelled or failed — no file created")
    return path


def post_image_to_discord(
    image_path: str,
    channel_id: str,
    bot_token: str,
    caption: str = "",
    node_id: str = "",
) -> dict:
    """Upload image to Discord channel via REST multipart/form-data.

    Discord REST API accepts multipart with:
    - payload_json: JSON string with content/embeds
    - files[0]: the image file
    """
    node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "mac1")
    filename = Path(image_path).name

    # Build multipart body manually (stdlib only, no requests)
    boundary = f"----FleetScreenshot{int(time.time() * 1000)}"
    body_parts = []

    # Part 1: payload_json
    payload = {
        "content": caption or f"[{node_id.upper()}] Screenshot",
    }
    body_parts.append(f"--{boundary}\r\n")
    body_parts.append('Content-Disposition: form-data; name="payload_json"\r\n')
    body_parts.append("Content-Type: application/json\r\n\r\n")
    body_parts.append(json.dumps(payload))
    body_parts.append("\r\n")

    # Part 2: file
    body_parts.append(f"--{boundary}\r\n")
    body_parts.append(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n')
    body_parts.append("Content-Type: image/png\r\n\r\n")

    # Convert text parts to bytes, then append binary file content
    pre_file = "".join(body_parts).encode("utf-8")

    with open(image_path, "rb") as f:
        file_data = f.read()

    post_file = f"\r\n--{boundary}--\r\n".encode("utf-8")

    full_body = pre_file + file_data + post_file

    req = Request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        data=full_body,
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "message_id": result.get("id"), "channel_id": channel_id}
    except URLError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Screenshot to Discord")
    parser.add_argument("--file", "-f", help="Existing image file to upload (skip capture)")
    parser.add_argument("--caption", "-c", default="", help="Caption for the image")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive region select")
    parser.add_argument("--node", default="", help="Node ID (default: MULTIFLEET_NODE_ID or mac1)")
    args = parser.parse_args()

    token = get_bot_token()
    if not token:
        print("ERROR: No Discord bot token. Set DISCORD_BOT_TOKEN or add to keychain.", file=sys.stderr)
        sys.exit(1)

    channel_id = get_channel_id()
    if not channel_id:
        print("ERROR: Set FLEET_DISCORD_CHANNEL_ID env var.", file=sys.stderr)
        sys.exit(1)

    # Get or capture image
    if args.file:
        image_path = args.file
        if not Path(image_path).exists():
            print(f"ERROR: File not found: {image_path}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Capturing screenshot...")
        image_path = capture_screenshot(interactive=args.interactive)
        print(f"Captured: {image_path}")

    # Upload
    print("Uploading to Discord...")
    result = post_image_to_discord(
        image_path=image_path,
        channel_id=channel_id,
        bot_token=token,
        caption=args.caption,
        node_id=args.node,
    )

    if result["ok"]:
        print(f"Posted to Discord (message {result.get('message_id', '?')})")
    else:
        print(f"ERROR: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
