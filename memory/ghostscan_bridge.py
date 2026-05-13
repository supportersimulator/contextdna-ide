"""GhostScan Bridge — connects multi-fleet probe engine to ContextDNA memory.

Inspired by the GhostScan architectural pattern (many narrow probes, central
registry, uniform output), this bridge:

1. Runs multi-fleet probes against the current repo
2. Stores findings in SQLite (evidence candidates)
3. Caches hot results in Redis for fast webhook injection
4. Promotes high-confidence findings to the evidence pipeline
5. Provides query interface for webhook S3/S4 enrichment

Architecture:
    [Git state / file changes]
        -> [multi-fleet ProbeEngine]
        -> [GhostScanBridge normalizer]
        -> [SQLite evidence_candidates table]
        -> [Redis ghostscan:* cache]
        -> [Webhook S3 AWARENESS injection]

Storage:
    SQLite: ghostscan_findings table (persistent, queryable)
    Redis: ghostscan:latest (cached latest scan results, 5min TTL)
           ghostscan:summary (human-readable summary, 5min TTL)
"""

import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("contextdna.ghostscan_bridge")

# Default repo root
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# SQLite DB path (same directory as other ContextDNA stores)
_DB_DIR = Path.home() / ".context-dna"
_LEGACY_DB_PATH = _DB_DIR / "ghostscan.db"


def _get_ghost_db() -> Path:
    from memory.db_utils import get_unified_db_path
    return get_unified_db_path(_LEGACY_DB_PATH)


_DB_PATH = _get_ghost_db()

# Redis keys
_REDIS_LATEST_KEY = "ghostscan:latest"
_REDIS_SUMMARY_KEY = "ghostscan:summary"
_REDIS_TTL = 300  # 5 minutes

def _t_ghost(name: str) -> str:
    from memory.db_utils import unified_table
    return unified_table("ghostscan.db", name)



@dataclass
class ScanFinding:
    """A normalized finding from a probe scan."""
    probe_id: str
    title: str
    severity: str  # low/medium/high
    confidence: float
    blast_radius: str  # low/medium/high
    evidence_refs: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    category: str = ""
    scan_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "title": self.title,
            "severity": self.severity,
            "confidence": self.confidence,
            "blast_radius": self.blast_radius,
            "evidence_refs": self.evidence_refs,
            "suggested_actions": self.suggested_actions,
            "file_paths": self.file_paths,
            "category": self.category,
            "scan_ts": self.scan_ts,
        }


@dataclass
class ScanSummary:
    """Summary of a full probe scan run."""
    scan_id: str
    timestamp: float
    duration_ms: float
    probes_run: int
    probes_ok: int
    probes_warn: int
    probes_error: int
    probes_skipped: int
    total_findings: int
    high_findings: int
    medium_findings: int
    findings: list[ScanFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "probes_run": self.probes_run,
            "probes_ok": self.probes_ok,
            "probes_warn": self.probes_warn,
            "probes_error": self.probes_error,
            "probes_skipped": self.probes_skipped,
            "total_findings": self.total_findings,
            "high_findings": self.high_findings,
            "medium_findings": self.medium_findings,
            "findings": [f.to_dict() for f in self.findings],
        }

    def human_summary(self) -> str:
        """One-line summary for webhook injection."""
        parts = [f"Scan: {self.probes_run} probes in {self.duration_ms:.0f}ms"]
        if self.high_findings:
            parts.append(f"{self.high_findings} HIGH")
        if self.medium_findings:
            parts.append(f"{self.medium_findings} MED")
        if self.probes_warn:
            parts.append(f"{self.probes_warn} warnings")
        return " | ".join(parts)


def _get_redis():
    """Get Redis connection or None."""
    try:
        import redis
        rc = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_timeout=2)
        rc.ping()
        return rc
    except Exception:
        return None


