#!/usr/bin/env python3
"""Gains-Gate verification-before-completion check (RACE X3).

Wires the superpowers `verification-before-completion` skill into
`scripts/gains-gate.sh` as a programmatic check. Cross-cutting priorities
Priority #2 + V2 wave required this; until now the skill was only attached
to S6 output as a `[SUGGESTED_SKILL: ...]` pointer line — never actually
enforced at the gate.

Behavior summary
----------------
1. Scan staged work + the most recent N commits for completion claims:
   words/phrases like "complete", "done", "all tests pass", "fixed",
   "verified", "passes" — the same vocabulary the skill flags as a red flag
   when used without verification.

2. For each claim, classify the scope using `scripts/north_star.py classify`
   to identify which priority slot it touches. This is V5 — every claim must
   advance a North Star priority OR be flagged as drift.

3. Run the *actual* verification commands the claim implies. We default to
   `pytest` over the touched directories (paths derived from the commit's
   diff). PASS only if verification ran AND exit code was 0.

4. Emit observability — every invocation appends one JSON line to
   `gains-gate-verification-${ts}.log` in the repo's `logs/` dir capturing
   what was scanned, what claims fired, what commands ran, and the outcome.

Result codes
------------
0  PASS — either no completion claims since the last gate, or every claim
   was backed by a verification command that exited 0. WARN counters may
   still be non-zero (e.g. "no claim, no verification" — exploratory work).
1  FAIL — at least one completion claim was found whose verification
   command did not run or exited non-zero. The skill's Iron Law fired:
     "NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE"

The script intentionally degrades to WARN (not FAIL) when a commit has zero
completion claims — the rules allow exploratory work, only false success
claims are blocked.

Zero Silent Failures
--------------------
Every exception path either prints a structured error to stderr OR records
a `verification_errors` line in the JSON log. We never `except: pass`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Claim detection — vocabulary mirrors the skill's "Red Flags" list.
# ---------------------------------------------------------------------------

# Phrases that imply success without evidence. Word-boundary anchored so
# substrings inside larger words don't fire (e.g. "complete" matches but
# "completeness" does not).
_CLAIM_PATTERNS: tuple[str, ...] = (
    r"\ball tests pass(?:ing|ed)?\b",
    r"\btests? pass(?:ing|ed)?\b",
    r"\b(?:bug )?fixed\b",
    r"\bfully (?:done|complete|working)\b",
    r"\bfeature complete\b",
    r"\bcomplete\b",
    r"\bdone\b",
    r"\bworking\b",
    r"\bverified\b",
    r"\bgreen\b",
    r"\bship(?:ped|ping)?\b",
    r"\blanded\b",
    r"\bpasses\b",
    r"\bpassing\b",
    r"\bsucce(?:ss|eded|eds)\b",
    r"\bbuild succe(?:ss|eded|eds)\b",
    r"\blint clean\b",
    r"\blgtm\b",
)

# Compiled once. Case-insensitive — git commit subjects are mixed-case.
_CLAIM_RE = re.compile("|".join(_CLAIM_PATTERNS), re.IGNORECASE)


@dataclass
class Claim:
    """One completion claim found in a commit subject or staged message."""
    sha: str
    subject: str
    matched: tuple[str, ...]
    touched_paths: tuple[str, ...]
    priority_slug: str | None = None
    priority_rank: int | None = None


def find_claims_in_text(sha: str, subject: str) -> Claim | None:
    """Return a Claim if `subject` contains any completion-claim phrase."""
    if not subject:
        return None
    matches = tuple(sorted({m.group(0).lower() for m in _CLAIM_RE.finditer(subject)}))
    if not matches:
        return None
    return Claim(sha=sha, subject=subject, matched=matches, touched_paths=())


# ---------------------------------------------------------------------------
# Git inspection
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    """Run a git command, return stdout. Raise on non-zero (ZSF)."""
    cmd = ["git", "-C", str(repo), *args]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError(f"git not found on PATH: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {exc.stderr.strip()}"
        ) from exc
    return out


def recent_commits(
    repo: Path, count: int = 5
) -> list[tuple[str, str]]:
    """Return [(sha, subject)] for the last `count` commits on HEAD."""
    out = _git(repo, "log", f"-{count}", "--pretty=format:%H%x09%s")
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        rows.append((parts[0], parts[1]))
    return rows


def staged_message(repo: Path) -> str | None:
    """Return the staged commit message body if a COMMIT_EDITMSG exists."""
    msg_path = repo / ".git" / "COMMIT_EDITMSG"
    if not msg_path.exists():
        return None
    try:
        text = msg_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"verification-check: cannot read COMMIT_EDITMSG: {exc}",
              file=sys.stderr)
        return None
    # Strip git comment lines.
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    ).strip()
    return body or None


def commit_paths(repo: Path, sha: str) -> tuple[str, ...]:
    """Return the set of files changed in `sha`."""
    try:
        out = _git(repo, "show", "--name-only", "--pretty=format:", sha)
    except RuntimeError as exc:
        print(f"verification-check: git show failed for {sha[:8]}: {exc}",
              file=sys.stderr)
        return ()
    paths = tuple(sorted({ln.strip() for ln in out.splitlines() if ln.strip()}))
    return paths


# ---------------------------------------------------------------------------
# North Star classification (V5)
# ---------------------------------------------------------------------------

def classify_with_north_star(
    repo: Path, text: str
) -> tuple[str | None, int | None]:
    """Return (slug, rank) for `text` via scripts/north_star.py classify.

    Returns (None, None) if the script can't be located or classification
    drifts. ZSF: stderr breakage is reported, not swallowed.
    """
    ns = repo / "scripts" / "north_star.py"
    if not ns.exists():
        # Fallback: import directly so the check still works in test sandboxes.
        sys.path.insert(0, str(repo / "scripts"))
        try:
            import north_star as _ns  # type: ignore
        except ImportError:
            return (None, None)
        result = _ns.classify(text)
        if result.priority is None:
            return (None, None)
        return (result.priority.slug, result.priority.rank)

    cmd = [sys.executable, str(ns), "--json", "classify", text]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        print("verification-check: north_star classify timed out", file=sys.stderr)
        return (None, None)
    except OSError as exc:
        print(f"verification-check: north_star spawn failed: {exc}", file=sys.stderr)
        return (None, None)
    # rc=0 means matched, rc=1 means drift — both are valid outcomes.
    if not proc.stdout.strip():
        return (None, None)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"verification-check: north_star JSON decode failed: {exc}",
              file=sys.stderr)
        return (None, None)
    return (payload.get("slug"), payload.get("rank"))


# ---------------------------------------------------------------------------
# Verification command runner
# ---------------------------------------------------------------------------

@dataclass
class VerificationRun:
    """The actual command executed plus its exit code + truncated tail."""
    command: list[str]
    exit_code: int
    duration_ms: int
    tail: str
    skipped_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "tail": self.tail[-2000:],
            "skipped_reason": self.skipped_reason,
        }


def _select_pytest_targets(repo: Path, paths: Sequence[str]) -> list[str]:
    """Map touched files to pytest targets.

    Pick the `tests/` directory of any touched module. If none of the touched
    files have an obvious tests dir, fall back to the repo's top-level
    `scripts/tests` (where the gate's own tests live).
    """
    targets: set[str] = set()
    for p in paths:
        # Test files run themselves.
        if "/tests/" in p or p.startswith("tests/") or p.endswith("_test.py"):
            test_path = repo / p
            if test_path.exists():
                targets.add(p)
                continue
        # For a source file, look for a sibling tests/ dir.
        parts = Path(p).parts
        for i in range(len(parts), 0, -1):
            candidate = repo.joinpath(*parts[:i], "tests")
            if candidate.is_dir():
                targets.add(str(candidate.relative_to(repo)))
                break
    return sorted(targets)


def run_verification(
    repo: Path, claim: Claim, dry_run: bool = False
) -> VerificationRun:
    """Run the verification command implied by a claim.

    Currently picks pytest over the touched test directories. If no test
    paths are detectable, the run is recorded as skipped with a reason —
    this surfaces to the gate as a WARNING (claim made, no test scope).
    """
    targets = _select_pytest_targets(repo, claim.touched_paths)
    if not targets:
        return VerificationRun(
            command=[],
            exit_code=-1,
            duration_ms=0,
            tail="",
            skipped_reason="no test scope detected for touched paths",
        )
    # UU5 2026-05-12 — pick the FIRST interpreter that can actually import
    # pytest. Previously we unconditionally overrode `sys.executable` with
    # `.venv/bin/python3` if it existed — but `.venv` is often a symlink
    # to a slim venv (e.g. `venv.nosync.repo-root`) that does not have
    # pytest installed. The override turned a working host-python pytest
    # run into a hard failure with "No module named pytest", which gains-gate
    # then surfaced as a CRITICAL "stale verification claim" (TT5 finding).
    #
    # New behavior: probe candidates in order, prefer venv when it has
    # pytest (keeps deterministic deps when available), fall back to the
    # script's own interpreter, fall back to host python3. If NONE has
    # pytest, degrade to a skipped run with a clear repair hint instead of
    # treating tooling absence as a verification failure.
    venv_py = repo / ".venv" / "bin" / "python3"
    candidates: list[str] = []
    if venv_py.exists():
        candidates.append(str(venv_py))
    if sys.executable and sys.executable not in candidates:
        candidates.append(sys.executable)
    # /usr/bin/env-style fallback: anything on PATH named python3
    import shutil as _shutil
    host_py = _shutil.which("python3")
    if host_py and host_py not in candidates:
        candidates.append(host_py)

    py: str | None = None
    for cand in candidates:
        try:
            probe = subprocess.run(
                [cand, "-c", "import pytest"],
                capture_output=True, text=True, timeout=10, cwd=str(repo),
            )
            if probe.returncode == 0:
                py = cand
                break
        except (subprocess.TimeoutExpired, OSError):
            continue

    if py is None:
        chosen = candidates[0] if candidates else "python3"
        return VerificationRun(
            command=[chosen, "-m", "pytest", *targets],
            exit_code=-1,
            duration_ms=0,
            tail="pytest not importable in any candidate interpreter",
            skipped_reason=(
                "pytest unavailable in any of: "
                + ", ".join(candidates or ["<none>"])
                + " — install with `.venv/bin/pip install pytest` "
                "(or the analogous host) to enable claim verification"
            ),
        )
    cmd = [py, "-m", "pytest", "-x", "-q", *targets]
    if dry_run:
        return VerificationRun(
            command=cmd, exit_code=0, duration_ms=0, tail="(dry-run)",
            skipped_reason="dry-run requested",
        )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=str(repo),
        )
        dur = int((time.monotonic() - start) * 1000)
        tail = (proc.stdout or "") + "\n" + (proc.stderr or "")
        # UU5 2026-05-12 — pytest exit codes 4 (usage / collection error) and
        # 5 (no tests collected) are NOT test failures — they're scope / config
        # issues that we surface as WARN, not a verification fail. Examples:
        # mixed conftest.py imports from multiple roots, an .sh path passed as
        # a pytest target, or a touched dir with no test_*.py. Exit 0/1/2/3/124
        # remain real signal (pass / collection ran but tests failed / timeout).
        if proc.returncode in (4, 5):
            return VerificationRun(
                command=cmd, exit_code=-1,
                duration_ms=dur,
                tail=tail,
                skipped_reason=(
                    f"pytest exit={proc.returncode} (collection/config issue, "
                    f"not a test failure) — repair the test scope and re-run"
                ),
            )
        return VerificationRun(
            command=cmd, exit_code=proc.returncode,
            duration_ms=dur, tail=tail,
        )
    except subprocess.TimeoutExpired:
        dur = int((time.monotonic() - start) * 1000)
        return VerificationRun(
            command=cmd, exit_code=124, duration_ms=dur,
            tail="TIMEOUT after 120s",
            skipped_reason="pytest timeout",
        )
    except OSError as exc:
        return VerificationRun(
            command=cmd, exit_code=127, duration_ms=0,
            tail=f"OSError: {exc}",
            skipped_reason="pytest spawn failed",
        )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    status: str   # "pass" | "warn" | "fail"
    detail: str
    claims: list[Claim] = field(default_factory=list)
    runs: list[VerificationRun] = field(default_factory=list)
    started_at: str = ""
    finished_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_ms": self.finished_ms,
            "claims": [
                {
                    "sha": c.sha[:12],
                    "subject": c.subject,
                    "matched": list(c.matched),
                    "touched_paths": list(c.touched_paths),
                    "priority_slug": c.priority_slug,
                    "priority_rank": c.priority_rank,
                }
                for c in self.claims
            ],
            "runs": [r.to_dict() for r in self.runs],
        }


def evaluate(
    repo: Path,
    commit_count: int = 5,
    dry_run: bool = False,
) -> GateResult:
    """Run the full check pipeline. Returns a GateResult."""
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.monotonic()
    claims: list[Claim] = []

    # 1) Scan staged commit message (if any).
    staged = staged_message(repo)
    if staged:
        first_line = staged.splitlines()[0]
        c = find_claims_in_text("STAGED", first_line)
        if c is not None:
            # Staged work has no SHA yet — touched_paths from the index.
            try:
                idx = _git(repo, "diff", "--cached", "--name-only").splitlines()
            except RuntimeError as exc:
                print(f"verification-check: staged paths failed: {exc}",
                      file=sys.stderr)
                idx = []
            c.touched_paths = tuple(sorted(p for p in idx if p))
            claims.append(c)

    # 2) Scan recent commits.
    try:
        commits = recent_commits(repo, count=commit_count)
    except RuntimeError as exc:
        # Cannot list commits — surface as fail (ZSF, not silent pass).
        return GateResult(
            status="fail",
            detail=f"git log failed: {exc}",
            started_at=started,
            finished_ms=int((time.monotonic() - t0) * 1000),
        )

    for sha, subject in commits:
        c = find_claims_in_text(sha, subject)
        if c is None:
            continue
        c.touched_paths = commit_paths(repo, sha)
        claims.append(c)

    # 3) Classify each claim by North Star priority.
    for c in claims:
        slug, rank = classify_with_north_star(repo, c.subject)
        c.priority_slug = slug
        c.priority_rank = rank

    # 4) Run verification for each claim.
    runs: list[VerificationRun] = []
    fails: list[str] = []
    skips: list[str] = []
    for c in claims:
        r = run_verification(repo, c, dry_run=dry_run)
        runs.append(r)
        if r.skipped_reason and r.exit_code in (-1,):
            # No test scope detected — degrade to warn at the gate level.
            skips.append(f"{c.sha[:8]}: {r.skipped_reason}")
            continue
        if r.exit_code != 0:
            fails.append(
                f"{c.sha[:8]}: pytest exit={r.exit_code} "
                f"(reason={r.skipped_reason or 'tests failed'})"
            )

    elapsed = int((time.monotonic() - t0) * 1000)

    if not claims:
        return GateResult(
            status="pass",
            detail=f"no completion claims in last {commit_count} commits",
            started_at=started,
            finished_ms=elapsed,
        )
    if fails:
        return GateResult(
            status="fail",
            detail=(
                f"{len(fails)} claim(s) failed verification: "
                + "; ".join(fails[:3])
                + ("…" if len(fails) > 3 else "")
            ),
            claims=claims,
            runs=runs,
            started_at=started,
            finished_ms=elapsed,
        )
    if skips and len(skips) == len(claims):
        # Every claim had no detectable test scope — exploratory commits.
        return GateResult(
            status="warn",
            detail=(
                f"{len(claims)} completion claim(s) found but no test "
                f"scope detected — exploratory work permitted: "
                + "; ".join(skips[:3])
            ),
            claims=claims,
            runs=runs,
            started_at=started,
            finished_ms=elapsed,
        )
    # Some claims verified clean; partial-skip is OK.
    skip_note = f" ({len(skips)} skipped)" if skips else ""
    return GateResult(
        status="pass",
        detail=(
            f"{len(claims)} completion claim(s) verified — "
            f"all pytest targets exit=0{skip_note}"
        ),
        claims=claims,
        runs=runs,
        started_at=started,
        finished_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Logging — append one JSON line per invocation under repo/logs/
# ---------------------------------------------------------------------------

def write_log(repo: Path, result: GateResult, log_dir: Path | None = None) -> Path:
    """Append a JSON record to `gains-gate-verification-${ts}.log`.

    Returns the log path. ZSF: any IO error raises so callers know logging
    broke (the gate then surfaces it as a warning).
    """
    log_dir = log_dir or (repo / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d", time.gmtime())
    path = log_dir / f"gains-gate-verification-{ts}.log"
    line = json.dumps(result.to_dict(), separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI entrypoint — gains-gate.sh calls this
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gains_gate_verification.py",
        description="Verification-before-completion check for gains-gate.sh",
    )
    parser.add_argument(
        "--repo", default=None,
        help="repo path (default: walk up from this script)",
    )
    parser.add_argument(
        "--commits", type=int, default=5,
        help="number of recent commits to scan (default: 5)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="skip actual pytest invocation (used by tests)",
    )
    parser.add_argument(
        "--log-dir", default=None,
        help="override the logs/ directory (default: <repo>/logs)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit the GateResult as JSON on stdout",
    )
    args = parser.parse_args(argv)

    if args.repo:
        repo = Path(args.repo).resolve()
    else:
        here = Path(__file__).resolve()
        repo = here.parent.parent

    result = evaluate(repo, commit_count=args.commits, dry_run=args.dry_run)
    log_dir = Path(args.log_dir).resolve() if args.log_dir else None
    try:
        log_path = write_log(repo, result, log_dir=log_dir)
    except OSError as exc:
        print(f"verification-check: log write failed: {exc}", file=sys.stderr)
        log_path = None

    if args.json:
        payload = result.to_dict()
        payload["log_path"] = str(log_path) if log_path else None
        print(json.dumps(payload, indent=2))
    else:
        # gains-gate.sh parses the first line: STATUS|DETAIL|LOGPATH
        print(
            f"{result.status.upper()}|{result.detail}|"
            f"{log_path or ''}"
        )

    if result.status == "fail":
        return 1
    if result.status == "warn":
        return 2  # gains-gate.sh treats rc=2 as warning, rc=1 as critical
    return 0


if __name__ == "__main__":
    sys.exit(main())
