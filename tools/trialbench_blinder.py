"""TrialBench blinded-package generator (D5 missing 1/9 v0 deliverable).

Strips arm labels + identifying metadata from TrialBench run JSONs so a
(future) human reviewer can adjudicate without knowing which condition
produced which outcome.

Pipeline (v0):
  artifacts/trialbench/<trial_id>/run_*.json
       │  (input — arm labels visible)
       ▼
  create_blinded_package(run, policy)
       │  strips per BlindingPolicy, redacts arm-label strings in payload/content
       ▼
  artifacts/trialbench/<trial_id>/blinded/<blind_id>.json
       │  + manifest.json (blind_id list)
       │  + unblinding_map.json (chmod 600 — separate file, NOT in manifest)
       ▼
  blinded reviewer (synthetic LLM v0 → real human v1)

Trust-and-safety invariants:
  - ZERO SILENT FAILURES (every parse/IO error → counter + recorded fixme)
  - ASYMMETRIC-LEAKAGE-SAFE: if unblinding map can't be persisted, ALL
    blinded packages are deleted so a partial leak can't reach a reviewer
    with no way to unblind.
  - stdlib only.

The generator is purely additive — no edits to trialbench.py or
trialbench_score.py.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import sys
import uuid


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

BLINDED_DIR_TEMPLATE = "artifacts/trialbench/{trial_id}/blinded"

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ARTIFACTS_ROOT = REPO_ROOT / "artifacts" / "trialbench"

# Tokens that would unblind a reviewer if leaked into outcome content.
# Order matters — longer/more specific first to avoid partial replacements.
_ARM_LABEL_TOKENS = (
    "C_contextdna_packet",
    "C_governed",
    "A_raw",
    "B_synthetic",
)

# Per-blinded-package schema. The set of REQUIRED keys is checked before
# every write; optional keys may be present but never required.
_BLINDED_REQUIRED_KEYS = (
    "blind_id",
    "task_hash",
    "task_summary",
    "outcome_content",
    "metadata_hash",
    "schema_version",
)
_BLINDED_SCHEMA_VERSION = "v0.1.0"


# ---------------------------------------------------------------------------
# ZSF counters (process-local; mirror trialbench.py's pattern of recording
# failures rather than swallowing them).
# ---------------------------------------------------------------------------

COUNTERS: dict[str, int] = {
    "blinded_package_errors": 0,
    "blinded_package_validation_failures": 0,
    "blinded_package_redaction_warnings": 0,
}


def _bump(counter: str, n: int = 1) -> None:
    COUNTERS[counter] = COUNTERS.get(counter, 0) + n


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class BlindingPolicy:
    """Controls which fields are stripped during blinding.

    strip_protocol_hash defaults False because the operator wants
    verifiability — the protocol hash is a public anchor, not an
    identifying secret.
    """

    strip_arm_label: bool = True
    strip_node_id: bool = True
    strip_protocol_hash: bool = False


@dataclasses.dataclass
class BlindedPackage:
    """A single arm-stripped run package destined for a blinded reviewer."""

    blind_id: str
    task_hash: str
    task_summary: str
    outcome_content: str
    metadata_hash: str
    # Replay-relevant fields preserved per spec — kept on the dataclass
    # (not the schema's required tuple) so existing readers don't break
    # if the dataclass is widened later.
    latency_ms: int | None = None
    exit_code: int | None = None
    http_status: object | None = None
    finished_at: str | None = None
    schema_version: str = _BLINDED_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Core blinding logic
# ---------------------------------------------------------------------------

def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _sha256_hex(data: str | bytes, *, prefix: int | None = None) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    h = hashlib.sha256(data).hexdigest()
    return h[:prefix] if prefix else h


def _redact_arm_tokens(text: str) -> tuple[str, int]:
    """Replace any arm-label tokens in free-text content with [REDACTED_ARM].

    Returns (redacted_text, redaction_count). Used as defense-in-depth: even
    if a model echoes back its arm label inside outcome_content, we strip it.
    """
    if not isinstance(text, str) or not text:
        return text, 0
    redactions = 0
    out = text
    for token in _ARM_LABEL_TOKENS:
        # word-boundary-ish: token may appear in JSON, prose, or as a key.
        # Use plain substring replace because arm tokens contain underscores
        # which \b doesn't treat as a boundary.
        if token in out:
            occurrences = out.count(token)
            out = out.replace(token, "[REDACTED_ARM]")
            redactions += occurrences
    return out, redactions


def _summarize_task(run: dict) -> str:
    """Produce a short, arm-agnostic task summary for the reviewer.

    Pulls from request_payload.task_world.title when available (governed
    packets carry it), else falls back to the first 200 chars of the
    prompt with arm tokens redacted.
    """
    payload = run.get("request_payload") or {}
    if isinstance(payload, dict):
        task_world = payload.get("task_world") or {}
        if isinstance(task_world, dict):
            title = task_world.get("title")
            if isinstance(title, str) and title.strip():
                redacted, _ = _redact_arm_tokens(title.strip())
                return redacted
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            head = prompt.strip().splitlines()[0] if prompt.strip() else ""
            redacted, _ = _redact_arm_tokens(head[:200])
            return redacted
    # Last resort
    task_id = run.get("task_id", "unknown")
    return f"task:{task_id}"


def _strip_outcome_content(run: dict, policy: BlindingPolicy) -> tuple[str, int]:
    """Pull the model's content string and redact arm-label tokens.

    Returns (content, redaction_count). Content is always a string (possibly
    empty) so downstream consumers don't need a None branch.
    """
    raw = run.get("content")
    if raw is None:
        return "", 0
    text = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
    if policy.strip_arm_label:
        return _redact_arm_tokens(text)
    return text, 0


def _build_metadata_hash(run: dict, policy: BlindingPolicy) -> str:
    """Hash of the *unblinded* metadata so the reviewer's package has a
    fingerprint we can later cross-reference without exposing the fields.
    """
    fingerprint = {
        "trial_id": run.get("trial_id"),
        "task_id": run.get("task_id"),
        "arm": run.get("arm") if not policy.strip_arm_label else None,
        "node": run.get("node") if not policy.strip_node_id else None,
        "protocol_hash": (
            run.get("protocol_hash") if not policy.strip_protocol_hash else None
        ),
        "started_at": run.get("started_at"),
    }
    return _sha256_hex(json.dumps(fingerprint, sort_keys=True))


def create_blinded_package(
    run: dict,
    policy: BlindingPolicy | None = None,
) -> BlindedPackage:
    """Produce a single BlindedPackage from a TrialBench run dict.

    Never raises on malformed input — missing fields degrade to empty
    strings / None. Real malformed-JSON parse errors happen at the
    materialize boundary; this function is the pure, deterministic
    transform.
    """
    if policy is None:
        policy = BlindingPolicy()
    if not isinstance(run, dict):
        # Degenerate input — record + return an empty package so the
        # caller can still validate + decide whether to drop.
        _bump("blinded_package_errors")
        return BlindedPackage(
            blind_id=_short_uuid(),
            task_hash=_sha256_hex("invalid-run", prefix=8),
            task_summary="[invalid run dict]",
            outcome_content="",
            metadata_hash=_sha256_hex("invalid-run"),
        )

    task_id_raw = str(run.get("task_id", "unknown"))
    task_hash = _sha256_hex(task_id_raw, prefix=8)
    task_summary = _summarize_task(run)
    outcome_content, redactions = _strip_outcome_content(run, policy)
    if redactions:
        _bump("blinded_package_redaction_warnings", redactions)

    metadata_hash = _build_metadata_hash(run, policy)

    return BlindedPackage(
        blind_id=_short_uuid(),
        task_hash=task_hash,
        task_summary=task_summary,
        outcome_content=outcome_content,
        metadata_hash=metadata_hash,
        latency_ms=run.get("latency_ms"),
        exit_code=run.get("exit_code"),
        http_status=run.get("http_status"),
        finished_at=run.get("finished_at"),
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _validate_blinded_dict(d: dict) -> list[str]:
    """Return list of validation errors. Empty list = valid."""
    errors: list[str] = []
    if not isinstance(d, dict):
        return ["package is not a dict"]
    for key in _BLINDED_REQUIRED_KEYS:
        if key not in d:
            errors.append(f"missing required key: {key}")
    if "outcome_content" in d and not isinstance(d["outcome_content"], str):
        errors.append("outcome_content must be a string")
    # Defense-in-depth: scan stringly-serialised content for any arm token
    # that slipped past redaction. We compare against the canonical token
    # list so the check tracks _ARM_LABEL_TOKENS as it evolves.
    serialised = json.dumps(d, sort_keys=True, default=str)
    for token in _ARM_LABEL_TOKENS:
        if token in serialised:
            errors.append(f"arm-label leakage detected: {token!r}")
    return errors


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def _load_run_files(trial_dir: pathlib.Path) -> list[tuple[pathlib.Path, dict | None, str | None]]:
    """Return [(path, parsed_run_or_None, error_or_None), ...] for every
    run_*.json file in the trial dir. Sorted for determinism.
    """
    out: list[tuple[pathlib.Path, dict | None, str | None]] = []
    paths = sorted(trial_dir.glob("run_*.json"))
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _bump("blinded_package_errors")
            out.append((p, None, f"{type(e).__name__}: {e}"))
            continue
        if not isinstance(data, dict):
            _bump("blinded_package_errors")
            out.append((p, None, f"non-dict JSON root: {type(data).__name__}"))
            continue
        out.append((p, data, None))
    return out


def _restrict_permissions(path: pathlib.Path) -> bool:
    """chmod 600 on POSIX. Returns True if applied (or unsupported on
    platform — best-effort). On any failure, log + return False."""
    try:
        os.chmod(path, 0o600)
        return True
    except Exception as e:
        print(
            f"[trialbench_blinder] WARN chmod 600 failed on {path}: {e}",
            file=sys.stderr,
        )
        return False


def _delete_packages(blinded_dir: pathlib.Path, written: list[pathlib.Path]) -> None:
    """Best-effort cleanup of already-written blinded packages on rollback.

    If unblinding-map persistence fails we MUST NOT leave a population of
    packages in reviewer-readable form without any way to unblind them —
    that would be asymmetric leakage (reviewer can read, scorer can't
    correlate back to arm).
    """
    for p in written:
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            print(
                f"[trialbench_blinder] WARN rollback delete failed on {p}: {e}",
                file=sys.stderr,
            )
            _bump("blinded_package_errors")


def materialize_packages(
    trial_id: str,
    output_dir: pathlib.Path | None = None,
    policy: BlindingPolicy | None = None,
    artifacts_root: pathlib.Path | None = None,
) -> list[BlindedPackage]:
    """Read all runs for trial_id, blind them, and write per-run JSONs +
    a manifest + a separate unblinding map.

    Returns the list of BlindedPackages successfully written. ZSF: every
    error is counted (COUNTERS) and surfaced via stderr; the function
    never raises on individual run failures. The ONLY way this returns
    fewer than (and rolled-back to zero) packages is the unblinding-map
    write failing — that triggers the asymmetric-leakage cleanup.
    """
    if policy is None:
        policy = BlindingPolicy()
    root = artifacts_root or ARTIFACTS_ROOT
    trial_dir = root / trial_id
    if not trial_dir.exists():
        _bump("blinded_package_errors")
        print(
            f"[trialbench_blinder] ERROR trial dir missing: {trial_dir}",
            file=sys.stderr,
        )
        return []

    blinded_dir = (
        output_dir
        if output_dir is not None
        else trial_dir / "blinded"
    )
    blinded_dir.mkdir(parents=True, exist_ok=True)

    loaded = _load_run_files(trial_dir)
    if not loaded:
        print(
            f"[trialbench_blinder] WARN no run_*.json files in {trial_dir}",
            file=sys.stderr,
        )

    packages: list[BlindedPackage] = []
    written_paths: list[pathlib.Path] = []
    unblinding_entries: list[dict] = []
    parse_errors: list[dict] = []

    for src_path, run, err in loaded:
        if err is not None or run is None:
            parse_errors.append({"file": src_path.name, "error": err or "unknown"})
            continue
        pkg = create_blinded_package(run, policy)
        pkg_dict = pkg.to_dict()
        # Validate before write — if validation fails we DO NOT write.
        validation_errors = _validate_blinded_dict(pkg_dict)
        if validation_errors:
            _bump("blinded_package_validation_failures")
            print(
                f"[trialbench_blinder] ERROR validation failed for {src_path.name}: "
                f"{validation_errors}",
                file=sys.stderr,
            )
            continue

        out_path = blinded_dir / f"{pkg.blind_id}.json"
        try:
            out_path.write_text(
                json.dumps(pkg_dict, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            _bump("blinded_package_errors")
            print(
                f"[trialbench_blinder] ERROR writing {out_path}: {e}",
                file=sys.stderr,
            )
            continue

        packages.append(pkg)
        written_paths.append(out_path)
        unblinding_entries.append(
            {
                "blind_id": pkg.blind_id,
                "source_run_file": src_path.name,
                "trial_id": run.get("trial_id"),
                "task_id": run.get("task_id"),
                "arm": run.get("arm"),
                "node": run.get("node"),
                "protocol_hash": run.get("protocol_hash"),
                "metadata_hash": pkg.metadata_hash,
            }
        )

    # Manifest: blind_ids only — NO arm/node leakage. Reviewer-safe.
    manifest = {
        "trial_id": trial_id,
        "schema_version": _BLINDED_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "policy": dataclasses.asdict(policy),
        "package_count": len(packages),
        "blind_ids": [p.blind_id for p in packages],
        "parse_errors": parse_errors,
        "counters": dict(COUNTERS),
    }
    manifest_path = blinded_dir / "manifest.json"
    try:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:
        _bump("blinded_package_errors")
        print(
            f"[trialbench_blinder] ERROR writing manifest {manifest_path}: {e}",
            file=sys.stderr,
        )
        # Manifest failure isn't asymmetric-leakage — packages still exist
        # and unblinding will be attempted next. We return the in-memory
        # packages so the caller can decide.

    # Unblinding map: SEPARATE file, restricted permissions. Reviewer must
    # never see this — must live in a different file so chmod 600 can be
    # applied without affecting the reviewer-readable packages.
    unblinding_map = {
        "trial_id": trial_id,
        "schema_version": _BLINDED_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "entries": unblinding_entries,
    }
    unblinding_path = blinded_dir / "unblinding_map.json"
    try:
        unblinding_path.write_text(
            json.dumps(unblinding_map, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:
        # ASYMMETRIC LEAKAGE GUARD — without unblinding we can't ever
        # correlate reviewer scores back to arms. Roll everything back.
        _bump("blinded_package_errors")
        print(
            f"[trialbench_blinder] FATAL unblinding map write failed: {e} — "
            f"deleting all blinded packages to prevent asymmetric leakage",
            file=sys.stderr,
        )
        _delete_packages(blinded_dir, written_paths)
        try:
            if manifest_path.exists():
                manifest_path.unlink()
        except (OSError, FileNotFoundError, PermissionError) as cleanup_exc:
            # ZSF: rollback-cleanup best-effort; the unblinding-map write
            # already failed, so we're already returning []. Record but do
            # not re-raise — the asymmetric-leakage guard above is the
            # primary safety net.
            _bump("blinded_package_errors")
            sys.stderr.write(
                f"WARN trialbench-blinder: manifest cleanup failed "
                f"during rollback: {cleanup_exc}\n"
            )
        return []

    # chmod 600 — best-effort; warn but don't roll back since the file is
    # local-disk and the package files themselves are still arm-blind.
    _restrict_permissions(unblinding_path)

    return packages


# ---------------------------------------------------------------------------
# CLI entry — convenience for ops, not strictly required by spec
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TrialBench blinded-package generator")
    parser.add_argument("trial_id", help="trial_id under artifacts/trialbench/")
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=None,
        help="override output dir (default: artifacts/trialbench/<trial_id>/blinded)",
    )
    parser.add_argument(
        "--keep-protocol-hash",
        action="store_true",
        help="(default) keep trial_protocol_hash in metadata fingerprint",
    )
    parser.add_argument(
        "--strip-protocol-hash",
        action="store_true",
        help="strip protocol hash from metadata fingerprint (reduces verifiability)",
    )
    args = parser.parse_args(argv)

    policy = BlindingPolicy(
        strip_protocol_hash=bool(args.strip_protocol_hash),
    )
    pkgs = materialize_packages(args.trial_id, output_dir=args.output_dir, policy=policy)
    print(f"[trialbench_blinder] wrote {len(pkgs)} packages")
    print(f"[trialbench_blinder] counters: {COUNTERS}")
    return 0 if pkgs else 1


if __name__ == "__main__":
    sys.exit(main())