def _init_db() -> sqlite3.Connection:
    """Initialize the SQLite database for scan findings."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_t_ghost('ghostscan_findings')} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            probe_id TEXT NOT NULL,
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            confidence REAL NOT NULL,
            blast_radius TEXT DEFAULT 'low',
            category TEXT DEFAULT '',
            evidence_refs TEXT DEFAULT '[]',
            suggested_actions TEXT DEFAULT '[]',
            file_paths TEXT DEFAULT '[]',
            created_at REAL NOT NULL,
            promoted INTEGER DEFAULT 0
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_t_ghost('ghostscan_runs')} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL UNIQUE,
            timestamp REAL NOT NULL,
            duration_ms REAL NOT NULL,
            probes_run INTEGER NOT NULL,
            probes_ok INTEGER DEFAULT 0,
            probes_warn INTEGER DEFAULT 0,
            probes_error INTEGER DEFAULT 0,
            probes_skipped INTEGER DEFAULT 0,
            total_findings INTEGER DEFAULT 0,
            high_findings INTEGER DEFAULT 0,
            medium_findings INTEGER DEFAULT 0,
            summary_json TEXT DEFAULT '{{}}'
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_findings_severity
        ON {_t_ghost('ghostscan_findings')}(severity, created_at DESC)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_findings_probe
        ON {_t_ghost('ghostscan_findings')}(probe_id, created_at DESC)
    """)
    conn.commit()
    return conn


def _build_probe_context(repo_path: str, task: str = "") -> Any:
    """Build a ProbeContext from live git state."""
    try:
        # Add multi-fleet to path if needed
        mf_path = os.path.join(repo_path, "multi-fleet")
        if mf_path not in sys.path:
            sys.path.insert(0, mf_path)

        from multifleet.probe_context_factory import build_probe_context
        return build_probe_context(repo_path=repo_path, task=task)
    except ImportError:
        logger.warning("probe_context_factory not available, using minimal context")
        return None


def _get_probe_engine():
    """Get the multi-fleet ProbeEngine with all probes registered."""
    try:
        mf_path = os.path.join(_REPO_ROOT, "multi-fleet")
        if mf_path not in sys.path:
            sys.path.insert(0, mf_path)

        from multifleet.probes import create_engine
        return create_engine(wire_evidence_bridge=False)
    except ImportError as e:
        logger.warning("ProbeEngine not available: %s", e)
        return None


