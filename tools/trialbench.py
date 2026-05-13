"""TrialBench v0 dispatcher — orchestrates A_raw vs C_governed runs across fleet.

Hardened (3s round 3-6):
- 2s spacing between dispatches (avoid daemon DoS)
- Retry once on HTTP 429 with backoff
- Pre-flight /health (non-zero exit if daemon down)
- Fleet-state.json node discovery — NO hardcoded count
- Min-2 nodes — fail loud if only 1
- Namespaced artifacts: artifacts/trialbench/<trial_id>/
- X-Trial-Protocol-Hash + X-Trial-Id headers (surgeon-review interlock per N1 INV-021)
- ZERO SILENT FAILURES — every failure logs + recorded in outcome JSON
- stdlib only (urllib, no requests)

Sibling contracts (build against, even if commits not yet visible):
  N1 memory/invariants.py: ActionProposal, evaluate(), SCHEMA_VERSION
  N3 docs/dao/trialbench-protocol.lock.json: trial_protocol_hash + task_bank.json
  N4 tools/trialbench_packet.py: build_governed_packet(task_id, arm) -> dict
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import time
import urllib.error
import os
import urllib.request

# Daemon URL: env override for cross-node runs (mac1/mac3 dispatch from mac2 etc.)
# Fallback to localhost loopback for the common case.
DAEMON = os.environ.get("TRIALBENCH_DAEMON_URL", "http://127.0.0.1:8855").rstrip("/")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEET_STATE = REPO_ROOT / "fleet-state.json"
PROTOCOL_LOCK = REPO_ROOT / "docs" / "dao" / "trialbench-protocol.lock.json"
TASK_BANK = REPO_ROOT / "docs" / "dao" / "task_bank.json"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts" / "trialbench"

NODE_FRESHNESS_SECS = 120
DISPATCH_SPACING_SECS = 2.0
RETRY_BACKOFF_SECS = 5.0
PLACEHOLDER_HASH = "PLACEHOLDER_PROTOCOL_HASH_PENDING_N3"

# Make sibling modules importable when running this file directly
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Sibling import shims — defer + degrade gracefully
# ---------------------------------------------------------------------------

def _import_invariants():
    """Try to import N1's invariants module. Return (module, available)."""
    try:
        from memory import invariants  # type: ignore
        return invariants, True
    except Exception as e:
        print(f"[trialbench] FIXME N1: memory.invariants not importable yet ({e})", file=sys.stderr)
        return None, False


def _import_packet_builder():
    """Try to import N4's build_governed_packet. Return (callable, available)."""
    try:
        from tools.trialbench_packet import build_governed_packet  # type: ignore
        return build_governed_packet, True
    except Exception as e:
        print(f"[trialbench] FIXME N4: tools.trialbench_packet not importable yet ({e}) — using stub", file=sys.stderr)

        def _stub(task: dict, arm: str) -> dict:
            prompt = task.get("task_prompt", task.get("prompt", ""))
            if arm == "C_governed":
                prompt = "[GOVERNED] " + prompt
            return {"prompt": prompt, "_stub": True, "arm": arm}

        return _stub, False


# ---------------------------------------------------------------------------
# Fleet discovery
# ---------------------------------------------------------------------------

def _strip_git_conflict_markers(text: str) -> str:
    """Best-effort strip of git merge-conflict markers — keep HEAD side.

    fleet-state.json is auto-synced and frequently has unresolved markers.
    We DO NOT silently drop content; we keep the HEAD side and log.
    """
    if "<<<<<<<" not in text:
        return text
    print("[trialbench] WARN fleet-state.json has unresolved merge conflict — keeping HEAD side", file=sys.stderr)
    out_lines = []
    skip = False
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            skip = False  # entering HEAD side, keep
            continue
        if line.startswith("======="):
            skip = True  # entering other side, drop
            continue
        if line.startswith(">>>>>>>"):
            skip = False
            continue
        if not skip:
            out_lines.append(line)
    return "\n".join(out_lines)


