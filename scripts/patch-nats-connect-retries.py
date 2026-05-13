#!/usr/bin/env python3
"""patch-nats-connect-retries.py — Add ``--connect_retries N`` to the local
NATS-cluster launchd plist so a transient LAN drop at boot cannot stretch
the connect-retry backoff to ~1h between attempts.

Why: ZZ4 (`.fleet/audits/2026-05-12-ZZ4-mac1-mac2-cluster-mesh-diagnosis.md`)
root-caused WW1's 45h webhook silence to NATS connect-retry backoff
saturation. The default exponential backoff stretches to ~1h between
attempts after a single transient LAN drop in early uptime, leaving
perfectly-reachable peers ignored until the daemon is kickstarted.

nats-server's ``--connect_retries N`` CLI flag (Cluster Options) bounds the
retry loop for *implicit routes* and — with a high value such as 120 —
keeps reconnect cadence on the ~2s base interval instead of the
exponential ceiling. Self-heals WW1-class outages without operator
involvement.

This script patches ``ProgramArguments`` in
``~/Library/LaunchAgents/io.contextdna.nats-server.plist``:

  before:   <string>--routes</string><string>nats://...,nats://...</string>
  after:    ... <string>--connect_retries</string><string>120</string>

The flag is inserted immediately *after* the ``--cluster_name`` block so
it stays in the Cluster Options section. If the flag already exists with
the desired value, the script is a no-op (idempotent).

Modes:
    --dry-run   (default) print unified diff, no writes.
    --apply     write ``<plist>.bak`` then update plist; print kickstart hint.
    --revert    restore plist from latest ``<plist>.bak``.

Options:
    --retries N         target retries value (default: 120).
    --config-path PATH  plist path override (default:
                        ~/Library/LaunchAgents/io.contextdna.nats-server.plist).

ZSF: every read / parse / write wrapped; verbatim errors; non-zero exit
on any failure; nothing swallowed. ``plutil -lint`` validates the new
plist before atomic replace; failure aborts and leaves the original file
untouched.

CONSTRAINT: this script never restarts the daemon. ``--apply`` prints the
``launchctl kickstart`` command for Aaron to run; reversibility is one
``--revert`` invocation away.
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

DEFAULT_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "io.contextdna.nats-server.plist"
)
DEFAULT_RETRIES = 120
FLAG_NAME = "--connect_retries"

# Match an existing <string>--connect_retries</string>\n<string>N</string> pair.
# DOTALL so whitespace between tags can include newlines.
_EXISTING_RE = re.compile(
    r"(<string>" + re.escape(FLAG_NAME) + r"</string>\s*\n?\s*<string>)([^<]*)(</string>)",
    re.MULTILINE,
)

# Anchor for insertion: the <string>--cluster_name</string><string>NAME</string>
# pair. We insert the connect_retries pair immediately after this so the new
# argument stays inside the Cluster Options section of ProgramArguments.
# Group 2 captures the indent (tabs/spaces) on the value line so the new pair
# matches surrounding plist formatting exactly.
_CLUSTER_NAME_ANCHOR = re.compile(
    r"(<string>--cluster_name</string>[ \t]*\n([ \t]*)<string>[^<]*</string>)",
    re.MULTILINE,
)


# ── helpers ────────────────────────────────────────────────────────────────
def read_plist(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"plist not found: {path}")
    try:
        return path.read_text()
    except OSError as e:
        raise SystemExit(f"failed to read {path}: {e}")


def validate_retries(value: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"invalid --retries value: {value!r} (must be a positive integer)")
    if n <= 0:
        raise SystemExit(f"invalid --retries value: {n} (must be a positive integer)")
    return n


def plan_patch(plist_text: str, retries: int) -> tuple[str, str]:
    """Return ``(new_text, action)`` for the requested retries value.

    action is one of:
        "noop"     — flag already present with desired value.
        "update"   — flag present with a different value; substitute.
        "insert"   — flag absent; insert pair after --cluster_name anchor.

    Raises SystemExit if the plist lacks the cluster_name anchor (refuses to
    patch a non-clustered nats-server plist — same defensive posture as
    unify-cluster-urls.py).
    """
    m = _EXISTING_RE.search(plist_text)
    if m is not None:
        current = m.group(2).strip()
        if current == str(retries):
            return plist_text, "noop"
        new_text, n = _EXISTING_RE.subn(
            lambda mm: mm.group(1) + str(retries) + mm.group(3),
            plist_text,
            count=1,
        )
        if n != 1:
            raise SystemExit(
                f"expected exactly 1 {FLAG_NAME} substitution, made {n}; "
                "plist may have an unexpected shape."
            )
        return new_text, "update"

    anchor = _CLUSTER_NAME_ANCHOR.search(plist_text)
    if anchor is None:
        raise SystemExit(
            "plist does not contain a <string>--cluster_name</string> element. "
            "Refusing to patch a plist that doesn't look like a clustered "
            "nats-server invocation."
        )
    indent = anchor.group(2) or "\t\t\t"
    insertion = (
        f"\n{indent}<string>{FLAG_NAME}</string>"
        f"\n{indent}<string>{retries}</string>"
    )
    new_text = (
        plist_text[: anchor.end()]
        + insertion
        + plist_text[anchor.end():]
    )
    return new_text, "insert"


def unified_diff(old: str, new: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{label} (current)",
            tofile=f"{label} (patched)",
            n=2,
        )
    )


def plutil_lint(text: str) -> None:
    """Run ``plutil -lint`` on the proposed plist contents.

    Aborts via SystemExit if validation fails. ZSF: errors propagate verbatim;
    nothing is swallowed.
    """
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".plist", delete=False
        ) as fh:
            tmp_path = fh.name
            fh.write(text)
        try:
            proc = subprocess.run(
                ["plutil", "-lint", tmp_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise SystemExit(
                    "plutil -lint failed on proposed plist:\n"
                    f"  stdout: {proc.stdout.strip()}\n"
                    f"  stderr: {proc.stderr.strip()}"
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except FileNotFoundError as e:
        # plutil missing → fail loud, not silent. This is macOS-only territory.
        raise SystemExit(f"plutil not available for validation: {e}")
    except OSError as e:
        raise SystemExit(f"failed to write temp plist for plutil: {e}")


# ── modes ──────────────────────────────────────────────────────────────────
def cmd_dry_run(plist: Path, retries: int) -> int:
    current = read_plist(plist)
    new_text, action = plan_patch(current, retries)

    print(f"plist:          {plist}")
    print(f"target retries: {retries}")
    print(f"action:         {action}")

    if action == "noop":
        print(f"no changes needed — {FLAG_NAME} already set to {retries}.")
        return 0

    diff = unified_diff(current, new_text, plist.name)
    if diff:
        print("--- diff (dry-run, no writes) ---")
        print(diff)
    print("re-run with --apply to write changes.")
    return 0


def cmd_apply(plist: Path, retries: int) -> int:
    current = read_plist(plist)
    new_text, action = plan_patch(current, retries)

    if action == "noop":
        print(f"no changes needed for {plist.name} — {FLAG_NAME}={retries} already.")
        return 0

    # Validate before mutating anything.
    plutil_lint(new_text)

    bak = plist.with_suffix(plist.suffix + ".bak")
    try:
        shutil.copy2(plist, bak)
    except OSError as e:
        raise SystemExit(f"failed to write backup {bak}: {e}")

    # Atomic-ish replace: write to sibling tmp then rename.
    tmp = plist.with_suffix(plist.suffix + ".tmp")
    try:
        tmp.write_text(new_text)
        tmp.replace(plist)
    except OSError as e:
        # Best-effort cleanup; original plist untouched because rename failed.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SystemExit(f"failed to write {plist}: {e}")

    print(f"backup:    {bak}")
    print(f"updated:   {plist}")
    print(f"action:    {action} ({FLAG_NAME}={retries})")
    print()
    print("Aaron must run the following to take effect:")
    print("  launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server")
    print()
    print("Verify after kickstart:")
    print("  bash scripts/diagnose-cluster-mesh.sh")
    return 0


def cmd_revert(plist: Path) -> int:
    bak = plist.with_suffix(plist.suffix + ".bak")
    if not bak.exists():
        raise SystemExit(f"no backup found at {bak}")
    try:
        shutil.copy2(bak, plist)
    except OSError as e:
        raise SystemExit(f"failed to restore {plist} from {bak}: {e}")
    print(f"restored {plist} from {bak}")
    print("kickstart to apply: launchctl kickstart -k gui/$(id -u)/io.contextdna.nats-server")
    return 0


# ── cli ────────────────────────────────────────────────────────────────────
def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Patch NATS launchd plist to set --connect_retries N so a "
            "transient LAN drop at boot cannot stretch the connect-retry "
            "backoff to ~1h between attempts (self-heals WW1-class "
            "webhook silences)."
        )
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print diff only, no writes (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="write .bak then update plist (kickstart still required)",
    )
    mode.add_argument(
        "--revert",
        action="store_true",
        help="restore plist from latest .bak",
    )
    p.add_argument(
        "--retries",
        default=str(DEFAULT_RETRIES),
        help=f"target --connect_retries value (default: {DEFAULT_RETRIES})",
    )
    p.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_PLIST,
        help=f"plist path (default: {DEFAULT_PLIST})",
    )
    return p.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    plist = args.config_path
    if args.revert:
        return cmd_revert(plist)
    retries = validate_retries(args.retries)
    if args.apply:
        return cmd_apply(plist, retries)
    return cmd_dry_run(plist, retries)


if __name__ == "__main__":
    sys.exit(main())
