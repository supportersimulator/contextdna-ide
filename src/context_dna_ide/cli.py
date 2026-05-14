"""ContextDNA IDE command-line interface.

Entry point registered in pyproject.toml as:
    context-dna-ide = "context_dna_ide.cli:main"

Commands (minimum viable surface for v0.1.0):
    context-dna-ide --version
        Print the package version and exit 0.

    context-dna-ide health
        Inspect docker compose stack + daemon health endpoint.
        Prints a structured table; exit 0 if everything is up, 1 otherwise.

    context-dna-ide consult "<topic>"
        Delegate to the 3-surgeons consult flow. Tries (in order):
          1. mothership root memory/surgery_bridge.py:cardio_review (direct import)
          2. mothership root scripts/3s-brainstorm.sh wrapper
          3. helpful diagnostic if neither is reachable
        Honors SURGERY_BRIDGE_MODE / MOTHERSHIP_ROOT env vars.

Design notes:
    - This module deliberately uses argparse (stdlib) not click, to keep
      `context-dna-ide --version` answerable even before optional deps install.
    - All subprocess calls are timeout-bounded (ZSF) and return structured
      exit codes — never silently swallow non-zero.
    - Heavy logic stays in the mothership tree (memory/, scripts/). This
      CLI is a 'thin seam' on purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__

# Default health endpoint (matches CLAUDE.md fleet daemon contract).
DEFAULT_DAEMON_HEALTH_URL: str = os.environ.get(
    "CONTEXTDNA_DAEMON_HEALTH_URL",
    "http://127.0.0.1:8855/health",
)

# Bound subprocesses so a hung docker/curl never wedges the CLI.
SUBPROCESS_TIMEOUT_S: int = int(os.environ.get("CONTEXTDNA_CLI_TIMEOUT", "15"))


# ---------------------------------------------------------------------------
# Mothership-root resolution
# ---------------------------------------------------------------------------
def _find_mothership_root() -> Optional[Path]:
    """Locate the mothership repo root.

    Resolution order:
        1. $MOTHERSHIP_ROOT (explicit override)
        2. The directory two levels above this file IF it contains pyproject.toml
           AND docker-compose.lite.yml (source-tree / editable install)
        3. Current working directory if it looks like a mothership checkout
        4. None — the CLI still runs (health/version), but consult features
           that need surgery_bridge.py will report 'mothership not found'.

    Returns None on failure rather than raising — `context-dna-ide --version`
    must work even from a wheel installed outside the mothership tree.
    """
    override = os.environ.get("MOTHERSHIP_ROOT")
    if override:
        p = Path(override).expanduser().resolve()
        if (p / "pyproject.toml").is_file():
            return p

    # src/context_dna_ide/cli.py → parents: cli.py, context_dna_ide, src, <repo>
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent
    if (candidate / "pyproject.toml").is_file() and (
        candidate / "docker-compose.lite.yml"
    ).is_file():
        return candidate

    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() and (cwd / "docker-compose.lite.yml").is_file():
        return cwd

    return None


# ---------------------------------------------------------------------------
# health command
# ---------------------------------------------------------------------------
def _run_docker_compose_ps(repo_root: Optional[Path]) -> Dict[str, Any]:
    """Run `docker compose ps --format json` and return a structured result."""
    if shutil.which("docker") is None:
        return {"ok": False, "error": "docker CLI not on PATH", "services": []}

    cmd: List[str] = ["docker", "compose"]
    cwd: Optional[str] = None
    if repo_root is not None:
        # Prefer the lite stack; user can override by running docker compose
        # against heavy explicitly. Both yamls live at the repo root.
        compose_file = repo_root / "docker-compose.lite.yml"
        if compose_file.is_file():
            cmd += ["-f", str(compose_file)]
        cwd = str(repo_root)
    cmd += ["ps", "--format", "json"]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker compose ps timed out after {SUBPROCESS_TIMEOUT_S}s", "services": []}
    except OSError as exc:
        return {"ok": False, "error": f"docker compose ps failed to spawn: {exc}", "services": []}

    if completed.returncode != 0:
        return {
            "ok": False,
            "error": completed.stderr.strip() or f"exit {completed.returncode}",
            "services": [],
        }

    # `docker compose ps --format json` can emit either a JSON array (newer)
    # or newline-delimited JSON objects (older). Handle both.
    out = completed.stdout.strip()
    services: List[Dict[str, Any]] = []
    if not out:
        return {"ok": True, "error": None, "services": []}

    try:
        parsed = json.loads(out)
        if isinstance(parsed, list):
            services = parsed
        elif isinstance(parsed, dict):
            services = [parsed]
    except json.JSONDecodeError:
        # Fall back to NDJSON.
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                # Surface, don't swallow — ZSF.
                return {
                    "ok": False,
                    "error": f"unparseable docker compose ps output line: {line!r}",
                    "services": services,
                }

    return {"ok": True, "error": None, "services": services}


def _probe_daemon(url: str) -> Dict[str, Any]:
    """HTTP-probe the fleet daemon /health endpoint.

    Uses stdlib urllib so we don't pull in httpx for `health` alone.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "context-dna-ide/health"})
    try:
        with urllib.request.urlopen(req, timeout=SUBPROCESS_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": body[:400]}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": None, "body": f"unreachable: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001 — surface, don't swallow (ZSF)
        return {"ok": False, "status": None, "body": f"probe error: {exc!r}"}


def cmd_health(args: argparse.Namespace) -> int:
    """`context-dna-ide health` — structured docker + daemon status."""
    repo_root = _find_mothership_root()
    compose = _run_docker_compose_ps(repo_root)
    daemon = _probe_daemon(args.daemon_url)

    if args.json:
        payload = {
            "mothership_root": str(repo_root) if repo_root else None,
            "docker_compose": compose,
            "daemon": {"url": args.daemon_url, **daemon},
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        # Rich-free table — keeps `health` working before optional deps install.
        print("ContextDNA IDE — health")
        print("-" * 60)
        print(f"mothership root: {repo_root if repo_root else '(not found — wheel-only install)'}")
        print()
        print("[docker compose]")
        if compose["ok"]:
            if not compose["services"]:
                print("  (no services running — try: docker compose -f docker-compose.lite.yml up -d)")
            else:
                for svc in compose["services"]:
                    name = svc.get("Service") or svc.get("Name") or "?"
                    state = svc.get("State") or svc.get("Status") or "?"
                    health = svc.get("Health") or ""
                    suffix = f" [{health}]" if health else ""
                    print(f"  - {name}: {state}{suffix}")
        else:
            print(f"  ERROR: {compose['error']}")
        print()
        print("[fleet daemon]")
        print(f"  url: {args.daemon_url}")
        if daemon["ok"]:
            print(f"  status: {daemon['status']} OK")
        else:
            print(f"  status: {daemon['status']} FAIL — {daemon['body']}")

    # Exit code: 0 only if BOTH docker layer reachable AND daemon reachable.
    # We don't require services to be running (clean clone may not have `up`d
    # yet) — but we DO require docker itself to respond.
    overall_ok = compose["ok"] and daemon["ok"]
    return 0 if overall_ok else 1


# ---------------------------------------------------------------------------
# consult command
# ---------------------------------------------------------------------------
def _consult_via_surgery_bridge(repo_root: Path, topic: str) -> Dict[str, Any]:
    """Try to import memory.surgery_bridge and call cardio_review."""
    sys_path_added = False
    try:
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
            sys_path_added = True
        try:
            from memory.surgery_bridge import cardio_review  # type: ignore
        except Exception as exc:  # noqa: BLE001 — surface import failure
            return {"ok": False, "path": "direct", "error": f"import failed: {exc!r}"}
        try:
            result = cardio_review(topic)
        except Exception as exc:  # noqa: BLE001 — surface call failure
            return {"ok": False, "path": "direct", "error": f"cardio_review failed: {exc!r}"}
        return {"ok": True, "path": "direct", "result": result}
    finally:
        if sys_path_added:
            try:
                sys.path.remove(str(repo_root))
            except ValueError:
                pass


def _consult_via_brainstorm_script(repo_root: Path, topic: str) -> Dict[str, Any]:
    """Fall back to the 3s-brainstorm.sh autonomous wrapper."""
    script = repo_root / "scripts" / "3s-brainstorm.sh"
    if not script.is_file():
        return {"ok": False, "path": "subprocess", "error": f"{script} not found"}

    cmd = ["bash", str(script), "--cost-cap", "0.05", topic]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # brainstorm budget; 3s-brainstorm enforces its own cap too
            cwd=str(repo_root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "path": "subprocess", "error": "3s-brainstorm.sh timed out after 300s"}
    except OSError as exc:
        return {"ok": False, "path": "subprocess", "error": f"spawn failed: {exc}"}

    if completed.returncode != 0:
        return {
            "ok": False,
            "path": "subprocess",
            "error": completed.stderr.strip() or f"exit {completed.returncode}",
            "stdout": completed.stdout[-2000:],
        }
    return {"ok": True, "path": "subprocess", "result": completed.stdout[-2000:]}


def cmd_consult(args: argparse.Namespace) -> int:
    """`context-dna-ide consult "<topic>"` — 3-surgeon cross-examination."""
    topic = args.topic.strip()
    if not topic:
        print("error: consult topic is empty", file=sys.stderr)
        return 2
    if len(topic.split()) <= 5:
        # Match the global "5-word gate" called out in CLAUDE.md / webhook docs.
        print(
            "warning: topic is <=5 words; brainstorm may produce generic output. "
            "Consider expanding the prompt before retrying.",
            file=sys.stderr,
        )

    repo_root = _find_mothership_root()
    if repo_root is None:
        print(
            "error: mothership root not found.\n"
            "  set MOTHERSHIP_ROOT=/path/to/contextdna-ide and retry, or run\n"
            "  `context-dna-ide consult` from inside a fresh clone.",
            file=sys.stderr,
        )
        return 3

    # Strategy: try the in-process bridge first (fast, no subprocess), then
    # the brainstorm script. SURGERY_BRIDGE_MODE=subprocess forces the script.
    mode = os.environ.get("SURGERY_BRIDGE_MODE", "auto").lower()

    attempts: List[Dict[str, Any]] = []
    if mode != "subprocess":
        attempts.append(_consult_via_surgery_bridge(repo_root, topic))
        if attempts[-1]["ok"]:
            _emit_consult_result(attempts[-1], json_mode=args.json)
            return 0

    attempts.append(_consult_via_brainstorm_script(repo_root, topic))
    if attempts[-1]["ok"]:
        _emit_consult_result(attempts[-1], json_mode=args.json)
        return 0

    # Both failed — emit structured diagnostic, exit non-zero.
    if args.json:
        print(json.dumps({"ok": False, "attempts": attempts}, indent=2, default=str))
    else:
        print("consult failed on every route:", file=sys.stderr)
        for att in attempts:
            print(f"  - path={att.get('path')}: {att.get('error')}", file=sys.stderr)
    return 1


def _emit_consult_result(attempt: Dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(attempt, indent=2, default=str))
        return
    print(f"[consult ok via {attempt['path']}]")
    result = attempt.get("result")
    if isinstance(result, dict):
        # surgery_bridge returns {"status", "output", "path"} — pretty-print.
        print(json.dumps(result, indent=2, default=str))
    elif result is None:
        print("(no output)")
    else:
        print(str(result))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context-dna-ide",
        description="ContextDNA IDE — mothership CLI (health, consult, ...).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"context-dna-ide {__version__}",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_health = sub.add_parser(
        "health",
        help="Inspect docker compose stack + fleet daemon health.",
    )
    p_health.add_argument(
        "--daemon-url",
        default=DEFAULT_DAEMON_HEALTH_URL,
        help=f"Fleet daemon health URL (default: {DEFAULT_DAEMON_HEALTH_URL})",
    )
    p_health.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_health.set_defaults(func=cmd_health)

    p_consult = sub.add_parser(
        "consult",
        help='Run a 3-surgeon consult for "<topic>".',
    )
    p_consult.add_argument("topic", help="The topic / question to cross-examine.")
    p_consult.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p_consult.set_defaults(func=cmd_consult)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint. Returns an exit code (never raises to the shell)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0

    try:
        return int(func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — ZSF: surface, don't swallow
        print(f"context-dna-ide: unexpected error: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
