"""tests/test_cli.py — parse-only smoke tests for the v0.1.0 CLI surface.

Validates that the 3 commands declared in pyproject.toml's
[project.scripts] entry actually parse without ImportError and produce
the expected exit codes for `--help` / `--version` / unknown-args.

These tests intentionally do NOT spin up docker, hit the live daemon,
or call surgery_bridge — those are integration concerns. Smoke tests
here protect the seam A4 found broken: "wheel installs but CLI raises
ImportError on first invocation."
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = str(REPO_ROOT / "src")


def _cli_env() -> dict[str, str]:
    """Run the CLI via python -m so we don't depend on the entry point
    script being on PATH (works in both editable + wheel-install modes)."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{SRC_PATH}{os.pathsep}{existing}" if existing else SRC_PATH
    )
    return env


def _run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "context_dna_ide.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_cli_env(),
        cwd=str(REPO_ROOT),
    )


def test_module_importable() -> None:
    """The package must import without side effects (ZSF-friendly)."""
    proc = subprocess.run(
        [sys.executable, "-c", "import context_dna_ide; print(context_dna_ide.__version__)"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_cli_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()  # non-empty version string


def test_cli_version_exits_zero() -> None:
    proc = _run("--version")
    assert proc.returncode == 0, proc.stderr
    assert "context-dna-ide" in proc.stdout.lower()


def test_cli_help_exits_zero() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "health" in proc.stdout
    assert "consult" in proc.stdout


def test_cli_no_args_prints_help() -> None:
    """Bare `context-dna-ide` should not crash — it prints help and exits 0."""
    proc = _run()
    assert proc.returncode == 0


def test_health_subcommand_parses() -> None:
    """`health --help` proves the subparser is wired correctly."""
    proc = _run("health", "--help")
    assert proc.returncode == 0
    assert "--daemon-url" in proc.stdout
    assert "--json" in proc.stdout


def test_consult_subcommand_parses() -> None:
    proc = _run("consult", "--help")
    assert proc.returncode == 0
    assert "topic" in proc.stdout.lower()


def test_consult_requires_topic() -> None:
    """argparse should reject a bare `consult` with exit 2 (usage error)."""
    proc = _run("consult")
    assert proc.returncode == 2
    assert "topic" in proc.stderr.lower() or "topic" in proc.stdout.lower()


def test_consult_rejects_empty_topic() -> None:
    """An empty-string topic should exit non-zero with a clear message."""
    proc = _run("consult", "")
    assert proc.returncode != 0


def test_health_emits_json_when_requested() -> None:
    """JSON mode should produce parseable output even when daemon is down."""
    import json

    # We deliberately do NOT require the daemon to be up here — the test
    # is that the JSON envelope is well-formed regardless of state.
    proc = _run("health", "--json", "--daemon-url", "http://127.0.0.1:1/", timeout=30)
    # exit code may be 0 or 1 depending on local docker state; we only
    # care that stdout is valid JSON.
    payload = json.loads(proc.stdout)
    assert "docker_compose" in payload
    assert "daemon" in payload


@pytest.mark.parametrize(
    "argv",
    [
        ["--not-a-real-flag"],
        ["nonsense-subcommand"],
    ],
)
def test_cli_rejects_unknown_input(argv: list[str]) -> None:
    proc = _run(*argv)
    assert proc.returncode != 0
