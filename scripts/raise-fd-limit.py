"""Idempotent FD-limit patcher for fleet daemon plist.

Tries io.contextdna.fleet-nats.plist first (mac1+mac3 convention), then
io.contextdna.fleet-nerve.plist (mac2 convention). Auto-detects canonical
label per node. Keeps a .bak before write. Safe to re-run.

Usage:
    python3 scripts/raise-fd-limit.py             # apply
    python3 scripts/raise-fd-limit.py --dry-run    # preview only

Output (parseable):
    NONE-FOUND               — no canonical plist on this node
    ALREADY-SET: <path>      — limits already raised; no-op
    PATCHED: <path> LABEL=<>  — file was modified; .bak written
    DRY-PATCHED: <path>       — --dry-run; no file change
"""

from __future__ import annotations

import os
import re
import sys

SOFT_FD = 4096
HARD_FD = 8192

CANDIDATES = [
    "~/Library/LaunchAgents/io.contextdna.fleet-nats.plist",
    "~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist",
]

INSERT = f"""    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>{SOFT_FD}</integer>
    </dict>
    <key>HardResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>{HARD_FD}</integer>
    </dict>
"""


def main() -> int:
    dry = "--dry-run" in sys.argv
    target = next(
        (p for p in (os.path.expanduser(c) for c in CANDIDATES) if os.path.exists(p)),
        None,
    )
    if not target:
        print("NONE-FOUND")
        return 0

    text = open(target).read()
    if "SoftResourceLimits" in text and str(SOFT_FD) in text:
        print(f"ALREADY-SET: {target}")
        return 0

    label = os.path.basename(target).replace(".plist", "")

    if dry:
        print(f"DRY-PATCHED: {target} LABEL={label}")
        return 0

    new_text = re.sub(r"(</dict>\s*</plist>\s*)$", INSERT + r"\1", text, count=1)
    open(target + ".bak", "w").write(text)
    open(target, "w").write(new_text)
    print(f"PATCHED: {target} LABEL={label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
