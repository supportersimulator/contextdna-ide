"""
RACE V4 — Tests for memory/superset_context_loader.

Verifies:
  * Bridge is lazy — importing the loader does NOT import multifleet.
  * load_superset_context calls get_workspace_details + list_tasks.
  * Empty / shaped output when bridge is unavailable.
  * Circuit breaker opens after N consecutive failures, blocks calls,
    half-opens after cooldown, and re-closes on success.
  * SUPERSET_CTX_ENABLED=0 disables the loader entirely.
  * launch_agent_session wires through the bridge's launch_agent (the
    `start_agent_session` MCP tool) and reports breaker state.
  * Exceptions in the bridge are caught and recorded — never re-raised.
  * Wall-clock budget is observed (no hangs even on slow bridges).
"""

from __future__ import annotations

import importlib
import sys
import time

import pytest


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeBridge:
    """Records calls; configurable per-method outcome."""

    def __init__(self):
        self.workspace_calls = []
        self.task_calls = []
        self.agent_calls = []
        # Default behaviours — tests override.
        self.workspace_result = {"id": "ws-1", "branch": "main", "tabs": []}
        self.tasks_result = [
            {"id": "t1", "title": "do thing", "status": "open"},
            {"id": "t2", "title": "do other", "status": "open"},
        ]
        self.agent_result = {"sessionId": "sess-1"}
        self.workspace_raises = None
        self.tasks_raises = None
        self.agent_raises = None
        self.delay = 0.0

    def get_workspace_details(self, workspace_id, device_id=None):
        self.workspace_calls.append((workspace_id, device_id))
        if self.delay:
            time.sleep(self.delay)
        if self.workspace_raises:
            raise self.workspace_raises
        return self.workspace_result

    def list_tasks(self, status=None):
        self.task_calls.append(status)
        if self.delay:
            time.sleep(self.delay)
        if self.tasks_raises:
            raise self.tasks_raises
        return self.tasks_result

    def launch_agent(self, workspace_id, task_id=None, agent="claude", device_id=None):
        self.agent_calls.append((workspace_id, task_id, agent, device_id))
        if self.agent_raises:
            raise self.agent_raises
        return self.agent_result


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def loader(monkeypatch):
    """Reload the loader module fresh for each test, with sane env defaults."""
    # Force-enable + reset breaker + tight budget to keep tests fast.
    monkeypatch.setenv("SUPERSET_CTX_ENABLED", "1")
    monkeypatch.setenv("SUPERSET_CTX_BUDGET_SECONDS", "1.0")
    monkeypatch.setenv("SUPERSET_CTX_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("SUPERSET_CTX_BREAKER_COOLDOWN_SECONDS", "60.0")
    # Re-import so module-level constants pick up the env values.
    if "memory.superset_context_loader" in sys.modules:
        del sys.modules["memory.superset_context_loader"]
    mod = importlib.import_module("memory.superset_context_loader")
    mod.reset_breaker()
    yield mod
    # Cleanup — clear any injected fake.
    mod._set_bridge_for_tests(None)
    mod.reset_breaker()


@pytest.fixture
def fake_bridge(loader):
    """Inject a fake bridge so the loader never tries to reach Superset."""
    fb = _FakeBridge()
    loader._set_bridge_for_tests(fb)
    return fb


# ── Lazy-import guarantee ─────────────────────────────────────────────────────


def test_importing_loader_does_not_import_multifleet():
    """The whole point of lazy import: keep webhook startup cheap.

    We verify the source code rather than manipulating sys.modules — popping
    multifleet.superset_bridge at runtime would break other tests that
    imported SupersetBridge into their module namespace before us.
    """
    import inspect

    import memory.superset_context_loader as loader_mod
    src = inspect.getsource(loader_mod)
    # The only mention of multifleet.superset_bridge must be inside the
    # lazy-import body of _get_bridge().
    assert "from multifleet.superset_bridge" in src, "lazy import expected"
    # No top-level (unindented) import of multifleet.
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("import multifleet", "from multifleet")):
            assert line != stripped, (
                "multifleet must only be imported lazily inside a function"
            )


# ── Happy path ────────────────────────────────────────────────────────────────


