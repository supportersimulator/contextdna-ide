# Submodule Realization Status

Audited 2026-05-13 by Agent B5 (autonomous /loop, Round 5).

This document is **non-destructive analysis**. No submodules were initialized,
fetched, or written to .git/config. Aaron decides realization next step.

## Why this exists

The repo declares 4 submodules in `.gitmodules` but 0 of them are realized on
disk. `git clone --recurse-submodules` from a clean room therefore produces a
mothership tree with empty `engines/3-surgeons`, `engines/multi-fleet`,
`engines/context-dna`, and `apps/ide-shell` directories. The README quickstart
claims "clone → docker compose → working"; that contract is currently weakened
by the missing engines.

## URL reachability probe

Confirmed via `curl -I` from this host on 2026-05-13:

| Declared submodule URL | HTTP | Notes |
|------------------------|------|-------|
| `https://github.com/supportersimulator/3-surgeons` | 200 | Reachable. |
| `https://github.com/supportersimulator/multi-fleet` | 200 | Reachable. |
| `https://github.com/supportersimulator/context-dna` | **404** | Repo does not exist. Already flagged inline in `.gitmodules` comment. |
| `https://github.com/supportersimulator/v0-Context-DNA-1` | 200 | Reachable (apps/ide-shell). |

## Per-submodule status

### `engines/3-surgeons`
- **URL:** `https://github.com/supportersimulator/3-surgeons.git`
- **Branch:** `main`
- **Status:** Reachable. Realization is a green-light decision.
- **Owner:** supportersimulator org.
- **Current shape in mothership:** Heavy logic still lives in
  `er-simulator-superrepo/3-surgeons/` (the superrepo submodule). The mothership
  expects the same package layout when realized here.
- **Realization risk:** Low. Existing `wire-submodules.sh` already handles this
  path safely.

### `engines/multi-fleet`
- **URL:** `https://github.com/supportersimulator/multi-fleet.git`
- **Branch:** `main`
- **Status:** Reachable.
- **Owner:** supportersimulator org (Mac3 owns OSS polish per fleet memory).
- **Realization risk:** Low. Public OSS surface is mature (banner + manifesto
  README per project memory `project_mac3_multifleet_oss_polish.md`).
- **Note:** Per fleet memory, do NOT auto-sync this back into the superrepo —
  the OSS repo is the canonical source.

### `engines/context-dna`
- **URL:** `https://github.com/supportersimulator/context-dna.git`
- **Branch:** `main`
- **Status:** **404 — repo does not exist.** This is the most important blocker.
- **Inline note in `.gitmodules`:** Already acknowledged; `wire-submodules.sh`
  is documented to log and skip rather than fail.
- **Current shape:** The `context-dna` engine ships today only as a local
  pip-packaged module inside `er-simulator-superrepo` (see superrepo's
  `context-dna/engine/` tree). It has never been promoted to a standalone
  public repo.
- **Aaron decision needed:**
  1. **Promote** — extract `context-dna/engine/` to its own public GitHub repo
     under supportersimulator. Most idiomatic.
  2. **Vendor** — copy the engine tree into `engines/context-dna/` and drop the
     submodule declaration. Lowest external dependency.
  3. **Defer** — keep the 404 + the existing inline TODO, ship the rest. The
     mothership remains usable without this engine realized in-tree, since
     `surgery_bridge.py` already does dynamic import resolution.
- **Realization risk if forced now:** High. `git submodule update --init` will
  fail hard with auth + 404 errors; cleaning up takes manual `.git/config` edits.

### `apps/ide-shell`
- **URL:** `https://github.com/supportersimulator/v0-Context-DNA-1.git`
- **Branch:** `main`
- **Status:** Reachable.
- **Owner:** supportersimulator org.
- **Realization risk:** Low. v0-generated Next.js IDE shell; standalone repo.
- **Note:** Optional for the lite stack — `docker-compose.lite.yml` does not
  depend on the IDE shell. Heavy stack may.

## Recommendation summary

Aaron's safest path:

1. Promote `context-dna` to its own public repo OR vendor it. This unblocks the
   `engines/context-dna` 404.
2. Then run `bash scripts/wire-submodules.sh` (which already handles partial
   realization gracefully per its design).
3. Update README quickstart to clarify which submodules are required for the
   lite stack vs. heavy stack; today every submodule looks equally mandatory.

Until step 1, the README's "clone with `--recurse-submodules`" line will
print a non-fatal warning for `engines/context-dna` on every fresh clone.

## What this audit did NOT do

- Did not run `git submodule init`, `git submodule update`, or `git submodule add`.
- Did not write to `.git/config` or `.gitmodules`.
- Did not push or commit. The working tree change set for round 5 is:
  - `src/context_dna_ide/__init__.py` (new)
  - `src/context_dna_ide/cli.py` (new)
  - `scripts/bootstrap-from-scratch.sh` (new wrapper)
  - `tests/test_cli.py` (new)
  - `dist/context_dna_ide-0.1.0-py3-none-any.whl` (build artifact — gitignored or rm'able)
  - `SUBMODULES_STATUS.md` (this file)
