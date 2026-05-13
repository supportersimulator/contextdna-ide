"""
Claude Agent Runner - Subprocess engine for Claude Code CLI sessions.

Manages dual-mode agent execution:
  - subscription: spawns `claude` CLI (uses user's Pro/Max subscription OAuth)
  - api: calls Anthropic Messages API directly (uses ANTHROPIC_API_KEY)

Session persistence is ON by default - sessions survive crashes and can be resumed.
"""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures (match agent-panel.tsx TypeScript interfaces)
# ---------------------------------------------------------------------------

@dataclass
class AgentSession:
    id: str
    task: str
    model: str
    status: str  # idle, running, completed, failed, crashed, stopped
    startedAt: float  # ms timestamp
    tokens: int = 0
    injectionActive: bool = False
    mode: str = "subscription"
    cost_usd: float = 0.0
    claude_session_id: Optional[str] = None  # For resume capability
    num_turns: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentOutput:
    agentId: str
    type: str  # text, tool_call, tool_result, error
    content: str
    timestamp: int  # ms

    def to_dict(self) -> dict:
        return asdict(self)


# Model alias mapping (match CLI conventions)
MODEL_MAP = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
}

# Provider → model catalog (catalog model ID → (provider, API model ID))
PROVIDER_MODEL_MAP = {
    "anthropic/opus": ("anthropic", "claude-opus-4-6"),
    "anthropic/sonnet": ("anthropic", "claude-sonnet-4-5-20250929"),
    "anthropic/haiku": ("anthropic", "claude-haiku-4-5-20251001"),
    "openai/gpt-4o": ("openai", "gpt-4o"),
    "openai/gpt-4o-mini": ("openai", "gpt-4o-mini"),
    "openai/o1": ("openai", "o1"),
    "deepseek/chat": ("deepseek", "deepseek-chat"),
    "deepseek/reasoner": ("deepseek", "deepseek-reasoner"),
}

PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}

PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "Context_DNA_OPENAI",
    "deepseek": "Context_DNA_Deepseek",
}


