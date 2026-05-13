"""Idempotent CONTEXT_DNA_NEURO_PROVIDER patcher for fleet launchd plists.

Flips the neuro slot from local-only (qwen3-4b via ollama / mlx) to DeepSeek
across the whole fleet without code changes. Default = OFF (no env var
present). Aaron flips by changing the single ENABLE constant below to True
(or running with --enable), then re-running this script and `launchctl
kickstart`-ing the affected daemons.

Targets (in order tried; auto-detects which exist on this node):
    ~/Library/LaunchAgents/io.contextdna.fleet-nats.plist     (mac1, mac3)
    ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist    (mac2)
    ~/Library/LaunchAgents/com.contextdna.unified.plist       (helper agent)
    ~/Library/LaunchAgents/io.contextdna.llm-proxy.plist      (proxy)

Why those: `Config.discover()` in 3-surgeons reads CONTEXT_DNA_NEURO_PROVIDER
at process start. The neuro slot is invoked from
    - tools/fleet_nerve_nats.py        (fleet daemon — fleet-nats plist)
    - memory/surgery_bridge.py         (in-process via helper agent — unified plist)
so both daemons need the env var. Patching all four is idempotent + cheap.

Usage:
    python3 scripts/patch-neuro-cutover.py             # apply default (ENABLE constant)
    python3 scripts/patch-neuro-cutover.py --enable    # one-shot enable
    python3 scripts/patch-neuro-cutover.py --disable   # one-shot disable
    python3 scripts/patch-neuro-cutover.py --dry-run   # preview only

Output (parseable; one line per target):
    NONE-FOUND: <name>            — plist not present on this node (skipped)
    ALREADY-SET: <path>           — env var already at desired state; no-op
    PATCHED: <path> SET=<value>   — env var added/updated; .bak written
    PATCHED: <path> REMOVED       — env var removed (disable path); .bak written
    DRY-PATCHED: <path> would <action>

ZSF: any I/O failure prints `ERROR: <reason>` and exits non-zero so xbar /
sync-node-config can surface it. Default off — running this script with no
flags and ENABLE=False (the shipped default) leaves every node unchanged.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# -- THE FLIP -------------------------------------------------------------
# Aaron: change the next line to `ENABLE = True` to flip the whole fleet to
# DeepSeek for the neurologist surgeon, then run:
#     python3 scripts/patch-neuro-cutover.py
#     launchctl kickstart -k gui/$(id -u)/io.contextdna.fleet-nats
#     launchctl kickstart -k gui/$(id -u)/com.contextdna.unified
# (mac2 uses io.contextdna.fleet-nerve — kickstart that label instead.)
# Reverse: set ENABLE = False and re-run; .bak files preserve pre-flip state.
ENABLE = False
# -------------------------------------------------------------------------

ENV_KEY = "CONTEXT_DNA_NEURO_PROVIDER"
ENV_VALUE = "deepseek"

CANDIDATES = [
    "~/Library/LaunchAgents/io.contextdna.fleet-nats.plist",
    "~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist",
    "~/Library/LaunchAgents/com.contextdna.unified.plist",
    "~/Library/LaunchAgents/io.contextdna.llm-proxy.plist",
]

# Match an existing <key>CONTEXT_DNA_NEURO_PROVIDER</key> + adjacent <string>...
# pair. DOTALL so the whitespace between the two tags can include newlines.
RE_EXISTING_PAIR = re.compile(
    r"\s*<key>" + re.escape(ENV_KEY) + r"</key>\s*<string>[^<]*</string>",
    re.DOTALL,
)

# Find the EnvironmentVariables <dict> opening so we can inject a new
# <key>/<string> pair just inside it. Group 1 = the opening up through the
# `<dict>` line so we can preserve indentation.
RE_ENV_DICT_OPEN = re.compile(
    r"(<key>EnvironmentVariables</key>\s*<dict>)",
    re.DOTALL,
)


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _patch_one(path: str, enable: bool, dry: bool) -> str:
    """Patch a single plist. Return a status string for the caller to print."""
    name = os.path.basename(path)
    if not os.path.exists(path):
        return f"NONE-FOUND: {name}"

    try:
        text = open(path).read()
    except OSError as exc:
        return f"ERROR: read {path}: {exc}"

    has_key = RE_EXISTING_PAIR.search(text) is not None
    has_correct_value = (
        f"<key>{ENV_KEY}</key>" in text
        and f"<string>{ENV_VALUE}</string>" in text
        and has_key
    )

    if enable:
        if has_correct_value:
            return f"ALREADY-SET: {path} ({ENV_KEY}={ENV_VALUE})"
        if dry:
            action = "update existing pair" if has_key else "insert new pair"
            return f"DRY-PATCHED: {path} would {action} ({ENV_KEY}={ENV_VALUE})"

        if has_key:
            new_text = RE_EXISTING_PAIR.sub(
                f"\n\t\t<key>{ENV_KEY}</key>\n\t\t<string>{ENV_VALUE}</string>",
                text,
                count=1,
            )
        else:
            match = RE_ENV_DICT_OPEN.search(text)
            if not match:
                return f"ERROR: {path}: no <key>EnvironmentVariables</key> block found"
            insert = (
                f"\n\t\t<key>{ENV_KEY}</key>\n\t\t<string>{ENV_VALUE}</string>"
            )
            new_text = (
                text[: match.end()]
                + insert
                + text[match.end():]
            )

        try:
            open(path + ".bak", "w").write(text)
            _atomic_write(path, new_text)
        except OSError as exc:
            return f"ERROR: write {path}: {exc}"
        return f"PATCHED: {path} SET={ENV_VALUE}"

    # disable path: remove the pair if present.
    if not has_key:
        return f"ALREADY-SET: {path} (no {ENV_KEY} present)"
    if dry:
        return f"DRY-PATCHED: {path} would remove {ENV_KEY}"
    new_text = RE_EXISTING_PAIR.sub("", text, count=1)
    try:
        open(path + ".bak", "w").write(text)
        _atomic_write(path, new_text)
    except OSError as exc:
        return f"ERROR: write {path}: {exc}"
    return f"PATCHED: {path} REMOVED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--enable",
        action="store_true",
        help="One-shot: write env var to all matching plists.",
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="One-shot: remove env var from all matching plists.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only; no file changes."
    )
    args = parser.parse_args()

    if args.enable and args.disable:
        print("ERROR: --enable and --disable are mutually exclusive", file=sys.stderr)
        return 2
    if args.enable:
        enable = True
    elif args.disable:
        enable = False
    else:
        enable = ENABLE

    any_error = False
    for cand in CANDIDATES:
        path = os.path.expanduser(cand)
        result = _patch_one(path, enable=enable, dry=args.dry_run)
        print(result)
        if result.startswith("ERROR:"):
            any_error = True

    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