def run_scan(
    repo_path: str = _REPO_ROOT,
    task: str = "",
    cost_filter: str = "all",
    probe_ids: Optional[list[str]] = None,
) -> Optional[ScanSummary]:
    """Run a probe scan and store results.

    Args:
        repo_path: Path to the repo to scan
        task: Description of current task (for context-aware probes)
        cost_filter: "cheap", "medium", or "all"
        probe_ids: If set, run only these specific probes

    Returns:
        ScanSummary with all findings, or None if engine unavailable
    """
    engine = _get_probe_engine()
    if engine is None:
        return None

    ctx = _build_probe_context(repo_path, task)
    if ctx is None:
        return None

    t0 = time.monotonic()
    scan_id = f"scan-{int(time.time())}-{os.getpid()}"

    # Run probes based on filter
    # ProbeEngine.scan() uses trigger names: "manual", "all", "incremental",
    # "session_start", "session_end", "commit", etc.
    try:
        context = ctx.to_dict() if hasattr(ctx, "to_dict") else {}
        if probe_ids:
            results = [engine.scan_one(pid, context) for pid in probe_ids]
        elif cost_filter == "cheap":
            # Cheap = session_start trigger (fast probes only)
            results = engine.scan(trigger="session_start", context=context)
        elif cost_filter == "medium":
            # Medium = commit trigger
            results = engine.scan(trigger="commit", context=context)
        else:
            # All probes
            results = engine.scan(trigger="all", context=context)
    except Exception as e:
        logger.error("Probe scan failed: %s", e)
        return None

    duration_ms = (time.monotonic() - t0) * 1000

    # Normalize results into findings
    findings: list[ScanFinding] = []
    ok = warn = error = skipped = 0

    for r in results:
        status = getattr(r, "status", "unknown")
        if status == "ok":
            ok += 1
        elif status in ("warn", "warning"):
            warn += 1
        elif status == "error":
            error += 1
        elif status == "skipped":
            skipped += 1

        # Extract findings from probe results
        probe_findings = getattr(r, "findings", [])
        probe_id = getattr(r, "probe_id", "unknown")
        category = getattr(r, "category", "")

        for f in probe_findings:
            title = getattr(f, "title", str(f))
            severity = getattr(f, "severity", "low")
            # Normalize severity names
            if severity in ("info",):
                severity = "low"
            elif severity in ("critical",):
                severity = "high"

            confidence = getattr(f, "confidence", getattr(r, "confidence", 0.5))
            blast_radius = getattr(f, "blast_radius", "low")
            evidence = getattr(f, "evidence", getattr(f, "evidence_refs", []))
            actions = getattr(f, "suggested_actions", getattr(f, "suggested_injections", []))
            file_paths = getattr(f, "file_paths", getattr(f, "file_path", []))
            if isinstance(file_paths, str):
                file_paths = [file_paths] if file_paths else []

            findings.append(ScanFinding(
                probe_id=probe_id,
                title=title,
                severity=severity,
                confidence=confidence,
                blast_radius=blast_radius,
                evidence_refs=evidence if isinstance(evidence, list) else [str(evidence)],
                suggested_actions=actions if isinstance(actions, list) else [str(actions)],
                file_paths=file_paths,
                category=category,
                scan_ts=time.time(),
            ))

    high = sum(1 for f in findings if f.severity == "high")
    medium = sum(1 for f in findings if f.severity == "medium")

    summary = ScanSummary(
        scan_id=scan_id,
        timestamp=time.time(),
        duration_ms=duration_ms,
        probes_run=len(results),
        probes_ok=ok,
        probes_warn=warn,
        probes_error=error,
        probes_skipped=skipped,
        total_findings=len(findings),
        high_findings=high,
        medium_findings=medium,
        findings=findings,
    )

    # Store results
    _store_results(summary)
    _cache_results(summary)

    logger.info(
        "GhostScan complete: %d probes, %d findings (%d high, %d med) in %.0fms",
        len(results), len(findings), high, medium, duration_ms,
    )

    return summary


