#!/usr/bin/env python3
"""Apply the null-safe em1() patch to Claude Code's cli.js.

Fixes: TypeError: Cannot read properties of null (reading 'alwaysThinking')
       (raised inside MCP/bridge code paths when em1() returns null mid-call).

Patch: rewrite `em1().alwaysThinking` -> `(em1()||{}).alwaysThinking` so the
property access is null-safe. The bug surfaces under multifleet bridge load
and corrupts Claude Code sessions.

Usage:
    python3 apply.py            # apply patch (default)
    python3 apply.py --apply    # explicit apply
    python3 apply.py --check    # report state, no changes
    python3 apply.py --revert   # restore latest sha-pinned backup

Idempotent. Safe to re-run after `npm i -g @anthropic-ai/claude-code`.
Backups are sha256-pinned so we never overwrite a known-good original.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

CLI_JS = Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js")
PATCH_DIR = Path(__file__).resolve().parent

# Exact substring to find (unpatched) and the replacement (patched).
OLD = "em1().alwaysThinking"
NEW = "(em1()||{}).alwaysThinking"

# Marker string that, if present, means we already patched this file.
PATCH_MARKER = NEW


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _backup_path_for(content_bytes: bytes) -> Path:
    """Backup path keyed by sha256 of pre-patch content (first 16 hex chars)."""
    digest = _sha256(content_bytes)[:16]
    return PATCH_DIR / f".cli.js.{digest}.bak"


def _list_backups() -> list[Path]:
    return sorted(PATCH_DIR.glob(".cli.js.*.bak"))


def _count(content: str, needle: str) -> int:
    return content.count(needle)


def cmd_check() -> int:
    if not CLI_JS.is_file():
        print(f"MISSING: {CLI_JS}", file=sys.stderr)
        return 2
    content = CLI_JS.read_text(encoding="utf-8", errors="surrogatepass")
    # OLD ("em1().alwaysThinking") and NEW ("(em1()||{}).alwaysThinking") are
    # disjoint — neither is a substring of the other (em1(). vs em1()||{}).).
    pure_unpatched = _count(content, OLD)
    patched = _count(content, NEW)
    sha = _sha256(content.encode("utf-8", errors="surrogatepass"))[:16]
    print(f"cli.js: {CLI_JS}")
    print(f"sha256[:16]: {sha}")
    print(f"size: {CLI_JS.stat().st_size:,} bytes")
    print(f"patched sites:   {patched}")
    print(f"unpatched sites: {pure_unpatched}")
    backups = _list_backups()
    print(f"local backups:   {len(backups)}")
    for b in backups[-3:]:
        print(f"  - {b.name}")
    if pure_unpatched == 0 and patched > 0:
        print(f"\nSTATE: already patched ({patched} sites)")
        return 0
    if pure_unpatched == 0 and patched == 0:
        print("\nSTATE: pattern not found — upstream may have refactored em1().")
        return 3
    print(f"\nSTATE: needs patching ({pure_unpatched} unpatched sites)")
    return 1


def cmd_apply() -> int:
    if not CLI_JS.is_file():
        print(f"ERROR: {CLI_JS} missing — is claude-code installed globally?", file=sys.stderr)
        return 2
    content = CLI_JS.read_text(encoding="utf-8", errors="surrogatepass")
    pure_unpatched = _count(content, OLD)
    patched = _count(content, NEW)
    if pure_unpatched == 0:
        if patched > 0:
            print(f"SKIP: already patched ({patched} sites). No changes.")
            return 0
        print("ERROR: pattern not found — upstream cli.js may have changed.", file=sys.stderr)
        return 3

    # Backup pre-patch content (sha-pinned, idempotent).
    backup = _backup_path_for(content.encode("utf-8", errors="surrogatepass"))
    if not backup.is_file():
        shutil.copy2(CLI_JS, backup)
        print(f"BACKUP: {backup.name} ({CLI_JS.stat().st_size:,} bytes)")
    else:
        print(f"BACKUP: {backup.name} (exists, reusing)")

    # Write a small manifest entry so we can audit when patches were applied.
    manifest = PATCH_DIR / "applied.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pre_sha = _sha256(content.encode("utf-8", errors="surrogatepass"))[:16]

    # Apply replacement. OLD and NEW are disjoint substrings, so str.replace
    # is safe and exact — no risk of re-matching inserted text.
    new_content = content.replace(OLD, NEW)
    # Sanity check: post-patch should have ZERO unpatched sites.
    post_pure_unpatched = _count(new_content, OLD)
    if post_pure_unpatched != 0:
        print(f"ABORT: post-patch still has {post_pure_unpatched} unpatched sites — refusing to write.", file=sys.stderr)
        return 4

    CLI_JS.write_text(new_content, encoding="utf-8", errors="surrogatepass")
    post_sha = _sha256(new_content.encode("utf-8", errors="surrogatepass"))[:16]
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts}\tapply\tpre={pre_sha}\tpost={post_sha}\tsites={pure_unpatched}\tbackup={backup.name}\n")
    print(f"PATCHED: {pure_unpatched} site(s). pre={pre_sha} post={post_sha}")
    return 0


def cmd_revert() -> int:
    backups = _list_backups()
    if not backups:
        print("ERROR: no backups available in tools/cli-js-patches/.cli.js.*.bak", file=sys.stderr)
        return 2
    latest = backups[-1]
    shutil.copy2(latest, CLI_JS)
    manifest = PATCH_DIR / "applied.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts}\trevert\tfrom={latest.name}\n")
    print(f"REVERTED: cli.js <- {latest.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply patch (default)")
    g.add_argument("--check", action="store_true", help="report state, no changes")
    g.add_argument("--revert", action="store_true", help="restore latest backup")
    args = ap.parse_args()
    if args.check:
        return cmd_check()
    if args.revert:
        return cmd_revert()
    return cmd_apply()


if __name__ == "__main__":
    sys.exit(main())