def test_load_workspace_and_tasks_happy_path(loader, fake_bridge):
    out = loader.load_superset_context(workspace_id="ws-1", task_status="open")
    assert out["available"] is True
    assert out["workspace"] == fake_bridge.workspace_result
    assert out["tasks"] == fake_bridge.tasks_result
    assert out["errors"] == []
    assert fake_bridge.workspace_calls == [("ws-1", None)]
    assert fake_bridge.task_calls == ["open"]
    assert out["breaker"]["state"] == "closed"
    assert out["breaker"]["successes"] == 1


def test_no_workspace_id_skips_workspace_call_but_loads_tasks(loader, fake_bridge):
    out = loader.load_superset_context()
    assert out["available"] is True
    assert out["workspace"] == {}
    assert fake_bridge.workspace_calls == []
    assert out["tasks"] == fake_bridge.tasks_result


def test_task_limit_caps_tasks_returned(loader, fake_bridge):
    fake_bridge.tasks_result = [{"id": f"t{i}"} for i in range(10)]
    out = loader.load_superset_context(task_limit=3)
    assert len(out["tasks"]) == 3


def test_task_limit_zero_returns_no_tasks(loader, fake_bridge):
    out = loader.load_superset_context(task_limit=0)
    # Successful call (Superset replied) but caller asked for nothing.
    assert out["available"] is True
    assert out["tasks"] == []


# ── Failure / unavailability paths ────────────────────────────────────────────


def test_disabled_via_env_short_circuits(loader, fake_bridge, monkeypatch):
    monkeypatch.setattr(loader, "ENABLED", False)
    out = loader.load_superset_context(workspace_id="ws-1")
    assert out["available"] is False
    assert "disabled_via_env" in out["errors"]
    assert fake_bridge.workspace_calls == []
    assert fake_bridge.task_calls == []


def test_bridge_unavailable_records_failure(loader):
    loader._set_bridge_for_tests(None)
    # Force the lazy importer to also fail by overriding it.
    def _none():
        return None
    loader._get_bridge = _none  # type: ignore
    out = loader.load_superset_context(workspace_id="ws-1")
    assert out["available"] is False
    assert "bridge_unavailable" in out["errors"]
    assert out["breaker"]["failures"] == 1


def test_workspace_unavailable_marker_recorded(loader, fake_bridge):
    fake_bridge.workspace_result = {"_available": False, "error": "HTTP 503"}
    out = loader.load_superset_context(workspace_id="ws-1")
    # Tasks still returned successfully → overall available True.
    assert out["available"] is True
    assert any("workspace_details_unavailable" in e for e in out["errors"])
    assert "HTTP 503" in " ".join(out["errors"])


def test_workspace_exception_caught(loader, fake_bridge):
    fake_bridge.workspace_raises = RuntimeError("boom")
    out = loader.load_superset_context(workspace_id="ws-1", task_status="open")
    assert any("workspace_details_exception" in e for e in out["errors"])
    # Tasks still attempted.
    assert fake_bridge.task_calls == ["open"]


def test_list_tasks_exception_caught(loader, fake_bridge):
    fake_bridge.tasks_raises = RuntimeError("kaboom")
    out = loader.load_superset_context(workspace_id="ws-1")
    assert any("list_tasks_exception" in e for e in out["errors"])
    assert out["available"] is True  # workspace succeeded


def test_unexpected_task_type_recorded(loader, fake_bridge):
    fake_bridge.tasks_result = "not a list"
    out = loader.load_superset_context()
    assert any("list_tasks_unexpected_type" in e for e in out["errors"])
    assert out["available"] is False


# ── Circuit breaker ──────────────────────────────────────────────────────────


def test_breaker_opens_after_threshold_consecutive_failures(loader, fake_bridge):
    fake_bridge.tasks_raises = RuntimeError("down")
    fake_bridge.workspace_raises = RuntimeError("down")
    # Threshold from fixture = 3.
    for _ in range(3):
        out = loader.load_superset_context(workspace_id="ws-1")
        assert out["available"] is False
    state = loader.get_breaker_state()
    assert state["state"] == "open"
    assert state["opens"] == 1


