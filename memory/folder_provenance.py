"""Folder provenance — retroactive 4-folder pipeline audit (R2).

Per CLAUDE.md "4-folder system":

    docs/inbox/   (L1 LOW)
    docs/vision/  (L2 MED)
    docs/reflect/ (L3 HIGH)
    docs/dao/     (L4 HIGHEST)

The pipeline rule is *documents don't move between folders* — each level
generates independently. This module audits that invariant retroactively:

  * scans the current filesystem snapshot to record which level each
    doc currently lives in
  * mines ``git log --follow --diff-filter=R --name-status`` per file to
    reconstruct any historical rename history (transitions between
    levels), so we surface violations of the no-move rule
  * writes a JSON index to ``memory/folder_provenance.json`` that the
    daemon can summarize at ``/health.folder_provenance``

Output schema::

    {
      "<repo-relative path>": {
        "current_level": "L1"|"L2"|"L3"|"L4",
        "ever_in":       ["L1", ...],          # ordered, dedup
        "transitions":   [                     # chronological
          {"from": "L1", "to": "L2", "ts": "ISO", "commit": "<sha>"}
        ]
      }
    }

ZERO SILENT FAILURES:
  * counters dict ``COUNTERS`` is module-level + daemon-scrapable
  * missing folder, git-log failure, parse error each bumps a counter
  * snapshot fields always come back (never crashes)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import pathlib
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DOCS_ROOT = REPO_ROOT / "docs"
DEFAULT_INDEX_PATH = REPO_ROOT / "memory" / "folder_provenance.json"

# Folder -> level mapping. Order matters for stable iteration.
FOLDER_LEVELS: Dict[str, str] = {
    "inbox": "L1",
    "vision": "L2",
    "reflect": "L3",
    "dao": "L4",
}

# Module-level counter dict — every failure bumps one of these.
COUNTERS: Dict[str, int] = {
    "total_scanned": 0,
    "folder_missing": 0,
    "git_log_failures": 0,
    "git_parse_failures": 0,
    "bogus_root": 0,
    "index_write_failures": 0,
    "rebuild_calls": 0,
}


def _bump(name: str, by: int = 1) -> None:
    """Bump a ZSF counter. Unknown names are auto-registered."""
    COUNTERS[name] = COUNTERS.get(name, 0) + by


def get_counters() -> Dict[str, int]:
    """Return a defensive copy of the counter snapshot (daemon-scrapable)."""
    return dict(COUNTERS)


def reset_counters() -> None:
    """Test helper — zero every counter."""
    for k in list(COUNTERS.keys()):
        COUNTERS[k] = 0


def _resolve_docs_root(docs_root: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
    """Return docs_root or DEFAULT_DOCS_ROOT, or None if bogus."""
    root = pathlib.Path(docs_root) if docs_root is not None else DEFAULT_DOCS_ROOT
    if not root.exists() or not root.is_dir():
        _bump("bogus_root")
        logger.debug("folder_provenance: bogus docs_root %r", str(root))
        return None
    return root


def _walk_level(folder_path: pathlib.Path, level: str) -> List[pathlib.Path]:
    """Return list of .md files inside ``folder_path`` (recursive)."""
    if not folder_path.exists() or not folder_path.is_dir():
        _bump("folder_missing")
        logger.debug("folder_provenance: missing folder %r", str(folder_path))
        return []
    return sorted(p for p in folder_path.rglob("*.md") if p.is_file())


def _git_rename_history(
    repo_root: pathlib.Path, rel_path: str
) -> List[Dict[str, str]]:
    """Reconstruct transitions for ``rel_path`` from git's --follow rename log.

    Returns a list of ``{"from": L?, "to": L?, "ts": ISO, "commit": sha}``
    entries ordered oldest -> newest. Empty list on any failure (counter
    bumped). The caller MUST always be able to produce a current_level
    even without rename history.
    """
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "log",
        "--follow",
        "--diff-filter=R",
        "--name-status",
        "--format=%H%x09%aI",
        "--",
        rel_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _bump("git_log_failures")
        logger.debug("folder_provenance: git log failed for %s: %r", rel_path, exc)
        return []
    if completed.returncode != 0:
        _bump("git_log_failures")
        logger.debug(
            "folder_provenance: git log returncode=%d for %s stderr=%s",
            completed.returncode,
            rel_path,
            (completed.stderr or "").strip()[:200],
        )
        return []

    transitions: List[Dict[str, str]] = []
    current_commit = ""
    current_ts = ""
    for raw_line in completed.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        # Commit header lines look like ``<sha>\t<iso-ts>``.
        if "\t" in line and not line.startswith("R"):
            parts = line.split("\t", 1)
            if len(parts) == 2 and len(parts[0]) >= 7 and " " not in parts[0]:
                current_commit = parts[0].strip()
                current_ts = parts[1].strip()
                continue
        # Rename status lines look like ``R<score>\t<old>\t<new>``.
        if line.startswith("R"):
            cols = line.split("\t")
            if len(cols) < 3:
                _bump("git_parse_failures")
                continue
            old_path, new_path = cols[1], cols[2]
            from_level = _level_for_path(old_path)
            to_level = _level_for_path(new_path)
            if from_level is None or to_level is None:
                # Rename crossed the docs-tree boundary; skip silently.
                continue
            if from_level == to_level:
                # Renamed within the same level — not a pipeline transition.
                continue
            transitions.append(
                {
                    "from": from_level,
                    "to": to_level,
                    "ts": current_ts,
                    "commit": current_commit,
                }
            )
    # git log returns newest -> oldest; reverse so callers see chronological.
    transitions.reverse()
    return transitions


def _level_for_path(rel_path: str) -> Optional[str]:
    """Return the 4-folder level for ``rel_path`` (repo-relative)."""
    parts = pathlib.PurePosixPath(rel_path).parts
    # Expect ``docs/<folder>/...``.
    if len(parts) >= 2 and parts[0] == "docs":
        return FOLDER_LEVELS.get(parts[1])
    return None


def _ever_in(current: str, transitions: List[Dict[str, str]]) -> List[str]:
    """Return ordered, deduplicated list of every level this doc has lived in.

    Walks transitions oldest -> newest, prepending each ``from`` and ending at
    the current level.
    """
    seen: List[str] = []

    def _add(level: str) -> None:
        if level and level not in seen:
            seen.append(level)

    for t in transitions:
        _add(t.get("from", ""))
        _add(t.get("to", ""))
    _add(current)
    return seen


def scan_folders(
    docs_root: Optional[pathlib.Path] = None,
    repo_root: Optional[pathlib.Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the provenance map for every .md file in the 4 folders.

    Returns ``{repo_relative_path: {current_level, ever_in, transitions}}``.
    A bogus ``docs_root`` returns an empty dict (counter bumped).
    """
    root = _resolve_docs_root(docs_root)
    if root is None:
        return {}

    repo = pathlib.Path(repo_root) if repo_root else REPO_ROOT

    out: Dict[str, Dict[str, Any]] = {}
    for folder, level in FOLDER_LEVELS.items():
        folder_path = root / folder
        for path in _walk_level(folder_path, level):
            _bump("total_scanned")
            try:
                rel_path = str(path.relative_to(repo)).replace(os.sep, "/")
            except ValueError:
                # Doc lives outside the repo root (test harness etc.).
                rel_path = str(path)
            transitions = _git_rename_history(repo, rel_path)
            out[rel_path] = {
                "current_level": level,
                "ever_in": _ever_in(level, transitions),
                "transitions": transitions,
            }
    return out


