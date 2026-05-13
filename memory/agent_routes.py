"""
Agent Task Router - /api/agents/* endpoints for the Agent Panel UI.

Dual-mode agent execution:
  - subscription: spawns `claude` CLI (uses user's Claude Pro/Max subscription)
  - api: calls Anthropic Messages API directly (uses ANTHROPIC_API_KEY)

Mounts into agent_service.py alongside swarm_controller, librarian, etc.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Optional, List, Set

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from memory.claude_agent_runner import (
    ClaudeAgentRunner,
    PROVIDER_MODEL_MAP,
    PROVIDER_BASE_URLS,
    PROVIDER_ENV_KEYS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic API quota — Redis-backed, survives restarts, shared across nodes
# ---------------------------------------------------------------------------
_ANTHROPIC_DAILY_CAP = int(os.environ.get("ANTHROPIC_DAILY_AGENT_CAP", "10"))
_ANTHROPIC_WEEKLY_TOKEN_CAP = int(os.environ.get("ANTHROPIC_WEEKLY_TOKEN_CAP", "500000"))
_DEEPSEEK_FAILOVER_ENABLED = os.environ.get("DEEPSEEK_FAILOVER_ENABLED", "1") == "1"

# ---------------------------------------------------------------------------
# OmniRoute integration — 429/rate-limit detection + 5-tier failover
# ---------------------------------------------------------------------------
_omniroute_orch = None

def _get_omniroute():
    """Lazy-init OmniRoute orchestrator (5-tier failover with 429 detection)."""
    global _omniroute_orch
    if _omniroute_orch is None:
        try:
            import sys
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            sys.path.insert(0, os.path.join(repo_root, "multi-fleet"))
            from multifleet.omniroute import OmniRouteOrchestrator
            _omniroute_orch = OmniRouteOrchestrator(repo_root=repo_root)
            logger.info("[AgentRouter] OmniRoute orchestrator initialized (5-tier failover)")
        except Exception as e:
            logger.debug("[AgentRouter] OmniRoute not available: %s", e)
    return _omniroute_orch

def _get_redis():
    """Get Redis connection for persistent quota tracking."""
    try:
        import redis
        return redis.Redis(host="127.0.0.1", port=6379, db=0, decode_responses=True)
    except Exception:
        return None

def _check_anthropic_cap() -> bool:
    """Return True if under daily cap. Uses Redis if available, in-memory fallback."""
    r = _get_redis()
    if r:
        try:
            count = int(r.get("anthropic:daily_agent_count") or 0)
            return count < _ANTHROPIC_DAILY_CAP
        except Exception:
            pass
    # In-memory fallback
    import time
    global _fallback_count, _fallback_reset
    now = time.time()
    if now - _fallback_reset > 86400:
        _fallback_count = 0
        _fallback_reset = now
    return _fallback_count < _ANTHROPIC_DAILY_CAP

_fallback_count = 0
_fallback_reset = 0.0


# ---------------------------------------------------------------------------
# Failover notification — macOS + fleet broadcast (once per day)
# ---------------------------------------------------------------------------

def _record_failover_event():
    """Record failover activation in Redis and fire user-facing notifications.

    Notifications are deduped to once per calendar day (UTC).
    """
    now_ts = time.time()
    r = _get_redis()
    already_notified_today = False

    if r:
        try:
            r.set("anthropic:failover_active", "1", ex=86400)
            r.set("anthropic:last_failover_at", str(now_ts), ex=86400)
            # Dedup: set a flag for today (expires at midnight-ish)
            notif_key = "anthropic:failover_notified_today"
            if r.get(notif_key):
                already_notified_today = True
            else:
                r.set(notif_key, "1", ex=86400)
        except Exception as e:
            logger.warning(f"[AgentRouter] Redis failover tracking failed: {e}")

    if already_notified_today:
        return

    # macOS notification (non-blocking)
    try:
        subprocess.Popen(
            [
                "osascript", "-e",
                'display notification "Anthropic daily cap reached — agents using DeepSeek failover" '
                'with title "Context DNA" subtitle "Agent Failover Active"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.debug(f"[AgentRouter] macOS notification failed (expected on non-mac): {e}")

    # Fleet broadcast via P7 git channel (best-effort, non-blocking)
    try:
        _broadcast_failover_fleet_message()
    except Exception as e:
        logger.warning(f"[AgentRouter] Fleet failover broadcast failed: {e}")


def _broadcast_failover_fleet_message():
    """Write a failover alert to .fleet-messages/all/ (P7 git channel)."""
    import socket
    from datetime import datetime, timezone

    node_id = os.environ.get("MULTIFLEET_NODE_ID", socket.gethostname().split(".")[0])
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filename_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    msg_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".fleet-messages", "all",
    )
    os.makedirs(msg_dir, exist_ok=True)

    msg_path = os.path.join(msg_dir, f"{filename_ts}-failover-{node_id}.md")
    content = (
        f"---\n"
        f"from: {node_id}\n"
        f"to: all\n"
        f"subject: Anthropic API cap reached — DeepSeek failover active\n"
        f"timestamp: {ts}\n"
        f"priority: high\n"
        f"---\n\n"
        f"Anthropic daily agent cap ({_ANTHROPIC_DAILY_CAP}) reached on {node_id}.\n"
        f"Agent tasks are routing to DeepSeek automatically.\n"
        f"Cap resets in ~24h.\n"
    )
    with open(msg_path, "w") as f:
        f.write(content)
    logger.info(f"[AgentRouter] Fleet failover broadcast written: {msg_path}")


def _increment_anthropic_cap(tokens_used: int = 0):
    """Increment daily call count + weekly token counter."""
    global _fallback_count
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            # Daily call counter (auto-expires at midnight UTC)
            pipe.incr("anthropic:daily_agent_count")
            pipe.expire("anthropic:daily_agent_count", 86400)
            # Weekly token counter (7-day rolling window)
            if tokens_used > 0:
                pipe.incrby("anthropic:weekly_tokens_used", tokens_used)
                pipe.expire("anthropic:weekly_tokens_used", 604800)
            result = pipe.execute()
            daily = result[0]
            weekly_tokens = int(r.get("anthropic:weekly_tokens_used") or 0)
            logger.info(f"[AgentRouter] Anthropic quota: {daily}/{_ANTHROPIC_DAILY_CAP} daily calls, "
                        f"{weekly_tokens:,}/{_ANTHROPIC_WEEKLY_TOKEN_CAP:,} weekly tokens")
            return
        except Exception as e:
            logger.warning(f"[AgentRouter] Redis quota update failed: {e}")
    # In-memory fallback
    _fallback_count += 1
    logger.info(f"[AgentRouter] Anthropic usage (in-memory): {_fallback_count}/{_ANTHROPIC_DAILY_CAP} daily cap")

def _get_anthropic_quota_status() -> dict:
    """Get current quota status for monitoring."""
    r = _get_redis()
    if r:
        try:
            failover_active = r.get("anthropic:failover_active") == "1"
            last_failover_raw = r.get("anthropic:last_failover_at")
            last_failover_at = float(last_failover_raw) if last_failover_raw else None
            return {
                "daily_calls": int(r.get("anthropic:daily_agent_count") or 0),
                "daily_cap": _ANTHROPIC_DAILY_CAP,
                "weekly_tokens": int(r.get("anthropic:weekly_tokens_used") or 0),
                "weekly_token_cap": _ANTHROPIC_WEEKLY_TOKEN_CAP,
                "failover_enabled": _DEEPSEEK_FAILOVER_ENABLED,
                "failover_active": failover_active,
                "last_failover_at": last_failover_at,
                "backend": "redis",
            }
        except Exception:
            pass
    return {
        "daily_calls": _fallback_count,
        "daily_cap": _ANTHROPIC_DAILY_CAP,
        "weekly_tokens": -1,
        "weekly_token_cap": _ANTHROPIC_WEEKLY_TOKEN_CAP,
        "failover_enabled": _DEEPSEEK_FAILOVER_ENABLED,
        "failover_active": _fallback_count >= _ANTHROPIC_DAILY_CAP,
        "last_failover_at": None,
        "backend": "in-memory",
    }

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

# Default research tools for spawned agents — full read + search + web access
AGENT_RESEARCH_TOOLS = [
    "Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch",
    "Task", "Write", "Edit",
]


class SpawnRequest(BaseModel):
    task: str = Field(..., description="Task description")
    model: str = Field(default="anthropic/sonnet", description="Catalog model ID: 'anthropic/sonnet', 'deepseek/chat', etc.")
    mode: str = Field(default="subscription", description="subscription or api")
    permission_mode: str = Field(default="default", description="Tool permission mode")
    inject_context: bool = Field(default=True, description="Inject Context DNA context")
    session_persistence: bool = Field(default=True, description="Keep session on disk for crash resume")
    allowed_tools: Optional[List[str]] = Field(default=None, description="Tools to allow. None = AGENT_RESEARCH_TOOLS default")
    system_prompt: Optional[str] = None


class SetApiKeyRequest(BaseModel):
    provider: str = Field(..., description="Provider: anthropic, openai, deepseek")
    api_key: str = Field(..., description="API key value")
    base_url: Optional[str] = Field(None, description="Custom base URL override")
    env_key: Optional[str] = Field(None, description="Custom env var name override (e.g., DS_KEY instead of Context_DNA_Deepseek)")


class ResumeRequest(BaseModel):
    claude_session_id: str = Field(..., description="Claude session ID to resume")


class StopRequest(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_agent_router() -> APIRouter:
    """Create the FastAPI router for Agent Tasks."""

    router = APIRouter(prefix="/api/agents", tags=["agents"])
    runner = ClaudeAgentRunner()

    # Module-level WS client set (accessed by agent_service.py for /ws/agents)
    # We store it on the runner so the WS endpoint in agent_service.py can access it
    runner._ws_clients: Set[WebSocket] = set()

    # ------------------------------------------------------------------
    # Wire callbacks for WebSocket broadcasting
    # ------------------------------------------------------------------

    def on_output(output_event: dict):
        """Broadcast output to all connected WS clients."""
        asyncio.ensure_future(_broadcast({
            "type": "output",
            "data": output_event,
        }))

    def on_session_update(session_id: str, session_data: dict):
        """Broadcast session state change."""
        asyncio.ensure_future(_broadcast({
            "type": "session_update",
            "data": session_data,
        }))

    runner._output_callback = on_output
    runner._session_callback = on_session_update

    async def _broadcast(msg: dict):
        dead = set()
        for ws in runner._ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        runner._ws_clients -= dead

    # ------------------------------------------------------------------
    # Context DNA injection helper
    # ------------------------------------------------------------------

    def _get_context_injection(task: str) -> str:
        """Load compact CLAUDE-agent.md for spawned agents (~400 tokens).

        Falls back to full webhook injection if file missing.
        """
        try:
            import os
            agent_md = os.path.join(os.path.dirname(os.path.dirname(__file__)), "CLAUDE-agent.md")
            if os.path.isfile(agent_md):
                with open(agent_md, "r") as f:
                    return f.read()
        except Exception:
            pass
        # Fallback: full injection (higher token cost)
        try:
            from memory.persistent_hook_structure import generate_context_injection
            result = generate_context_injection(task, mode="minimal")
            return result.content if hasattr(result, "content") else str(result)
        except Exception as e:
            logger.warning(f"[AgentRouter] Context injection failed: {e}")
            return f"[Context DNA injection failed: {e}]"

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @router.post("/spawn")
    async def spawn_agent(req: SpawnRequest):
        """Spawn a new agent session. Routes to correct provider based on model ID."""
        session_id = f"a-{uuid.uuid4().hex[:8]}"

        # Parse provider from catalog model ID (e.g., 'deepseek/chat' → provider='deepseek')
        provider, api_model_id = PROVIDER_MODEL_MAP.get(
            req.model,
            ("anthropic", req.model),  # fallback: treat as legacy Claude model name
        )

        # Build system prompt with optional Context DNA injection
        system_prompt = req.system_prompt or ""
        if req.inject_context:
            injection = _get_context_injection(req.task)
            if injection:
                system_prompt = injection + ("\n\n" + system_prompt if system_prompt else "")

        # Apply default research tools if not explicitly specified
        tools = req.allowed_tools if req.allowed_tools is not None else AGENT_RESEARCH_TOOLS

        try:
            if req.mode == "subscription" and provider == "anthropic":
                # Claude CLI subscription path
                # Map catalog ID to CLI model name: 'anthropic/sonnet' → 'sonnet'
                cli_model = req.model.split("/")[-1] if "/" in req.model else req.model
                session = await runner.spawn_subscription(
                    session_id=session_id,
                    task=req.task,
                    model=cli_model,
                    system_prompt=system_prompt,
                    permission_mode=req.permission_mode,
                    allowed_tools=tools,
                    session_persistence=req.session_persistence,
                )
            elif provider == "anthropic":
                # Anthropic API path — daily cap + auto-failover to DeepSeek
                if not _check_anthropic_cap():
                    if _DEEPSEEK_FAILOVER_ENABLED:
                        # Auto-failover: reroute to DeepSeek instead of 429
                        ds_key_env = ENV_KEY_MAP.get("deepseek", PROVIDER_ENV_KEYS.get("deepseek", ""))
                        ds_key = os.environ.get(ds_key_env, "")
                        if ds_key:
                            logger.warning(
                                f"[AgentRouter] Anthropic cap reached — auto-failover to DeepSeek "
                                f"(cap: {_ANTHROPIC_DAILY_CAP}/day)"
                            )
                            _record_failover_event()
                            ds_base = os.environ.get("DEEPSEEK_BASE_URL",
                                                     PROVIDER_BASE_URLS.get("deepseek", ""))
                            ds_model = "deepseek-chat"
                            session = await runner.spawn_openai_compat(
                                session_id=session_id,
                                task=req.task,
                                provider="deepseek",
                                model_id=ds_model,
                                system_prompt=system_prompt,
                                api_key=ds_key,
                                base_url=ds_base,
                            )
                            # Mark as failover in log (session is frozen dataclass)
                            logger.info(f"[AgentRouter] Session {session_id} failover: anthropic → deepseek")
                        else:
                            raise HTTPException(
                                status_code=429,
                                detail=f"Anthropic daily cap reached ({_ANTHROPIC_DAILY_CAP}). "
                                       f"DeepSeek failover enabled but no API key configured."
                            )
                    else:
                        raise HTTPException(
                            status_code=429,
                            detail=f"Anthropic daily agent cap reached ({_ANTHROPIC_DAILY_CAP}). "
                                   f"Set DEEPSEEK_FAILOVER_ENABLED=1 for auto-failover."
                        )
                else:
                    env_key = ENV_KEY_MAP.get("anthropic", "ANTHROPIC_API_KEY")
                    api_key = os.environ.get(env_key, "")
                    if not api_key:
                        raise HTTPException(status_code=503, detail=f"{env_key} not configured")
                    _increment_anthropic_cap()
                    session = await runner.spawn_api(
                        session_id=session_id,
                        task=req.task,
                        model=req.model.split("/")[-1] if "/" in req.model else req.model,
                        system_prompt=system_prompt,
                        api_key=api_key,
                    )
            elif provider in ("openai", "deepseek"):
                # OpenAI-compatible API path — check ENV_KEY_MAP first (may have custom name)
                env_key = ENV_KEY_MAP.get(provider, PROVIDER_ENV_KEYS.get(provider, ""))
                api_key = os.environ.get(env_key, "")
                if not api_key:
                    raise HTTPException(status_code=503, detail=f"{env_key} not configured for {provider}")
                base_url = os.environ.get(
                    f"{provider.upper()}_BASE_URL",
                    PROVIDER_BASE_URLS.get(provider, ""),
                )
                session = await runner.spawn_openai_compat(
                    session_id=session_id,
                    task=req.task,
                    provider=provider,
                    model_id=api_model_id,
                    system_prompt=system_prompt,
                    api_key=api_key,
                    base_url=base_url,
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

            return {
                "session_id": session.id,
                "status": session.status,
                "mode": session.mode,
                "provider": provider,
            }
        except RuntimeError as e:
            # Check if this is a rate-limit error and trigger OmniRoute failover
            error_str = str(e)
            orch = _get_omniroute()
            if orch and ("429" in error_str or "rate" in error_str.lower() or "overloaded" in error_str.lower()):
                error_code = 429 if "429" in error_str else (529 if "overloaded" in error_str.lower() else 503)
                failover_result = orch.detect_and_handle_error(error_code, error_str)
                if failover_result.get("action") == "failover":
                    logger.warning(
                        "[AgentRouter] OmniRoute failover triggered: %s -> %s",
                        failover_result.get("from_provider"),
                        failover_result.get("to_provider"),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=f"Rate limit hit. OmniRoute failover: "
                               f"{failover_result.get('from_provider')} -> {failover_result.get('to_provider')}. "
                               f"Retry with the new provider.",
                    )
            raise HTTPException(status_code=503, detail=str(e))

    @router.post("/resume/{session_id}")
    async def resume_agent(session_id: str, req: ResumeRequest):
        """Resume a crashed session."""
        system_prompt = ""
        old_session = runner.sessions.get(session_id)
        if old_session and old_session.injectionActive:
            system_prompt = _get_context_injection(old_session.task)

        try:
            session = await runner.resume_subscription(
                session_id=session_id,
                claude_session_id=req.claude_session_id,
                system_prompt=system_prompt,
            )
            return {
                "session_id": session.id,
                "status": session.status,
                "mode": session.mode,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    @router.get("/status")
    async def get_status():
        """Get all sessions and pending approvals."""
        return {
            "sessions": runner.get_all_sessions(),
            "approvals": [],  # CLI auto-executes tools in -p mode
        }

    @router.get("/quota")
    async def get_quota():
        """Get Anthropic API quota status — daily calls + weekly tokens."""
        return _get_anthropic_quota_status()

    @router.post("/stop/{session_id}")
    async def stop_agent(session_id: str):
        """Stop a running session."""
        if session_id not in runner.sessions:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        await runner.stop(session_id)
        return {"stopped": session_id}

    @router.get("/omniroute/status")
    async def omniroute_status():
        """Get OmniRoute orchestrator status — provider health, active tier, failover history."""
        orch = _get_omniroute()
        if orch is None:
            return {"available": False, "reason": "OmniRoute orchestrator not initialized"}
        return {"available": True, **orch.status()}

    @router.post("/omniroute/recover/{tier}")
    async def omniroute_recover(tier: int):
        """Attempt to recover a failed provider tier (1-5)."""
        orch = _get_omniroute()
        if orch is None:
            raise HTTPException(status_code=503, detail="OmniRoute not available")
        try:
            from multifleet.omniroute import ProviderTier
            ok = orch.attempt_recovery(ProviderTier(tier))
            return {"recovered": ok, "tier": tier, "status": orch.status()}
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid tier: {tier}. Valid: 1-5")

    @router.post("/approve/{approval_id}")
    async def approve_tool(approval_id: str, approved: bool = True):
        """Acknowledge tool execution (informational in CLI mode)."""
        return {"acknowledged": approval_id, "approved": approved}

    # ------------------------------------------------------------------
    # API Key configuration endpoints
    # ------------------------------------------------------------------

    ENV_KEY_MAP = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "Context_DNA_OPENAI",
        "deepseek": "Context_DNA_Deepseek",
        "google": "GOOGLE_API_KEY",
    }

    # Common alternate env var names to auto-detect
    _ALT_ENV_NAMES: dict = {
        "anthropic": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY"],
        "openai": ["Context_DNA_OPENAI", "OPENAI_KEY", "OPEN_AI_KEY"],
        "deepseek": ["Context_DNA_Deepseek", "DEEPSEEK_KEY", "DS_API_KEY", "DS_KEY"],
        "google": ["GOOGLE_API_KEY", "GOOGLE_AI_KEY", "GEMINI_API_KEY"],
    }

    @router.get("/config/api-keys")
    async def list_api_keys():
        """List configured API keys (masked — never expose full keys)."""
        result = {}
        for provider_id, env_key in ENV_KEY_MAP.items():
            val = os.environ.get(env_key, "")
            found_key = env_key
            # If primary env key is empty, scan alternate names
            if not val:
                for alt in _ALT_ENV_NAMES.get(provider_id, []):
                    alt_val = os.environ.get(alt, "")
                    if alt_val and alt_val.strip():
                        val = alt_val
                        found_key = alt
                        # Update the map so spawn uses the correct key
                        ENV_KEY_MAP[provider_id] = alt
                        break
            result[provider_id] = {
                "configured": bool(val and val.strip()),
                "masked": (val[:4] + "...") if val and len(val) > 4 else "",
                "env_key": found_key,
            }
        return result

    @router.post("/config/api-key")
    async def set_api_key(req: SetApiKeyRequest):
        """Store API key in os.environ and persist to .env file."""
        # Use custom env var name if provided, else canonical default
        env_key = req.env_key or ENV_KEY_MAP.get(req.provider)
        if not env_key:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

        # Track the custom env key mapping so spawn can find the key
        if req.env_key:
            ENV_KEY_MAP[req.provider] = req.env_key

        # Update runtime environment
        os.environ[env_key] = req.api_key
        if req.base_url:
            os.environ[f"{req.provider.upper()}_BASE_URL"] = req.base_url

        # Persist to .env file
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "memory", ".env.local",
        )
        _update_env_file(env_path, env_key, req.api_key)
        if req.base_url:
            _update_env_file(env_path, f"{req.provider.upper()}_BASE_URL", req.base_url)

        logger.info(f"[AgentRouter] API key set for {req.provider} ({env_key})")
        return {"status": "ok", "provider": req.provider, "env_key": env_key}

    # Store runner on router for agent_service.py to access WS clients
    router._runner = runner

    return router


def _update_env_file(path: str, key: str, value: str) -> None:
    """Update or add a key=value in an .env file."""
    lines = []
    found = False
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.writelines(new_lines)