def test_breaker_blocks_calls_when_open(loader, fake_bridge):
    fake_bridge.tasks_raises = RuntimeError("down")
    fake_bridge.workspace_raises = RuntimeError("down")
    for _ in range(3):
        loader.load_superset_context(workspace_id="ws-1")
    pre_calls = len(fake_bridge.task_calls)
    out = loader.load_superset_context(workspace_id="ws-1")
    assert out["available"] is False
    assert "breaker_open" in out["errors"]
    # Bridge should NOT have been called again.
    assert len(fake_bridge.task_calls) == pre_calls


def test_breaker_half_opens_after_cooldown_and_closes_on_success(
    loader, fake_bridge, monkeypatch
):
    fake_bridge.tasks_raises = RuntimeError("down")
    fake_bridge.workspace_raises = RuntimeError("down")
    for _ in range(3):
        loader.load_superset_context(workspace_id="ws-1")
    assert loader.get_breaker_state()["state"] == "open"

    # Fast-forward by mutating the breaker's cooldown to 0.
    loader._breaker._cooldown = 0.0
    # And remove the failures.
    fake_bridge.tasks_raises = None
    fake_bridge.workspace_raises = None
    out = loader.load_superset_context(workspace_id="ws-1")
    assert out["available"] is True
    state = loader.get_breaker_state()
    assert state["state"] == "closed"
    assert state["consec_failures"] == 0


def test_success_resets_consecutive_failure_counter(loader, fake_bridge):
    fake_bridge.tasks_raises = RuntimeError("down")
    loader.load_superset_context()
    assert loader.get_breaker_state()["consec_failures"] == 1
    fake_bridge.tasks_raises = None
    loader.load_superset_context()
    assert loader.get_breaker_state()["consec_failures"] == 0


# ── Wall-clock budget ────────────────────────────────────────────────────────


def test_budget_exhausted_after_workspace_skips_tasks(loader, fake_bridge):
    fake_bridge.delay = 0.05  # 50ms per call
    out = loader.load_superset_context(
        workspace_id="ws-1", budget_seconds=0.001  # 1ms — already exceeded
    )
    # Workspace was called, tasks skipped because budget exhausted.
    assert fake_bridge.workspace_calls == [("ws-1", None)]
    assert fake_bridge.task_calls == []
    assert any("budget_exhausted" in e for e in out["errors"])


# ── launch_agent_session (third high-value tool) ─────────────────────────────


def test_launch_agent_session_happy_path(loader, fake_bridge):
    out = loader.launch_agent_session(
        workspace_id="ws-1", task_id="t1", agent="claude", device_id="dev-1"
    )
    assert out["available"] is True
    assert out["result"] == fake_bridge.agent_result
    assert fake_bridge.agent_calls == [("ws-1", "t1", "claude", "dev-1")]


def test_launch_agent_session_breaker_open_blocks(loader, fake_bridge):
    fake_bridge.tasks_raises = RuntimeError("down")
    fake_bridge.workspace_raises = RuntimeError("down")
    for _ in range(3):
        loader.load_superset_context(workspace_id="ws-1")
    out = loader.launch_agent_session(workspace_id="ws-1")
    assert out["available"] is False
    assert out["error"] == "breaker_open"
    # No call reached the bridge.
    assert fake_bridge.agent_calls == []


def test_launch_agent_session_exception_caught(loader, fake_bridge):
    fake_bridge.agent_raises = RuntimeError("session start failed")
    out = loader.launch_agent_session(workspace_id="ws-1")
    assert out["available"] is False
    assert "session start failed" in out["error"]


def test_launch_agent_session_disabled_via_env(loader, fake_bridge, monkeypatch):
    monkeypatch.setattr(loader, "ENABLED", False)
    out = loader.launch_agent_session(workspace_id="ws-1")
    assert out["available"] is False
    assert out["error"] == "disabled_via_env"
    assert fake_bridge.agent_calls == []


# ── Public surface ───────────────────────────────────────────────────────────


def test_public_api_surface(loader):
    assert hasattr(loader, "load_superset_context")
    assert hasattr(loader, "launch_agent_session")
    assert hasattr(loader, "get_breaker_state")
    assert hasattr(loader, "reset_breaker")