def update_provenance_index(
    docs_root: Optional[pathlib.Path] = None,
    output_path: Optional[pathlib.Path] = None,
    repo_root: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    """Rebuild the index and persist it to JSON. Returns the full payload.

    Payload shape::

        {
          "last_scan_ts": "<ISO>",
          "total_indexed": int,
          "level_counts":  {"L1": int, ...},
          "counter_snapshot": {...},
          "docs": { <path>: {...} },
        }
    """
    _bump("rebuild_calls")
    docs = scan_folders(docs_root=docs_root, repo_root=repo_root)
    level_counts: Dict[str, int] = {lvl: 0 for lvl in FOLDER_LEVELS.values()}
    for entry in docs.values():
        lvl = entry.get("current_level")
        if isinstance(lvl, str) and lvl in level_counts:
            level_counts[lvl] += 1

    payload: Dict[str, Any] = {
        "last_scan_ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "total_indexed": len(docs),
        "level_counts": level_counts,
        "counter_snapshot": get_counters(),
        "docs": docs,
    }

    target = pathlib.Path(output_path) if output_path else DEFAULT_INDEX_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        _bump("index_write_failures")
        logger.warning(
            "folder_provenance: failed to write index %s: %r", str(target), exc
        )
    return payload


def load_index(index_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Read the persisted index. Empty dict if missing / malformed."""
    target = pathlib.Path(index_path) if index_path else DEFAULT_INDEX_PATH
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("folder_provenance: failed to load index: %r", exc)
        return {}


def summarize_for_health(
    index_path: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    """Compact daemon-friendly summary for ``/health.folder_provenance``.

    ZSF: any failure returns ``{"available": False, "error": ...}``.
    """
    try:
        payload = load_index(index_path=index_path)
        if not payload:
            return {
                "available": False,
                "reason": "index-not-built",
                "counter_snapshot": get_counters(),
            }
        return {
            "available": True,
            "total_indexed": int(payload.get("total_indexed", 0)),
            "last_scan_ts": payload.get("last_scan_ts"),
            "level_counts": payload.get("level_counts", {}),
            "counter_snapshot": payload.get(
                "counter_snapshot", get_counters()
            ),
        }
    except Exception as exc:  # noqa: BLE001 — defensive ZSF
        logger.debug("folder_provenance: summarize_for_health failed: %r", exc)
        return {"available": False, "error": repr(exc)}


def is_index_current(
    docs_root: Optional[pathlib.Path] = None,
    index_path: Optional[pathlib.Path] = None,
) -> bool:
    """Return True iff the index exists AND its last_scan_ts is >= newest
    .md mtime under the 4 folders. CLI ``--check`` uses this.
    """
    payload = load_index(index_path=index_path)
    if not payload:
        return False
    last_scan = payload.get("last_scan_ts")
    if not isinstance(last_scan, str):
        return False
    try:
        last_scan_dt = _dt.datetime.fromisoformat(last_scan)
    except ValueError:
        return False

    root = _resolve_docs_root(docs_root)
    if root is None:
        # No folders to compare against — index is "current" by default.
        return True

    newest_mtime = 0.0
    for folder in FOLDER_LEVELS:
        folder_path = root / folder
        if not folder_path.exists():
            continue
        for p in folder_path.rglob("*.md"):
            try:
                mt = p.stat().st_mtime
                if mt > newest_mtime:
                    newest_mtime = mt
            except OSError:
                continue
    if newest_mtime <= 0.0:
        return True
    last_scan_epoch = last_scan_dt.timestamp()
    return last_scan_epoch >= newest_mtime
