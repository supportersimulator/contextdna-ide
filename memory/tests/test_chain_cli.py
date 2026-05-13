"""Tests for chain_cli — basic CLI for testing chain orchestration."""
import subprocess
import sys
import os
from pathlib import Path
import pytest

REPO = str(Path(__file__).resolve().parent.parent.parent)
CLI = [sys.executable, "memory/chain_cli.py"]
ENV = {**os.environ, "PYTHONPATH": REPO}


def test_cli_presets():
    result = subprocess.run(CLI + ["presets"], capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr
    assert "full-3s" in result.stdout
    assert "lightweight" in result.stdout
    assert "plan-review" in result.stdout
    assert "evidence-dive" in result.stdout


def test_cli_suggest_known_trigger():
    result = subprocess.run(CLI + ["suggest", "--trigger", "plan_file_detected"],
                            capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr
    assert "plan-review" in result.stdout


def test_cli_suggest_unknown_trigger():
    result = subprocess.run(CLI + ["suggest", "--trigger", "nope"],
                            capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr
    assert "No suggestion" in result.stdout


def test_cli_run_lightweight():
    result = subprocess.run(CLI + ["run", "lightweight", "--topic", "test run"],
                            capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr
    assert "lightweight" in result.stdout.lower() or "chain" in result.stdout.lower()


def test_cli_run_unknown_preset():
    result = subprocess.run(CLI + ["run", "nonexistent", "--topic", "test"],
                            capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode != 0 or "Unknown" in result.stdout or "error" in result.stdout.lower()


def test_cli_history():
    result = subprocess.run(CLI + ["history"], capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr


def test_cli_telemetry():
    result = subprocess.run(CLI + ["telemetry"], capture_output=True, text=True, cwd=REPO, env=ENV)
    assert result.returncode == 0, result.stderr