class ClaudeAgentRunner:
    """Manages claude CLI subprocess sessions with dual-mode support."""

    def __init__(self):
        self.sessions: Dict[str, AgentSession] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._output_callback: Optional[Callable] = None
        self._session_callback: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Subscription mode: spawn `claude` CLI
    # ------------------------------------------------------------------

    async def spawn_subscription(
        self,
        session_id: str,
        task: str,
        model: str = "sonnet",
        system_prompt: str = "",
        permission_mode: str = "default",
        allowed_tools: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        session_persistence: bool = True,
    ) -> AgentSession:
        """Spawn claude CLI using user's subscription auth."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError("claude CLI not found in PATH. Install: npm install -g @anthropic-ai/claude-code")

        cmd = [
            claude_bin, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", MODEL_MAP.get(model, model),
            "--permission-mode", permission_mode,
        ]
        if not session_persistence:
            cmd.append("--no-session-persistence")
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        if allowed_tools:
            cmd.extend(["--allowed-tools"] + allowed_tools)
        cmd.append(task)

        session = AgentSession(
            id=session_id,
            task=task,
            model=model,
            status="running",
            startedAt=time.time() * 1000,
            injectionActive=bool(system_prompt),
            mode="subscription",
        )
        self.sessions[session_id] = session

        work_dir = cwd or os.environ.get(
            "PROJECT_ROOT",
            str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        self._processes[session_id] = proc

        # Stream output in background
        bg_task = asyncio.ensure_future(self._stream_cli_output(session_id, proc))
        self._tasks[session_id] = bg_task

        logger.info(f"[AgentRunner] Spawned subscription session {session_id} (model={model}, pid={proc.pid})")
        return session

    # ------------------------------------------------------------------
    # Resume a crashed/stopped subscription session
    # ------------------------------------------------------------------

    async def resume_subscription(
        self,
        session_id: str,
        claude_session_id: str,
        system_prompt: str = "",
        cwd: Optional[str] = None,
    ) -> AgentSession:
        """Resume a crashed session using claude --resume."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError("claude CLI not found in PATH")

        old_session = self.sessions.get(session_id)
        model = old_session.model if old_session else "sonnet"

        cmd = [
            claude_bin, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", MODEL_MAP.get(model, model),
            "--resume", claude_session_id,
        ]
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        session = AgentSession(
            id=session_id,
            task=old_session.task if old_session else "(resumed)",
            model=model,
            status="running",
            startedAt=time.time() * 1000,
            injectionActive=old_session.injectionActive if old_session else False,
            mode="subscription",
            claude_session_id=claude_session_id,
        )
        self.sessions[session_id] = session

        work_dir = cwd or os.environ.get(
            "PROJECT_ROOT",
            str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        self._processes[session_id] = proc

        bg_task = asyncio.ensure_future(self._stream_cli_output(session_id, proc))
        self._tasks[session_id] = bg_task

        self._emit_output(session_id, "text", f"Resumed session {claude_session_id[:12]}...")
        logger.info(f"[AgentRunner] Resumed session {session_id} (claude_sid={claude_session_id[:12]})")
        return session

    # ------------------------------------------------------------------
    # API mode: direct Anthropic Messages API call
    # ------------------------------------------------------------------

    async def spawn_api(
        self,
        session_id: str,
        task: str,
        model: str = "sonnet",
        system_prompt: str = "",
        api_key: str = "",
    ) -> AgentSession:
        """Spawn direct Anthropic API session (pay-per-token mode)."""
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set for API mode")

        # Model ID mapping for API
        api_models = {
            "opus": "claude-opus-4-6",
            "sonnet": "claude-sonnet-4-5-20250929",
            "haiku": "claude-haiku-4-5-20251001",
        }

        session = AgentSession(
            id=session_id,
            task=task,
            model=model,
            status="running",
            startedAt=time.time() * 1000,
            injectionActive=bool(system_prompt),
            mode="api",
        )
        self.sessions[session_id] = session

        bg_task = asyncio.ensure_future(
            self._stream_api_output(session_id, task, api_models.get(model, model), system_prompt, api_key)
        )
        self._tasks[session_id] = bg_task

        logger.info(f"[AgentRunner] Spawned API session {session_id} (model={model})")
        return session

    # ------------------------------------------------------------------
    # OpenAI-compatible mode: DeepSeek, OpenAI, etc.
    # ------------------------------------------------------------------

    async def spawn_openai_compat(
        self,
        session_id: str,
        task: str,
        provider: str,
        model_id: str,
        system_prompt: str = "",
        api_key: str = "",
        base_url: str = "",
    ) -> AgentSession:
        """Spawn using OpenAI-compatible API (works for OpenAI, DeepSeek, etc.)."""
        if not api_key:
            env_key = PROVIDER_ENV_KEYS.get(provider, f"{provider.upper()}_API_KEY")
            raise RuntimeError(f"{env_key} not set for {provider} mode")

        session = AgentSession(
            id=session_id,
            task=task,
            model=f"{provider}/{model_id}",
            status="running",
            startedAt=time.time() * 1000,
            injectionActive=bool(system_prompt),
            mode="api",
        )
        self.sessions[session_id] = session

        bg_task = asyncio.ensure_future(
            self._stream_openai_output(session_id, task, model_id, system_prompt, api_key, base_url)
        )
        self._tasks[session_id] = bg_task

        logger.info(f"[AgentRunner] Spawned {provider} session {session_id} (model={model_id})")
        return session

    # ------------------------------------------------------------------
    # Stop a session
    # ------------------------------------------------------------------

    async def stop(self, session_id: str) -> None:
        """Stop a running session."""
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()

        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()

        session = self.sessions.get(session_id)
        if session and session.status == "running":
            session.status = "stopped"
            self._notify_session_update(session_id)

        logger.info(f"[AgentRunner] Stopped session {session_id}")

    # ------------------------------------------------------------------
    # Internal: parse CLI NDJSON output
    # ------------------------------------------------------------------

    async def _stream_cli_output(self, session_id: str, proc: asyncio.subprocess.Process) -> None:
        """Parse NDJSON from claude CLI stdout, emit events."""
        session = self.sessions[session_id]
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "system":
                    # Init event — capture Claude's session ID for resume
                    csid = event.get("session_id")
                    if csid:
                        session.claude_session_id = csid
                    self._emit_output(session_id, "text",
                        f"Session started (model={event.get('model', '?')})")

                elif etype == "stream_event":
                    inner = event.get("event", {})
                    inner_type = inner.get("type")
                    if inner_type == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            self._emit_output(session_id, "text", delta["text"])
                    elif inner_type == "content_block_start":
                        block = inner.get("content_block", {})
                        if block.get("type") == "tool_use":
                            self._emit_output(session_id, "tool_call",
                                f"{block.get('name', '?')}")

                elif etype == "assistant":
                    # Complete assistant message — extract tool calls
                    msg = event.get("message", {})
                    for content in msg.get("content", []):
                        if content.get("type") == "tool_use":
                            name = content.get("name", "?")
                            inp = json.dumps(content.get("input", {}))
                            self._emit_output(session_id, "tool_call",
                                f"{name}: {inp[:300]}")

                elif etype == "user":
                    # Tool results from CLI
                    self._emit_output(session_id, "tool_result", "(tool completed)")

                elif etype == "result":
                    # Final result
                    session.status = "failed" if event.get("is_error") else "completed"
                    usage = event.get("usage", {})
                    session.tokens = (
                        usage.get("input_tokens", 0) +
                        usage.get("output_tokens", 0) +
                        usage.get("cache_read_input_tokens", 0) +
                        usage.get("cache_creation_input_tokens", 0)
                    )
                    session.cost_usd = event.get("total_cost_usd", 0)
                    session.num_turns = event.get("num_turns", 0)

                    result_text = event.get("result", "")
                    if result_text:
                        self._emit_output(session_id, "text", f"\n--- Result ---\n{result_text}")

                    self._notify_session_update(session_id)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[AgentRunner] Stream error for {session_id}: {e}")
            self._emit_output(session_id, "error", str(e))

        # Wait for process to finish
        try:
            await proc.wait()
        except Exception:
            pass

        # Mark crashed if still running
        if session.status == "running":
            if proc.returncode and proc.returncode != 0:
                session.status = "crashed"
                # Read stderr for crash info
                try:
                    stderr = await proc.stderr.read()
                    if stderr:
                        err_text = stderr.decode("utf-8", errors="replace").strip()
                        if err_text:
                            self._emit_output(session_id, "error", err_text[:500])
                except Exception:
                    pass
            else:
                session.status = "completed"
            self._notify_session_update(session_id)

        # Cleanup
        self._processes.pop(session_id, None)
        self._tasks.pop(session_id, None)

    # ------------------------------------------------------------------
    # Internal: Anthropic Messages API streaming
    # ------------------------------------------------------------------

    async def _stream_api_output(
        self,
        session_id: str,
        task: str,
        model_id: str,
        system_prompt: str,
        api_key: str,
    ) -> None:
        """Stream from Anthropic Messages API (API key mode)."""
        session = self.sessions[session_id]
        try:
            import httpx
        except ImportError:
            session.status = "failed"
            self._emit_output(session_id, "error", "httpx not installed")
            self._notify_session_update(session_id)
            return

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model_id,
            "max_tokens": 8192,
            "stream": True,
            "system": system_prompt or "You are Claude, a helpful AI assistant.",
            "messages": [{"role": "user", "content": task}],
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        session.status = "failed"
                        self._emit_output(session_id, "error",
                            f"API error {response.status_code}: {error_body.decode()[:300]}")
                        self._notify_session_update(session_id)
                        return

                    accumulated = ""
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                accumulated += text
                                self._emit_output(session_id, "text", text)
                        elif etype == "message_delta":
                            usage = event.get("usage", {})
                            session.tokens = usage.get("output_tokens", 0)
                        elif etype == "message_stop":
                            break

            session.status = "completed"
            self._notify_session_update(session_id)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[AgentRunner] API stream error for {session_id}: {e}")
            session.status = "failed"
            self._emit_output(session_id, "error", str(e))
            self._notify_session_update(session_id)

    # ------------------------------------------------------------------
    # Internal: OpenAI-compatible streaming (DeepSeek, OpenAI, etc.)
    # ------------------------------------------------------------------

    async def _stream_openai_output(
        self,
        session_id: str,
        task: str,
        model_id: str,
        system_prompt: str,
        api_key: str,
        base_url: str,
    ) -> None:
        """Stream from OpenAI-compatible API (works for DeepSeek, OpenAI, etc.)."""
        session = self.sessions[session_id]
        try:
            import httpx
        except ImportError:
            session.status = "failed"
            self._emit_output(session_id, "error", "httpx not installed")
            self._notify_session_update(session_id)
            return

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})

        body = {
            "model": model_id,
            "max_tokens": 8192,
            "stream": True,
            "messages": messages,
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=body,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        session.status = "failed"
                        self._emit_output(session_id, "error",
                            f"API error {response.status_code}: {error_body.decode()[:300]}")
                        self._notify_session_update(session_id)
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        choices = event.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                self._emit_output(session_id, "text", content)

                        # Track usage if present (some providers include it)
                        usage = event.get("usage")
                        if usage:
                            session.tokens = (
                                usage.get("prompt_tokens", 0) +
                                usage.get("completion_tokens", 0)
                            )

            session.status = "completed"
            self._notify_session_update(session_id)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[AgentRunner] OpenAI-compat stream error for {session_id}: {e}")
            session.status = "failed"
            self._emit_output(session_id, "error", str(e))
            self._notify_session_update(session_id)

    # ------------------------------------------------------------------
    # Output + session callbacks
    # ------------------------------------------------------------------

    def _emit_output(self, session_id: str, output_type: str, content: str) -> None:
        if self._output_callback and content:
            self._output_callback(AgentOutput(
                agentId=session_id,
                type=output_type,
                content=content,
                timestamp=int(time.time() * 1000),
            ).to_dict())

    def _notify_session_update(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session and self._session_callback:
            self._session_callback(session_id, session.to_dict())

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def get_all_sessions(self) -> List[dict]:
        return [s.to_dict() for s in self.sessions.values()]

    def get_session(self, session_id: str) -> Optional[dict]:
        s = self.sessions.get(session_id)
        return s.to_dict() if s else None