def discover_nodes() -> list[str]:
    """Read fleet-state.json, return online nodes (last_seen < NODE_FRESHNESS_SECS)."""
    if not FLEET_STATE.exists():
        print(f"[trialbench] ERROR fleet-state.json missing at {FLEET_STATE}", file=sys.stderr)
        return []
    raw = FLEET_STATE.read_text(encoding="utf-8")
    cleaned = _strip_git_conflict_markers(raw)
    try:
        state = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[trialbench] ERROR parsing fleet-state.json: {e}", file=sys.stderr)
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = []
    for node_id, node in (state.get("nodes") or {}).items():
        health = node.get("health") or {}
        if health.get("status") != "online":
            continue
        last_seen = health.get("last_seen")
        if not last_seen:
            continue
        try:
            ts = datetime.datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except Exception as e:
            print(f"[trialbench] WARN node={node_id} bad last_seen={last_seen!r}: {e}", file=sys.stderr)
            continue
        age = (now - ts).total_seconds()
        if age <= NODE_FRESHNESS_SECS:
            fresh.append(node_id)
        else:
            print(f"[trialbench] node={node_id} stale ({age:.0f}s > {NODE_FRESHNESS_SECS}s) — skipping", file=sys.stderr)
    return sorted(fresh)


# ---------------------------------------------------------------------------
# Health pre-flight
# ---------------------------------------------------------------------------

