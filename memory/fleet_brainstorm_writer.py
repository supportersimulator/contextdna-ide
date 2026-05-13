"""Canonical writer for `.fleet/brainstorm/` artefacts.

JJ4 ship 1 (HH5 follow-up #1): `.fleet/brainstorm/` was previously populated
ad-hoc by various agents/scripts in different formats. This module is the
*canonical* surface so artefacts get a stable filename pattern and a JSON
sidecar, then ride the P7 git push channel to the rest of the fleet.

Pattern: `.fleet/brainstorm/YYYY-MM-DD-3s-<topic-slug>.md` (+ `.json`).

Design:
    - stdlib only (no new pip deps)
    - ZERO SILENT FAILURES — every failure path increments a counter and
      records the reason. Caller decides whether to surface.
    - Idempotent: repeated calls with the same topic on the same UTC day
      add a `-NN` suffix instead of clobbering the existing artefact.

API:
    write_brainstorm(topic, body_md, *, transcript_path=None,
                     fleet_dir=None, stamp=None) -> WriteResult

CLI:
    python3 -m memory.fleet_brainstorm_writer \
        --topic "<topic>" \
        --body /tmp/3s-brainstorm-<stamp>.md \
        [--transcript /tmp/3s-brainstorm-<stamp>.transcript.txt] \
        [--fleet-dir .fleet/brainstorm] \
        [--stamp YYYY-MM-DDTHH:MM:SSZ]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

_log = logging.getLogger("memory.fleet_brainstorm_writer")
if not _log.handlers:
    _log.addHandler(logging.NullHandler())

_counter_lock = threading.Lock()
_FAILURE_COUNTERS: Dict[str, int] = {
    "topic_blank": 0,
    "body_missing": 0,
    "fleet_dir_create_error": 0,
    "write_error": 0,
    "transcript_read_error": 0,
}


def _bump(counter: str) -> None:
    with _counter_lock:
        _FAILURE_COUNTERS[counter] = _FAILURE_COUNTERS.get(counter, 0) + 1


def get_failure_counters() -> Dict[str, int]:
    with _counter_lock:
        return dict(_FAILURE_COUNTERS)


@dataclass
class WriteResult:
    ok: bool
    md_path: Optional[Path] = None
    json_path: Optional[Path] = None
    reasons: list = field(default_factory=list)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(topic: str, max_len: int = 60) -> str:
    """Lowercase, ascii-only, hyphen-separated, capped at max_len.

    Matches the existing `.fleet/brainstorm/` filename style (e.g.
    `2026-05-07-AA5-aaron-first-action.md`).
    """
    s = topic.strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"


def _resolve_fleet_dir(fleet_dir: Optional[Path]) -> Path:
    if fleet_dir is not None:
        return Path(fleet_dir)
    # memory/fleet_brainstorm_writer.py -> repo root is parent of `memory/`
    here = Path(__file__).resolve()
    repo_root = here.parent.parent
    return repo_root / ".fleet" / "brainstorm"


def _next_available(base: Path) -> Path:
    """Return `base` if it doesn't exist, else `base.stem-NN.ext` (NN starts 2)."""
    if not base.exists():
        return base
    suffix = base.suffix
    stem = base.stem
    parent = base.parent
    n = 2
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1
        if n > 999:  # safety
            return parent / f"{stem}-{n}{suffix}"


def write_brainstorm(
    topic: str,
    body_md: str,
    *,
    transcript_path: Optional[Path] = None,
    fleet_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
) -> WriteResult:
    """Write the canonical `.fleet/brainstorm/` artefact pair.

    Returns WriteResult — never raises. ZSF: failures bump counters and
    populate `reasons`.
    """
    result = WriteResult(ok=False)

    if not topic or not topic.strip():
        _bump("topic_blank")
        result.reasons.append("topic is blank")
        return result

    if body_md is None or body_md == "":
        _bump("body_missing")
        result.reasons.append("body_md is empty")
        return result

    target_dir = _resolve_fleet_dir(fleet_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _bump("fleet_dir_create_error")
        result.reasons.append(f"mkdir failed: {exc}")
        return result

    if stamp is None:
        stamp_dt = datetime.now(timezone.utc)
    else:
        # accept either ISO-8601 or YYYYMMDDThhmmssZ; on parse failure, fall back to now.
        stamp_dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ"):
            try:
                stamp_dt = datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if stamp_dt is None:
            stamp_dt = datetime.now(timezone.utc)

    date_str = stamp_dt.strftime("%Y-%m-%d")
    slug = _slugify(topic)
    md_base = target_dir / f"{date_str}-3s-{slug}.md"
    md_path = _next_available(md_base)
    json_path = md_path.with_suffix(".json")

    transcript_excerpt: Optional[str] = None
    if transcript_path is not None:
        try:
            text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
            transcript_excerpt = text[-4000:] if len(text) > 4000 else text
        except OSError as exc:
            _bump("transcript_read_error")
            result.reasons.append(f"transcript read failed: {exc}")
            transcript_excerpt = None

    sidecar = {
        "topic": topic,
        "slug": slug,
        "stamp_utc": stamp_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": date_str,
        "md_path": str(md_path),
        "writer": "memory.fleet_brainstorm_writer",
        "writer_version": 1,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "transcript_tail": transcript_excerpt,
    }

    try:
        md_path.write_text(body_md, encoding="utf-8")
        json_path.write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        _bump("write_error")
        result.reasons.append(f"write failed: {exc}")
        return result

    result.ok = True
    result.md_path = md_path
    result.json_path = json_path
    return result


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Write canonical .fleet/brainstorm/ artefact pair (md + json sidecar)."
    )
    parser.add_argument("--topic", required=True, help="Brainstorm topic (used for slug).")
    parser.add_argument(
        "--body",
        required=True,
        help="Path to the rendered brainstorm markdown body.",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="Optional transcript path; tail (last 4kb) inlined into JSON sidecar.",
    )
    parser.add_argument(
        "--fleet-dir",
        default=None,
        help="Override .fleet/brainstorm/ destination (default: repo .fleet/brainstorm).",
    )
    parser.add_argument(
        "--stamp",
        default=None,
        help="Optional UTC stamp (YYYYMMDDThhmmssZ or ISO-8601). Defaults to now.",
    )
    args = parser.parse_args(argv)

    body_path = Path(args.body)
    if not body_path.exists():
        print(f"ERROR: --body file not found: {body_path}", file=sys.stderr)
        return 2
    try:
        body_md = body_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read --body {body_path}: {exc}", file=sys.stderr)
        return 2

    transcript_path = Path(args.transcript) if args.transcript else None
    fleet_dir = Path(args.fleet_dir) if args.fleet_dir else None

    result = write_brainstorm(
        args.topic,
        body_md,
        transcript_path=transcript_path,
        fleet_dir=fleet_dir,
        stamp=args.stamp,
    )

    if not result.ok:
        print(
            "fleet_brainstorm_writer FAIL: " + "; ".join(result.reasons),
            file=sys.stderr,
        )
        return 1
    print(f"md:   {result.md_path}")
    print(f"json: {result.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