def _store_results(summary: ScanSummary) -> None:
    """Store scan results in SQLite."""
    try:
        conn = _init_db()
        try:
            # Store run metadata
            conn.execute(f"""
                INSERT OR REPLACE INTO {_t_ghost('ghostscan_runs')}
                (scan_id, timestamp, duration_ms, probes_run, probes_ok,
                 probes_warn, probes_error, probes_skipped, total_findings,
                 high_findings, medium_findings, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary.scan_id, summary.timestamp, summary.duration_ms,
                summary.probes_run, summary.probes_ok, summary.probes_warn,
                summary.probes_error, summary.probes_skipped,
                summary.total_findings, summary.high_findings,
                summary.medium_findings, json.dumps(summary.to_dict()),
            ))

            # Store individual findings (medium/high only to avoid noise)
            for f in summary.findings:
                if f.severity in ("medium", "high"):
                    conn.execute(f"""
                        INSERT INTO {_t_ghost('ghostscan_findings')}
                        (scan_id, probe_id, title, severity, confidence,
                         blast_radius, category, evidence_refs,
                         suggested_actions, file_paths, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        summary.scan_id, f.probe_id, f.title, f.severity,
                        f.confidence, f.blast_radius, f.category,
                        json.dumps(f.evidence_refs),
                        json.dumps(f.suggested_actions),
                        json.dumps(f.file_paths),
                        f.scan_ts,
                    ))

            conn.commit()

            # Prune old data (keep last 100 runs, 1000 findings)
            conn.execute(f"""
                DELETE FROM {_t_ghost('ghostscan_runs')} WHERE id NOT IN (
                    SELECT id FROM {_t_ghost('ghostscan_runs')} ORDER BY timestamp DESC LIMIT 100
                )
            """)
            conn.execute(f"""
                DELETE FROM {_t_ghost('ghostscan_findings')} WHERE id NOT IN (
                    SELECT id FROM {_t_ghost('ghostscan_findings')} ORDER BY created_at DESC LIMIT 1000
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to store scan results: %s", e)


def _cache_results(summary: ScanSummary) -> None:
    """Cache scan results in Redis for fast webhook access."""
    rc = _get_redis()
    if rc is None:
        return

    try:
        # Cache full results
        rc.setex(_REDIS_LATEST_KEY, _REDIS_TTL, json.dumps(summary.to_dict()))

        # Cache human-readable summary
        rc.setex(_REDIS_SUMMARY_KEY, _REDIS_TTL, summary.human_summary())

        # Cache per-probe results for targeted queries
        for f in summary.findings:
            if f.severity in ("medium", "high"):
                key = f"ghostscan:probe:{f.probe_id}"
                rc.setex(key, _REDIS_TTL, json.dumps(f.to_dict()))
    except Exception as e:
        logger.error("Failed to cache scan results: %s", e)


def get_latest_scan() -> Optional[dict]:
    """Get the latest scan results (Redis first, then SQLite fallback)."""
    # Try Redis cache first
    rc = _get_redis()
    if rc:
        try:
            data = rc.get(_REDIS_LATEST_KEY)
            if data:
                return json.loads(data)
        except Exception:
            pass

    # Fallback: SQLite
    try:
        conn = _init_db()
        try:
            row = conn.execute(f"""
                SELECT summary_json FROM {_t_ghost('ghostscan_runs')}
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            if row:
                return json.loads(row[0])
        finally:
            conn.close()
    except Exception:
        pass

    return None


def get_scan_summary_text() -> str:
    """Get a human-readable summary string for webhook injection."""
    # Try Redis cache
    rc = _get_redis()
    if rc:
        try:
            text = rc.get(_REDIS_SUMMARY_KEY)
            if text:
                return text
        except Exception:
            pass

    # Fallback: reconstruct from SQLite
    scan = get_latest_scan()
    if scan:
        parts = [f"Scan: {scan.get('probes_run', 0)} probes in {scan.get('duration_ms', 0):.0f}ms"]
        if scan.get("high_findings"):
            parts.append(f"{scan['high_findings']} HIGH")
        if scan.get("medium_findings"):
            parts.append(f"{scan['medium_findings']} MED")
        return " | ".join(parts)

    return ""


def get_findings_for_injection(
    max_findings: int = 5,
    min_severity: str = "medium",
) -> list[str]:
    """Get probe findings formatted for webhook S3 injection.

    Returns a list of human-readable strings suitable for the AWARENESS section.
    """
    scan = get_latest_scan()
    if not scan:
        return []

    sev_order = {"low": 0, "medium": 1, "high": 2}
    min_sev = sev_order.get(min_severity, 1)

    lines = []
    for f in scan.get("findings", []):
        fsev = sev_order.get(f.get("severity", "low"), 0)
        if fsev >= min_sev:
            probe = f.get("probe_id", "?")
            title = f.get("title", "")
            sev = f.get("severity", "?").upper()
            confidence = f.get("confidence", 0)

            line = f"[{probe}] [{sev}] {title}"
            if confidence >= 0.8:
                line += " (high confidence)"

            # Add suggested action if available
            actions = f.get("suggested_actions", [])
            if actions:
                line += f" -> {actions[0]}"

            lines.append(line)

    # Sort by severity (high first)
    lines.sort(key=lambda l: (0 if "[HIGH]" in l else 1))
    return lines[:max_findings]


def get_probe_history(probe_id: str, limit: int = 10) -> list[dict]:
    """Get historical findings for a specific probe."""
    try:
        conn = _init_db()
        try:
            rows = conn.execute(f"""
                SELECT probe_id, title, severity, confidence, blast_radius,
                       evidence_refs, suggested_actions, file_paths, created_at
                FROM {_t_ghost('ghostscan_findings')}
                WHERE probe_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (probe_id, limit)).fetchall()
            return [
                {
                    "probe_id": r[0], "title": r[1], "severity": r[2],
                    "confidence": r[3], "blast_radius": r[4],
                    "evidence_refs": json.loads(r[5]),
                    "suggested_actions": json.loads(r[6]),
                    "file_paths": json.loads(r[7]),
                    "created_at": r[8],
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to get probe history: %s", e)
        return []


def get_recurring_findings(min_occurrences: int = 3, days: int = 7) -> list[dict]:
    """Find findings that keep recurring (evidence for promotion).

    These are strong candidates for wisdom promotion because they've
    been independently detected multiple times.
    """
    cutoff = time.time() - (days * 86400)
    try:
        conn = _init_db()
        try:
            rows = conn.execute(f"""
                SELECT probe_id, title, severity, COUNT(*) as occurrences,
                       AVG(confidence) as avg_confidence,
                       MAX(created_at) as last_seen
                FROM {_t_ghost('ghostscan_findings')}
                WHERE created_at > ?
                GROUP BY probe_id, title
                HAVING COUNT(*) >= ?
                ORDER BY occurrences DESC, avg_confidence DESC
                LIMIT 20
            """, (cutoff, min_occurrences)).fetchall()
            return [
                {
                    "probe_id": r[0], "title": r[1], "severity": r[2],
                    "occurrences": r[3], "avg_confidence": r[4],
                    "last_seen": r[5],
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to get recurring findings: %s", e)
        return []


def run_background_scan(task: str = "") -> Optional[ScanSummary]:
    """Run a background scan suitable for scheduler jobs.

    Uses 'cheap' cost filter to minimize impact. Only runs medium+ probes
    on commit or session boundaries.
    """
    return run_scan(
        repo_path=_REPO_ROOT,
        task=task,
        cost_filter="cheap",
    )


# CLI interface
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="GhostScan Bridge CLI")
    parser.add_argument("command", choices=["scan", "latest", "findings", "recurring", "history"])
    parser.add_argument("--cost", default="all", choices=["cheap", "medium", "all"])
    parser.add_argument("--task", default="")
    parser.add_argument("--probe", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "scan":
        result = run_scan(cost_filter=args.cost, task=args.task)
        if result:
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                print(result.human_summary())
                for f in result.findings:
                    sev = f.severity.upper()
                    print(f"  [{sev}] [{f.probe_id}] {f.title}")
        else:
            print("Scan failed or engine unavailable")

    elif args.command == "latest":
        data = get_latest_scan()
        if data:
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(f"Last scan: {data.get('probes_run', 0)} probes, "
                      f"{data.get('total_findings', 0)} findings")
        else:
            print("No scan data available")

    elif args.command == "findings":
        lines = get_findings_for_injection()
        if lines:
            for line in lines:
                print(f"  {line}")
        else:
            print("No findings available")

    elif args.command == "recurring":
        recurring = get_recurring_findings()
        if recurring:
            for r in recurring:
                print(f"  [{r['severity'].upper()}] {r['title']} "
                      f"({r['occurrences']}x, conf={r['avg_confidence']:.2f})")
        else:
            print("No recurring findings")

    elif args.command == "history":
        if not args.probe:
            print("--probe required for history command")
        else:
            history = get_probe_history(args.probe)
            for h in history:
                print(f"  [{h['severity']}] {h['title']} @ {h['created_at']}")