def health_preflight() -> bool:
    """GET /health; return True if status==ok."""
    url = f"{DAEMON}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[trialbench] ERROR /health unreachable: {e}", file=sys.stderr)
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"[trialbench] ERROR /health non-JSON body: {body[:200]!r}", file=sys.stderr)
        return False
    status = data.get("status")
    ok = status == "ok"
    if not ok:
        print(f"[trialbench] /health status={status!r} (expected 'ok')", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# Protocol hash
# ---------------------------------------------------------------------------

def load_protocol_hash() -> str:
    """Read docs/dao/trialbench-protocol.lock.json; return trial_protocol_hash.

    If N3 hasn't shipped yet, return PLACEHOLDER_HASH and warn.
    """
    if not PROTOCOL_LOCK.exists():
        print(f"[trialbench] FIXME N3: {PROTOCOL_LOCK} missing — using placeholder hash", file=sys.stderr)
        return PLACEHOLDER_HASH
    try:
        data = json.loads(PROTOCOL_LOCK.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[trialbench] ERROR parsing {PROTOCOL_LOCK}: {e} — using placeholder", file=sys.stderr)
        return PLACEHOLDER_HASH
    h = data.get("trial_protocol_hash")
    if not h:
        print(f"[trialbench] FIXME N3: {PROTOCOL_LOCK} missing 'trial_protocol_hash' — using placeholder", file=sys.stderr)
        return PLACEHOLDER_HASH
    return str(h)


# ---------------------------------------------------------------------------
# Task bank
# ---------------------------------------------------------------------------

def _placeholder_tasks(n: int) -> list[dict]:
    """Stub task bank if N3 hasn't shipped task_bank.json yet."""
    return [
        {
            "task_id": f"stub_{i:03d}",
            "task_prompt": f"Placeholder task {i}: list 3 invariants of a hardened dispatcher.",
            "_stub": True,
        }
        for i in range(n)
    ]


def load_tasks(n_per_arm: int) -> tuple[list[dict], bool]:
    """Return (tasks, is_real). If task_bank missing, return placeholder + False.

    Accepts either a top-level list or {"tasks": [...]} wrapper.
    """
    if not TASK_BANK.exists():
        print(f"[trialbench] FIXME N3: {TASK_BANK} missing — using placeholder tasks", file=sys.stderr)
        return _placeholder_tasks(n_per_arm), False
    try:
        data = json.loads(TASK_BANK.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[trialbench] ERROR parsing {TASK_BANK}: {e} — using placeholder", file=sys.stderr)
        return _placeholder_tasks(n_per_arm), False
    if isinstance(data, dict):
        tasks = data.get("tasks")
    else:
        tasks = data
    if not isinstance(tasks, list) or not tasks:
        print(f"[trialbench] ERROR {TASK_BANK} has no usable tasks — using placeholder", file=sys.stderr)
        return _placeholder_tasks(n_per_arm), False
    if len(tasks) < n_per_arm:
        print(f"[trialbench] WARN task_bank has {len(tasks)} tasks, requested {n_per_arm} — using all", file=sys.stderr)
        return tasks, True
    return tasks[:n_per_arm], True


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _build_payload(task: dict, arm: str, build_packet, packet_real: bool) -> dict:
    """Build the dispatch payload for a given task+arm."""
    if arm == "A_raw":
        # Arm A: raw task only (no governance wrapping)
        return {"prompt": task.get("task_prompt", task.get("prompt", "")), "arm": arm}
    # Arm C: governed packet from N4 (or stub)
    return build_packet(task, arm)


def _extract_prompt_text(payload: dict) -> str:
    """Pull plain-text prompt from a trialbench payload (raw or governed packet).

    Both arms put their composed prompt under "prompt"; governed packets may
    also carry richer fields, but the bridge only consumes plain text via
    Anthropic Messages content. ZSF: never raise — fall back to JSON dump
    so the bridge still gets *something* deterministic and the failure is
    visible in the recorded request_payload.
    """
    if isinstance(payload, dict):
        p = payload.get("prompt")
        if isinstance(p, str) and p:
            return p
    # Last-resort: serialize the whole payload so the bug is observable in
    # input_tokens (size > 1) instead of silently sending an empty string.
    return json.dumps(payload, sort_keys=True)


def dispatch_run(
    node: str,
    task: dict,
    arm: str,
    trial_id: str,
    protocol_hash: str,
    build_packet,
    packet_real: bool,
    dry_run: bool = False,
) -> dict:
    """Send one dispatch. Return outcome dict (always — never raise)."""
    payload = _build_payload(task, arm, build_packet, packet_real)
    prompt_text = _extract_prompt_text(payload)
    # Anthropic Messages API shape — bridge /v1/messages reads body.messages[].content
    # and body.system. Trialbench-specific metadata (trial_id, arm, task_id,
    # protocol_hash) rides in X-Trial-* headers; the bridge currently ignores
    # unknown body keys, but we keep the body strictly Anthropic-spec to avoid
    # breaking on a future bridge that validates schema.
    # Model alias: bridge resolves "claude-sonnet-4-6" via /v1/models alias map.
    body_obj = {
        "model": os.environ.get("TRIALBENCH_MODEL", "claude-sonnet-4-6"),
        "max_tokens": int(os.environ.get("TRIALBENCH_MAX_TOKENS", "1024")),
        "messages": [
            {"role": "user", "content": prompt_text},
        ],
    }
    body_bytes = json.dumps(body_obj).encode("utf-8")

    outcome: dict = {
        "task_id": task.get("task_id", "unknown"),
        "node": node,
        "arm": arm,
        "trial_id": trial_id,
        "protocol_hash": protocol_hash,
        "packet_stub": not packet_real,
        "request_payload": payload,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "latency_ms": None,
        "exit_code": None,
        "http_status": None,
        "content": None,
        "error": None,
    }

    if dry_run:
        outcome["exit_code"] = 0
        outcome["http_status"] = "DRY_RUN"
        outcome["content"] = (
            f"[DRY_RUN] would POST to {DAEMON}/v1/messages — "
            f"node={node} arm={arm} task={outcome['task_id']}"
        )
        outcome["latency_ms"] = 0
        outcome["finished_at"] = outcome["started_at"]
        outcome["attempts"] = 0
        return outcome

    url = f"{DAEMON}/v1/messages"  # /dispatch may not exist yet — fall back to /v1/messages
    headers = {
        "Content-Type": "application/json",
        "X-Trial-Protocol-Hash": protocol_hash,
        "X-Trial-Id": trial_id,
        "X-Trial-Arm": arm,
    }

    attempts = 0
    max_attempts = 2  # original + one retry on 429 OR transient URLError
    t0 = time.monotonic()
    while attempts < max_attempts:
        attempts += 1
        req = urllib.request.Request(url, data=body_bytes, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                outcome["http_status"] = resp.status
                outcome["content"] = resp_body[:4000]
                outcome["exit_code"] = 0 if 200 <= resp.status < 300 else 1
                break
        except urllib.error.HTTPError as e:
            outcome["http_status"] = e.code
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            if e.code == 429 and attempts < max_attempts:
                print(
                    f"[trialbench] HTTP 429 node={node} task={outcome['task_id']} — "
                    f"backing off {RETRY_BACKOFF_SECS}s",
                    file=sys.stderr,
                )
                time.sleep(RETRY_BACKOFF_SECS)
                continue
            outcome["error"] = f"HTTPError {e.code}: {err_body}"
            outcome["exit_code"] = 2
            break
        except urllib.error.URLError as e:
            # Transient network blips (DNS hiccup, daemon mid-restart): retry once.
            if attempts < max_attempts:
                print(
                    f"[trialbench] URLError node={node} task={outcome['task_id']} — "
                    f"retry after {RETRY_BACKOFF_SECS}s ({e})",
                    file=sys.stderr,
                )
                time.sleep(RETRY_BACKOFF_SECS)
                continue
            outcome["error"] = f"URLError: {e}"
            outcome["exit_code"] = 3
            break
        except Exception as e:
            outcome["error"] = f"{type(e).__name__}: {e}"
            outcome["exit_code"] = 4
            break

    outcome["latency_ms"] = int((time.monotonic() - t0) * 1000)
    outcome["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    outcome["attempts"] = attempts
    return outcome


# ---------------------------------------------------------------------------
# Main trial loop
# ---------------------------------------------------------------------------

def run_trial(
    trial_id: str | None = None,
    tasks_per_arm: int = 5,
    dry_run: bool = False,
    allow_single_node: bool = False,
) -> int:
    """Run one full trial. Return process exit code.

    allow_single_node=True permits v0 smoke runs when only the local fleet
    daemon is alive. Logs a warning that crossover validity is reduced —
    node-effect can't be separated from arm-effect with a single node.
    """
    if trial_id is None:
        trial_id = "ctxdna_trialbench_" + datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")

    print(f"[trialbench] trial_id={trial_id} tasks_per_arm={tasks_per_arm} dry_run={dry_run}")

    # Sibling imports
    invariants_mod, invariants_real = _import_invariants()
    schema_version = (
        getattr(invariants_mod, "SCHEMA_VERSION", "UNKNOWN") if invariants_real else "UNKNOWN"
    )
    print(f"[trialbench] invariants schema_version={schema_version} real={invariants_real}")

    build_packet, packet_real = _import_packet_builder()

    # Protocol hash
    protocol_hash = load_protocol_hash()
    print(f"[trialbench] protocol_hash={protocol_hash}")

    # Pre-flight health (skipped on dry-run since daemon may be down for testing)
    if not dry_run:
        if not health_preflight():
            print("[trialbench] ABORT — daemon /health failed pre-flight", file=sys.stderr)
            return 10
        print("[trialbench] /health ok")
    else:
        print("[trialbench] dry-run: skipping /health pre-flight")

    # Discover nodes
    nodes = discover_nodes()
    min_nodes_required = 2 if not allow_single_node else 1
    if len(nodes) < min_nodes_required:
        print(f"[trialbench] only {len(nodes)} online node(s): {nodes}", file=sys.stderr)
        if not dry_run:
            print(
                f"[trialbench] ABORT — need >={min_nodes_required} online nodes "
                f"(use --allow-single-node for v0 smoke)",
                file=sys.stderr,
            )
            return 11
        # Dry-run: synthesize placeholders so we exercise the full code path
        synthetic = ["mac1", "mac2", "mac3"]
        for n in synthetic:
            if n not in nodes:
                nodes.append(n)
            if len(nodes) >= 2:
                break
        print(f"[trialbench] dry-run: padded nodes to {nodes}", file=sys.stderr)
    elif allow_single_node and len(nodes) == 1:
        print(
            f"[trialbench] WARN: single-node mode — crossover validity reduced "
            f"(node-effect not separable from arm-effect). v0 smoke only.",
            file=sys.stderr,
        )
    print(f"[trialbench] nodes ({len(nodes)}): {nodes}")

    # Load tasks
    tasks, tasks_real = load_tasks(tasks_per_arm)
    print(f"[trialbench] tasks loaded: count={len(tasks)} real={tasks_real}")

    # Artifacts dir
    out_dir = ARTIFACTS_ROOT / trial_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[trialbench] artifacts -> {out_dir}")

    # Manifest header
    manifest = {
        "trial_id": trial_id,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "protocol_hash": protocol_hash,
        "invariants_schema_version": schema_version,
        "invariants_real": invariants_real,
        "packet_builder_real": packet_real,
        "tasks_real": tasks_real,
        "nodes": nodes,
        "tasks_per_arm": tasks_per_arm,
        "daemon": DAEMON,
        "dry_run": dry_run,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Dispatch matrix: each task x each arm x round-robin node
    arms = ["A_raw", "C_governed"]
    outcomes: list[dict] = []
    run_idx = 0
    for task in tasks:
        for arm in arms:
            node = nodes[run_idx % len(nodes)]
            print(
                f"[trialbench] run #{run_idx:03d} "
                f"task={task.get('task_id')} arm={arm} node={node}"
            )
            outcome = dispatch_run(
                node=node,
                task=task,
                arm=arm,
                trial_id=trial_id,
                protocol_hash=protocol_hash,
                build_packet=build_packet,
                packet_real=packet_real,
                dry_run=dry_run,
            )
            run_path = out_dir / f"run_{run_idx:03d}.json"
            try:
                run_path.write_text(
                    json.dumps(outcome, indent=2, default=str), encoding="utf-8"
                )
            except Exception as e:
                print(f"[trialbench] ERROR writing {run_path}: {e}", file=sys.stderr)
            outcomes.append(outcome)
            run_idx += 1
            # Spacing — skip on dry-run
            if not dry_run:
                time.sleep(DISPATCH_SPACING_SECS)

    # Summary
    ok = sum(1 for o in outcomes if o.get("exit_code") == 0)
    failed = len(outcomes) - ok
    fixmes = [
        None if invariants_real else "N1: memory.invariants not yet importable",
        None if packet_real else "N4: tools.trialbench_packet not yet importable",
        None if PROTOCOL_LOCK.exists() else "N3: docs/dao/trialbench-protocol.lock.json missing",
        None if TASK_BANK.exists() else "N3: docs/dao/task_bank.json missing",
    ]
    summary = {
        "trial_id": trial_id,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "total_runs": len(outcomes),
        "ok": ok,
        "failed": failed,
        "by_arm": {
            arm: {
                "ok": sum(1 for o in outcomes if o["arm"] == arm and o.get("exit_code") == 0),
                "total": sum(1 for o in outcomes if o["arm"] == arm),
            }
            for arm in arms
        },
        "fixmes": [f for f in fixmes if f],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n[trialbench] === SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"[trialbench] artifacts: {out_dir}")
    return 0 if failed == 0 else 12


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrialBench v0 dispatcher")
    parser.add_argument("--tasks-per-arm", type=int, default=5)
    parser.add_argument("--trial-id", type=str, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No network calls; print + write stub artifacts",
    )
    parser.add_argument(
        "--allow-single-node",
        action="store_true",
        help="v0 smoke: permit single-node trial (logs reduced-validity WARN)",
    )
    args = parser.parse_args(argv)
    return run_trial(
        trial_id=args.trial_id,
        tasks_per_arm=args.tasks_per_arm,
        dry_run=args.dry_run,
        allow_single_node=args.allow_single_node,
    )


if __name__ == "__main__":
    sys.exit(main())
