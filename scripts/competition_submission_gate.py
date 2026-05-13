#!/usr/bin/env python3
"""Competition Submission Governance gate — Python check engine.

Runs 8 named checks against a submission artifact + metadata pair before the
artifact is allowed to leave the fleet (publish to Kaggle, write to
``submissions/``, broadcast over NATS, etc).

Pattern: clones ``scripts/gains-gate.sh`` semantics — every check emits a
status line on stdout AND a JSON record into the audit log so the same data
fuels both Aaron's morning skim and the audit pipeline aggregator.

Checks (all log to ``.fleet/audits/<YYYY-MM-DD>-submission-gate.log``):

    1. artifact-exists           — file present, non-empty
    2. metadata-schema           — validates against
                                   multifleet.governed_packet (S1) — graceful
                                   fallback to a structural schema check if S1
                                   is not yet shipped.
    3. determinism               — re-running the artifact's stored
                                   ``regenerate_cmd`` (or simply re-hashing the
                                   bytes for static artifacts) produces a
                                   byte-identical artifact.
    4. constitutional-signoff    — a chief 3-Surgeon decision exists in
                                   ``.fleet/audits/<date>-decisions.md`` whose
                                   ``cluster_id`` references the artifact's
                                   ``submission_id``.
    5. evidence-ledger           — S2's ``EvidenceLedger.record`` writes an
                                   entry — graceful fallback to direct write
                                   if S2 not ready.
    6. leaderboard-guard         — S4's ``leaderboard_guard.check`` returns
                                   any verdict OTHER than divergence-warning
                                   — graceful fallback to PASS-with-warning
                                   if module not present.
    7. no-secrets                — regex scan over the submission for known
                                   credential shapes (reuses
                                   ``multifleet.secret_redact.PATTERNS``).
    8. reversibility-path        — artifact path lives under ``submissions/``
                                   (never in the core source tree, so a single
                                   ``rm`` reverts publication).

Usage:
    python3 competition_submission_gate.py \
        --artifact <path> --metadata <metadata.json> [--repo <root>]

Exit:
    0 — all 8 checks PASS
    1 — at least one CRITICAL check failed (gate blocks)
    2 — setup error (missing arguments, unparseable metadata, etc.)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Severity for each check — clones the gains-gate vocabulary.
CRITICAL = "critical"
WARNING = "warning"
SETUP_ERROR_RC = 2
GATE_FAIL_RC = 1
GATE_PASS_RC = 0


@dataclass
class CheckResult:
    name: str
    severity: str
    passed: bool
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _today() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _audit_log_path(repo: Path) -> Path:
    p = repo / ".fleet" / "audits" / f"{_today()}-submission-gate.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── Check 1: artifact-exists ───────────────────────────────────────────────


def check_artifact_exists(artifact: Path) -> CheckResult:
    name = "artifact-exists"
    if not artifact.is_file():
        return CheckResult(name, CRITICAL, False,
                           f"artifact missing: {artifact}")
    size = artifact.stat().st_size
    if size <= 0:
        return CheckResult(name, CRITICAL, False,
                           f"artifact empty: {artifact}", {"size_bytes": 0})
    return CheckResult(name, CRITICAL, True,
                       f"{size} bytes", {"size_bytes": size})


# ── Check 2: metadata-schema (S1 governed_packet, graceful fallback) ──────


def check_metadata_schema(metadata_obj: Dict[str, Any], repo: Path) -> CheckResult:
    name = "metadata-schema"
    # Try the S1 primitive first — when shipped, this is the canonical
    # validator. S1's API: GovernedPacket.from_dict(...) raises
    # GovernedPacketValidationError on bad input.
    sys.path.insert(0, str(repo / "multi-fleet"))
    GovernedPacket = None  # type: ignore[assignment]
    GPVErr = None  # type: ignore[assignment]
    try:
        from multifleet.governed_packet import (  # type: ignore
            GovernedPacket,
            GovernedPacketValidationError as GPVErr,  # type: ignore
        )
    except Exception:  # noqa: BLE001 — module-not-shipped is a known fallback
        GovernedPacket = None  # type: ignore
    finally:
        try:
            sys.path.remove(str(repo / "multi-fleet"))
        except ValueError:
            pass

    # Only use S1 if metadata declares its packet shape — bare submission
    # metadata isn't a full GovernedPacket. The structural fallback below
    # always runs as a baseline so we never lose the schema check.
    if GovernedPacket is not None and metadata_obj.get("packet_kind"):
        try:
            GovernedPacket.from_dict(metadata_obj)  # type: ignore[union-attr]
        except (GPVErr, Exception) as exc:  # type: ignore[misc]
            # S1 validator raised → metadata is structurally broken.
            return CheckResult(name, CRITICAL, False,
                               f"S1 GovernedPacket rejected: {exc}",
                               {"fallback": False})
        return CheckResult(name, CRITICAL, True,
                           "S1 GovernedPacket.from_dict OK",
                           {"fallback": False})

    # Fallback structural schema — required keys + types.
    required = {
        "submission_id": str,
        "competition": str,
        "produced_at": (int, float, str),
        "regenerate_cmd": (str, list, type(None)),
    }
    missing = [k for k in required if k not in metadata_obj]
    bad_type = [
        k for k, t in required.items()
        if k in metadata_obj and not isinstance(metadata_obj[k], t)
    ]
    if missing or bad_type:
        return CheckResult(
            name, CRITICAL, False,
            f"fallback: missing={missing} bad_type={bad_type}",
            {"fallback": True, "missing": missing, "bad_type": bad_type},
        )
    return CheckResult(name, CRITICAL, True,
                       "fallback structural schema OK",
                       {"fallback": True})


# ── Check 3: determinism ───────────────────────────────────────────────────


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_determinism(artifact: Path, metadata_obj: Dict[str, Any],
                      repo: Path) -> CheckResult:
    """Determinism = same inputs produce byte-identical artifact.

    Two modes:
      A. metadata declares ``regenerate_cmd`` — execute it in a tempdir, hash
         the produced artifact, compare to the original. Failure modes are
         observable (rc != 0, file missing, hash differs) — never silent.
      B. no regenerate_cmd — fall back to the weaker "static byte invariance"
         check: hash the artifact twice and confirm identical (catches
         filesystem-level non-determinism / corruption). This DOES NOT prove
         end-to-end determinism but does prove the artifact bytes are stable
         on disk for the duration of the gate run.
    """
    name = "determinism"
    h_orig = _hash(artifact)
    cmd = metadata_obj.get("regenerate_cmd")
    if not cmd:
        h_again = _hash(artifact)
        if h_orig != h_again:
            return CheckResult(name, CRITICAL, False,
                               "static re-hash drifted (filesystem instability?)",
                               {"sha256_a": h_orig, "sha256_b": h_again,
                                "mode": "static"})
        return CheckResult(
            name, WARNING, True,
            "static-only (no regenerate_cmd) — limited determinism proof",
            {"sha256": h_orig, "mode": "static"},
        )

    # Mode A: actually re-run.
    tmp_artifact = artifact.parent / f"{artifact.stem}.gate-replay{artifact.suffix}"
    try:
        env = os.environ.copy()
        env["SUBMISSION_GATE_REPLAY_OUT"] = str(tmp_artifact)
        if isinstance(cmd, str):
            proc = subprocess.run(cmd, shell=True, env=env,
                                  capture_output=True, text=True,
                                  timeout=120, cwd=str(repo))
        else:
            proc = subprocess.run(list(cmd), env=env,
                                  capture_output=True, text=True,
                                  timeout=120, cwd=str(repo))
        if proc.returncode != 0:
            return CheckResult(
                name, CRITICAL, False,
                f"regenerate_cmd exited rc={proc.returncode}",
                {"stderr": proc.stderr[-400:], "mode": "replay"},
            )
        if not tmp_artifact.is_file():
            return CheckResult(
                name, CRITICAL, False,
                "regenerate_cmd produced no artifact",
                {"expected": str(tmp_artifact), "mode": "replay"},
            )
        h_replay = _hash(tmp_artifact)
        if h_replay != h_orig:
            return CheckResult(
                name, CRITICAL, False,
                "byte-identity violated (sha256 differs)",
                {"sha256_orig": h_orig, "sha256_replay": h_replay,
                 "mode": "replay"},
            )
        return CheckResult(
            name, CRITICAL, True,
            "byte-identical on replay",
            {"sha256": h_orig, "mode": "replay"},
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, CRITICAL, False,
                           "regenerate_cmd timed out (>120s)",
                           {"mode": "replay"})
    finally:
        try:
            if tmp_artifact.is_file():
                tmp_artifact.unlink()
        except OSError:
            pass


# ── Check 4: constitutional-signoff ────────────────────────────────────────


def check_constitutional_signoff(submission_id: str, repo: Path) -> CheckResult:
    """Look for a chief 3-Surgeon decision referencing ``submission_id``.

    The audit pipeline writes decisions to
    ``.fleet/audits/<YYYY-MM-DD>-decisions.md`` with cluster_ids of the form
    ``C-<detector>-<class>``. For submissions, the convention is to use
    ``cluster_id = "C-submission-<submission_id>"`` (or the submission_id
    appears in finding_ids / rationale). We accept any of those references
    so that minor convention shifts in S1/chief don't break the gate.
    """
    name = "constitutional-signoff"
    audits_dir = repo / ".fleet" / "audits"
    if not audits_dir.is_dir():
        return CheckResult(name, CRITICAL, False,
                           f"no audits dir: {audits_dir}")

    # Search the last 7 days of decisions docs.
    needle = submission_id
    today = _dt.date.today()
    matches: List[str] = []
    for delta in range(0, 7):
        d = (today - _dt.timedelta(days=delta)).strftime("%Y-%m-%d")
        path = audits_dir / f"{d}-decisions.md"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle in text:
            # Try to surface the decision verdict adjacent to the match.
            for line in text.splitlines():
                if needle in line and line.startswith("### "):
                    matches.append(f"{d}: {line[4:200]}")
                    break
            else:
                matches.append(f"{d}: reference found")
    if not matches:
        return CheckResult(
            name, CRITICAL, False,
            f"no chief decision references submission_id={submission_id} "
            f"in last 7d of .fleet/audits/*-decisions.md",
        )
    # Reject if any matched line says ROLLBACK / HALT / ESCALATE.
    bad = [m for m in matches if any(
        v in m for v in ("ROLLBACK", "HALT_GREEN_LIGHT", "ESCALATE_TO_RED"))]
    if bad:
        return CheckResult(
            name, CRITICAL, False,
            f"chief decision blocks submission: {bad[0][:120]}",
            {"matches": matches},
        )
    return CheckResult(
        name, CRITICAL, True,
        f"signoff found ({len(matches)} reference(s))",
        {"matches": matches[:3]},
    )


# ── Check 5: evidence-ledger ───────────────────────────────────────────────


def check_evidence_ledger(submission_id: str, artifact: Path,
                          metadata_obj: Dict[str, Any],
                          repo: Path) -> CheckResult:
    name = "evidence-ledger"
    sys.path.insert(0, str(repo / "multi-fleet"))
    try:
        from multifleet.evidence_ledger import (  # type: ignore
            EvidenceLedger,
        )
    except Exception:  # noqa: BLE001 — S2 not shipped → graceful fallback
        EvidenceLedger = None  # type: ignore
    finally:
        try:
            sys.path.remove(str(repo / "multi-fleet"))
        except ValueError:
            pass

    payload = {
        "submission_id": submission_id,
        "artifact_path": str(artifact),
        "sha256": _hash(artifact) if artifact.is_file() else "",
        "metadata": metadata_obj,
        "ts": int(_dt.datetime.now().timestamp()),
    }

    if EvidenceLedger is not None:
        # Try several known constructor / record() shapes. The ledger module
        # has been generalized through multiple iterations — we accept any
        # working API. Any exception falls through to the fallback writer.
        try:
            ledger = EvidenceLedger()
            # Current API: record(event_type, node_id, subject, ...).
            try:
                entry_id = ledger.record(
                    event_type="submission_gate",
                    node_id=os.environ.get("MULTIFLEET_NODE_ID", "local"),
                    subject=str(submission_id),
                    payload=payload,
                )
            except TypeError:
                # Older / generalized API: record(kind=..., payload=...).
                entry_id = ledger.record(  # type: ignore[call-arg]
                    kind="submission_gate", payload=payload,
                )
            return CheckResult(
                name, CRITICAL, True,
                f"S2 EvidenceLedger entry={entry_id}",
                {"fallback": False, "entry_id": str(entry_id)},
            )
        except Exception as exc:  # noqa: BLE001 — observable: degrade to fallback
            # ZSF: surface to stderr, then write the fallback record so the
            # operator gets BOTH the failure detail AND a durable audit row.
            print(f"[submission-gate] S2 ledger unavailable: {exc} "
                  f"(falling back to JSONL)", file=sys.stderr)

    # Fallback: append a JSON-line directly to a known evidence file.
    fallback_dir = repo / ".fleet" / "evidence"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = fallback_dir / "submission-gate.jsonl"
    try:
        with fallback_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except OSError as exc:
        return CheckResult(
            name, CRITICAL, False,
            f"fallback ledger write failed: {exc}",
            {"fallback": True},
        )
    return CheckResult(
        name, WARNING, True,
        f"fallback ledger appended ({fallback_path.name})",
        {"fallback": True, "path": str(fallback_path)},
    )


# ── Check 6: leaderboard-guard ─────────────────────────────────────────────


def check_leaderboard_guard(submission_id: str, artifact: Path,
                            metadata_obj: Dict[str, Any],
                            repo: Path) -> CheckResult:
    name = "leaderboard-guard"
    sys.path.insert(0, str(repo / "multi-fleet"))
    try:
        from multifleet.leaderboard_guard import (  # type: ignore
            check as guard_check,
        )
    except Exception:  # noqa: BLE001 — S4 not shipped → graceful fallback
        guard_check = None  # type: ignore
    finally:
        try:
            sys.path.remove(str(repo / "multi-fleet"))
        except ValueError:
            pass

    if guard_check is None:
        return CheckResult(
            name, WARNING, True,
            "S4 leaderboard_guard not shipped — fallback PASS-with-warning",
            {"fallback": True},
        )

    try:
        verdict = guard_check(
            submission_id=submission_id,
            artifact_path=str(artifact),
            metadata=metadata_obj,
        )
    except Exception as exc:  # noqa: BLE001 — surface guard failure
        return CheckResult(
            name, CRITICAL, False,
            f"S4 guard raised: {exc}", {"fallback": False},
        )

    # Verdict shape: {"verdict": "...", "reason": "..."}; "divergence-warning"
    # is the explicit fail signal.
    v = verdict.get("verdict") if isinstance(verdict, dict) else str(verdict)
    if v == "divergence-warning":
        reason = verdict.get("reason", "") if isinstance(verdict, dict) else ""
        return CheckResult(
            name, CRITICAL, False,
            f"divergence-warning: {reason[:200]}",
            {"verdict": v, "fallback": False},
        )
    return CheckResult(
        name, CRITICAL, True,
        f"verdict={v}", {"verdict": v, "fallback": False},
    )


# ── Check 7: no-secrets ────────────────────────────────────────────────────


def check_no_secrets(artifact: Path, repo: Path) -> CheckResult:
    """Regex-scan the submission for credential shapes.

    Reuses the canonical patterns from
    ``multi-fleet/multifleet/secret_redact.py`` so this gate cannot drift from
    the rest of the fleet. Falls back to an inline minimal regex set if the
    redactor is unimportable (mf in transition).
    """
    name = "no-secrets"
    sys.path.insert(0, str(repo / "multi-fleet"))
    patterns: List[Tuple[str, "re.Pattern[str]"]] = []
    try:
        from multifleet.secret_redact import PATTERNS as SR_PATTERNS  # type: ignore
        for entry in SR_PATTERNS:
            # Each entry is (name, compiled_pattern, sub_fn) — we only need
            # the first two for detection.
            patterns.append((entry[0], entry[1]))
    except Exception:  # noqa: BLE001 — fallback inline patterns
        inline = [
            ("openai_like", r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
            ("github_pat", r"\bghp_[A-Za-z0-9]{36}\b"),
            ("aws_akid", r"\bAKIA[0-9A-Z]{16}\b"),
            ("password_assign",
             r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]"
             r"\s*['\"]?([A-Za-z0-9_\-./+=]{8,})['\"]?"),
            ("bearer", r"\bBearer\s+[A-Za-z0-9_.\-=]{20,}\b"),
        ]
        patterns = [(n, re.compile(p)) for n, p in inline]
    finally:
        try:
            sys.path.remove(str(repo / "multi-fleet"))
        except ValueError:
            pass

    if not artifact.is_file():
        return CheckResult(name, CRITICAL, False, "artifact not readable")

    # Read the artifact in chunks to defend against very large files; cap at
    # 64 MB scan budget (anything larger is a CSV submission — patterns will
    # land in the first MB if they're going to land at all).
    SCAN_BUDGET = 64 * 1024 * 1024
    hits: List[str] = []
    try:
        with artifact.open("rb") as fh:
            buf = fh.read(SCAN_BUDGET)
        text = buf.decode("utf-8", errors="replace")
    except OSError as exc:
        return CheckResult(name, CRITICAL, False, f"read failed: {exc}")

    for pat_name, pat in patterns:
        try:
            m = pat.search(text)
        except Exception:  # noqa: BLE001 — bad pattern shouldn't fail gate
            continue
        if m:
            hits.append(f"{pat_name}@{m.start()}")
    if hits:
        return CheckResult(
            name, CRITICAL, False,
            f"{len(hits)} secret-shape hit(s): {', '.join(hits[:3])}",
            {"hits": hits},
        )
    return CheckResult(name, CRITICAL, True,
                       f"clean ({len(patterns)} patterns scanned)")


# ── Check 8: reversibility-path ────────────────────────────────────────────


def check_reversibility_path(artifact: Path, repo: Path) -> CheckResult:
    """Artifact MUST live under ``submissions/`` (or under repo's submissions
    subdir). Constitutional Physics #5 — Prefer Reversible Actions: a single
    ``rm`` (or ``git revert`` of the publish commit) reverts the publication.
    """
    name = "reversibility-path"
    try:
        artifact_abs = artifact.resolve()
        repo_abs = repo.resolve()
    except OSError as exc:
        return CheckResult(name, CRITICAL, False, f"resolve failed: {exc}")
    try:
        rel = artifact_abs.relative_to(repo_abs)
    except ValueError:
        return CheckResult(
            name, CRITICAL, False,
            f"artifact outside repo: {artifact_abs}",
        )
    parts = rel.parts
    if not parts or parts[0] != "submissions":
        return CheckResult(
            name, CRITICAL, False,
            f"artifact not under submissions/: {rel}",
        )
    # Defend against accidental writes into core trees that *happen* to be
    # named submissions/ but live under memory/ or backend/.
    risky_parents = {"memory", "backend", "scripts", "system", "tools"}
    if any(p in risky_parents for p in parts[1:]):
        return CheckResult(
            name, CRITICAL, False,
            f"artifact path traverses core dir: {rel}",
        )
    return CheckResult(name, CRITICAL, True, f"under {parts[0]}/")


# ── Driver ─────────────────────────────────────────────────────────────────


def run_all(artifact: Path, metadata_obj: Dict[str, Any],
            repo: Path) -> List[CheckResult]:
    submission_id = str(metadata_obj.get("submission_id", "")) or "?"
    return [
        check_artifact_exists(artifact),
        check_metadata_schema(metadata_obj, repo),
        check_determinism(artifact, metadata_obj, repo),
        check_constitutional_signoff(submission_id, repo),
        check_evidence_ledger(submission_id, artifact, metadata_obj, repo),
        check_leaderboard_guard(submission_id, artifact, metadata_obj, repo),
        check_no_secrets(artifact, repo),
        check_reversibility_path(artifact, repo),
    ]


def write_audit(repo: Path, artifact: Path, metadata_obj: Dict[str, Any],
                results: List[CheckResult]) -> Path:
    log_path = _audit_log_path(repo)
    record = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "submission_id": metadata_obj.get("submission_id", ""),
        "artifact": str(artifact),
        "checks": [
            {
                "name": r.name,
                "severity": r.severity,
                "passed": r.passed,
                "detail": r.detail,
                "extra": r.extra,
            }
            for r in results
        ],
        "summary": {
            "passed": sum(1 for r in results if r.passed),
            "critical_failures": sum(
                1 for r in results
                if not r.passed and r.severity == CRITICAL
            ),
            "warnings": sum(
                1 for r in results
                if not r.passed and r.severity == WARNING
            ),
        },
    }
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        # ZSF: surface, don't swallow. Use stderr so the operator sees it.
        print(f"[submission-gate] audit log write failed: {exc}",
              file=sys.stderr)
    return log_path


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Competition Submission Governance gate")
    parser.add_argument("--artifact", required=True,
                        help="path to submission artifact (CSV / file)")
    parser.add_argument("--metadata", required=True,
                        help="path to metadata JSON")
    parser.add_argument("--repo", default=None,
                        help="repo root (defaults to git toplevel)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON results to stdout")
    args = parser.parse_args(argv)

    artifact = Path(args.artifact)
    metadata = Path(args.metadata)

    if args.repo:
        repo = Path(args.repo)
    else:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True, timeout=10,
            )
            repo = Path(out.stdout.strip())
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            repo = Path.cwd()

    if not metadata.is_file():
        print(f"[submission-gate] FAIL: metadata not found: {metadata}",
              file=sys.stderr)
        return SETUP_ERROR_RC
    try:
        metadata_obj = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[submission-gate] FAIL: metadata unparseable: {exc}",
              file=sys.stderr)
        return SETUP_ERROR_RC
    if not isinstance(metadata_obj, dict):
        print("[submission-gate] FAIL: metadata must be a JSON object",
              file=sys.stderr)
        return SETUP_ERROR_RC

    results = run_all(artifact, metadata_obj, repo)
    log_path = write_audit(repo, artifact, metadata_obj, results)

    if args.json:
        print(json.dumps([r.__dict__ for r in results], default=str))
    else:
        for r in results:
            mark = "PASS" if r.passed else (
                "CRIT" if r.severity == CRITICAL else "WARN")
            print(f"  [{mark}] {r.name} — {r.detail}")
        crit = sum(1 for r in results
                   if not r.passed and r.severity == CRITICAL)
        passed = sum(1 for r in results if r.passed)
        print(f"\nSummary: {passed}/{len(results)} pass, "
              f"{crit} critical fail(s); audit log: {log_path}")

    if any(not r.passed and r.severity == CRITICAL for r in results):
        return GATE_FAIL_RC
    return GATE_PASS_RC


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
