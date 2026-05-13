#!/usr/bin/env python3
"""
Swarm Controller — orchestrates DeepSeek agents for complex coding tasks.

Pattern: "Swarm cheap, integrate premium"
- N DeepSeek agents ($0.28/1M) do parallel analysis
- 1 premium integrator (local Qwen3 or Opus) synthesizes

Architecture:
  Task → Fan-out (parallel agents) → Collect → Harmonize → Integrate → Output

Usage:
    from memory.swarm_controller import SwarmController

    controller = SwarmController()
    run = await controller.execute(
        task="Refactor error handling in memory/*.py",
        context={"focus_dirs": ["memory/"]},
        roles=[SwarmAgentRole.CODE_ARCHAEOLOGIST, SwarmAgentRole.PATCH_DRAFTER],
    )
    print(run.integrated_result)
    print(f"Cost: ${run.cost_estimate.total_usd:.4f}")

FastAPI:
    POST /v1/swarm/run         — start a swarm run
    GET  /v1/swarm/run/{id}    — status + results
    GET  /v1/swarm/health      — system health

Created: February 10, 2026
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("context_dna.swarm")

LIBRARIAN_URL = "http://127.0.0.1:8080/v1/context/query"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CostEstimate:
    """Token usage and cost tracking for a swarm run."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_usd: float = 0.0

    def add(self, input_tok: int, output_tok: int, usd: float) -> None:
        self.input_tokens += input_tok
        self.output_tokens += output_tok
        self.total_usd += usd

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_usd": round(self.total_usd, 6),
        }


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COLLECTING = "collecting"
    HARMONIZING = "harmonizing"
    INTEGRATING = "integrating"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class AgentResult:
    """Result from a single swarm agent."""
    role: str
    agent_id: str
    content: str = ""
    error: Optional[str] = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "agent_id": self.agent_id,
            "content": self.content[:2000] if self.content else "",
            "error": self.error,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class SwarmRun:
    """State container for a single swarm execution."""
    run_id: str
    task: str
    status: RunStatus = RunStatus.PENDING
    agent_results: Dict[str, AgentResult] = field(default_factory=dict)
    integrated_result: Optional[str] = None
    cost_estimate: CostEstimate = field(default_factory=CostEstimate)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    roles_requested: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "status": self.status.value,
            "agent_results": {k: v.to_dict() for k, v in self.agent_results.items()},
            "integrated_result": self.integrated_result,
            "cost_estimate": self.cost_estimate.to_dict(),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "roles_requested": self.roles_requested,
            "elapsed_s": round((self.completed_at or time.time()) - self.created_at, 2),
        }


# ---------------------------------------------------------------------------
# SwarmAgent — wraps single agent execution
# ---------------------------------------------------------------------------


class SwarmAgent:
    """Executes a single DeepSeek agent with role-specific injection."""

    def __init__(self, role, run_id: str, provider):
        from memory.providers.deepseek_provider import SwarmAgentRole
        self.role: SwarmAgentRole = role
        self.run_id = run_id
        self.agent_id = f"swarm-{role.name.lower()}-{run_id[:8]}"
        self.provider = provider

    async def execute(self, task: str, context_payload: dict) -> AgentResult:
        """Run this agent: build injection, call DeepSeek, return result."""
        from memory.providers.deepseek_provider import create_swarm_injection

        result = AgentResult(
            role=self.role.name,
            agent_id=self.agent_id,
        )

        try:
            messages = create_swarm_injection(
                role=self.role,
                task=task,
                context_payload=context_payload,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )

            t0 = time.monotonic()
            response = await self.provider.generate(
                messages=messages,
                max_tokens=2000,
                temperature=0.3,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            result.content = response.get("content", "")
            result.latency_ms = response.get("latency_ms", elapsed_ms)
            result.input_tokens = response.get("usage", {}).get("input_tokens", 0)
            result.output_tokens = response.get("usage", {}).get("output_tokens", 0)
            result.cost_usd = response.get("cost_estimate", 0.0)

        except Exception as e:
            result.error = f"{type(e).__name__}: {str(e)[:200]}"
            logger.error(f"Agent {self.agent_id} failed: {result.error}")

        return result


# ---------------------------------------------------------------------------
# SwarmController
# ---------------------------------------------------------------------------

# Default roles if none specified
_DEFAULT_ROLES_NAMES = [
    "CODE_ARCHAEOLOGIST",
    "PATCH_DRAFTER",
    "RISK_REVIEWER",
]


class SwarmController:
    """Orchestrates parallel DeepSeek agents for complex tasks.

    Lifecycle: create_run → fan_out → collect_results → integrate → done.
    Shortcut: execute() runs the full pipeline.
    """

    def __init__(self, bus=None, deepseek_provider=None):
        self._bus = bus
        self._provider = deepseek_provider
        self._runs: Dict[str, SwarmRun] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_bus(self):
        if self._bus is None:
            from memory.context_bus import get_context_bus
            self._bus = get_context_bus()
        return self._bus

    def _get_provider(self):
        if self._provider is None:
            from memory.providers.deepseek_provider import DeepSeekProvider
            self._provider = DeepSeekProvider()
        return self._provider

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._http_client

    async def close(self) -> None:
        """Clean up resources."""
        provider = self._provider
        if provider is not None:
            await provider.close()
        client = self._http_client
        if client is not None and not client.is_closed:
            await client.aclose()

    # -- Run lifecycle --

    async def create_run(self, task: str, context: Optional[dict] = None) -> SwarmRun:
        """Create a new swarm run and persist seed to ContextBus."""
        run_id = uuid.uuid4().hex[:12]
        run = SwarmRun(
            run_id=run_id,
            task=task,
            context=context or {},
        )
        self._runs[run_id] = run

        bus = self._get_bus()
        bus.put(
            f"swarm:{run_id}:seed",
            {"task": task, "context": context or {}, "created_at": run.created_at},
            ttl=3600,
        )

        logger.info(f"Swarm run {run_id} created: {task[:80]}")
        return run

    async def _query_librarian(self, task: str, intent: str = "locate") -> dict:
        """Query the Repo Librarian for codebase context.

        Returns a dict suitable for create_swarm_injection's context_payload.
        Falls back to empty context if Librarian is unreachable.
        """
        payload = {
            "agent_id": "swarm-controller",
            "intent": intent,
            "query": task,
            "max_files": 15,
            "max_snippets": 5,
        }

        try:
            client = await self._get_http()
            resp = await client.post(LIBRARIAN_URL, json=payload)
            if resp.status_code != 200:
                logger.warning(f"Librarian returned {resp.status_code}, using empty context")
                return {}
            data = resp.json()
        except Exception as e:
            logger.warning(f"Librarian unreachable ({e}), using empty context")
            return {}

        # Transform Librarian response into swarm context_payload format
        file_tree = [f.get("path", "") for f in data.get("files", [])]
        symbols = [
            f"{f.get('summary', '')} @ {f.get('path', '')}"
            for f in data.get("files", [])
        ]
        snippets = data.get("snippets", [])
        code_context = [
            f"{s.get('file', '')}:{s.get('line_start', 0)}-{s.get('line_end', 0)}\n{s.get('content', '')}"
            for s in snippets
        ]
        sops = data.get("related_sops", [])
        patterns = [f"{s.get('title', '')}: {s.get('summary', '')}" for s in sops]

        return {
            "file_tree": file_tree,
            "symbols": symbols,
            "conventions": {"code_snippets": "\n---\n".join(code_context[:3])},
            "architecture_decisions": [],
            "patterns": patterns,
        }

    async def fan_out(
        self,
        run: SwarmRun,
        roles: List[Any],
    ) -> None:
        """Dispatch agents in parallel. Each agent gets Librarian context + DeepSeek."""
        from memory.providers.deepseek_provider import SwarmAgentRole

        run.status = RunStatus.RUNNING
        run.roles_requested = [r.name for r in roles]
        provider = self._get_provider()
        bus = self._get_bus()

        # Get codebase context once, share across all agents
        context_payload = await self._query_librarian(run.task)
        # Merge user-supplied context
        if run.context:
            for key, val in run.context.items():
                if key not in context_payload:
                    context_payload[key] = val
                elif isinstance(context_payload[key], list) and isinstance(val, list):
                    context_payload[key].extend(val)

        # Build agents
        agents = [SwarmAgent(role, run.run_id, provider) for role in roles]

        # Fan-out: run all agents concurrently
        async def _run_agent(agent: SwarmAgent) -> AgentResult:
            logger.info(f"Swarm agent {agent.agent_id} starting ({agent.role.name})")
            result = await agent.execute(run.task, context_payload)
            # Publish to bus
            bus.publish(
                f"swarm:{run.run_id}:results",
                {
                    "agent_id": agent.agent_id,
                    "role": agent.role.name,
                    "status": "error" if result.error else "complete",
                    "latency_ms": result.latency_ms,
                },
            )
            # Store full result in KV
            bus.put(
                f"swarm:{run.run_id}:agent:{agent.agent_id}:result",
                result.to_dict(),
                ttl=3600,
            )
            return result

        results = await asyncio.gather(
            *[_run_agent(agent) for agent in agents],
            return_exceptions=True,
        )

        # Collect results, handling exceptions from gather
        for i, res in enumerate(results):
            agent = agents[i]
            if isinstance(res, Exception):
                ar = AgentResult(
                    role=agent.role.name,
                    agent_id=agent.agent_id,
                    error=f"{type(res).__name__}: {str(res)[:200]}",
                )
                run.agent_results[agent.agent_id] = ar
                logger.error(f"Agent {agent.agent_id} exception: {res}")
            else:
                run.agent_results[agent.agent_id] = res
                # Track cost
                run.cost_estimate.add(
                    res.input_tokens, res.output_tokens, res.cost_usd,
                )

        run.status = RunStatus.COLLECTING
        logger.info(
            f"Swarm run {run.run_id} fan-out complete: "
            f"{sum(1 for r in run.agent_results.values() if not r.error)}/{len(roles)} succeeded"
        )

    async def collect_results(self, run: SwarmRun, timeout: float = 120) -> Dict[str, AgentResult]:
        """Gather all agent results. If fan_out was called, results are already in run.agent_results.

        This method exists for recovery scenarios where results are in ContextBus
        but not in memory (e.g., after a process restart).
        """
        bus = self._get_bus()

        # If we already have results in memory, return them
        if run.agent_results:
            return run.agent_results

        # Recovery path: read from ContextBus KV
        deadline = time.time() + timeout
        while time.time() < deadline:
            keys = bus.list_keys(f"swarm:{run.run_id}:agent:")
            result_keys = [k for k in keys if k.endswith(":result")]

            if len(result_keys) >= len(run.roles_requested):
                break
            await asyncio.sleep(2)

        for key in bus.list_keys(f"swarm:{run.run_id}:agent:"):
            if not key.endswith(":result"):
                continue
            data = bus.get(key)
            if data:
                ar = AgentResult(
                    role=data.get("role", "unknown"),
                    agent_id=data.get("agent_id", "unknown"),
                    content=data.get("content", ""),
                    error=data.get("error"),
                    latency_ms=data.get("latency_ms", 0),
                    input_tokens=data.get("input_tokens", 0),
                    output_tokens=data.get("output_tokens", 0),
                    cost_usd=data.get("cost_usd", 0.0),
                )
                run.agent_results[ar.agent_id] = ar

        return run.agent_results

    async def integrate(self, run: SwarmRun) -> str:
        """Synthesize agent results using a premium integrator.

        Primary: local Qwen3 via llm_priority_queue (free, fast).
        Fallback: DeepSeek reasoner model ($0.55/$2.19 per 1M).
        """
        run.status = RunStatus.INTEGRATING

        successful = {
            aid: ar for aid, ar in run.agent_results.items() if not ar.error
        }
        failed = {
            aid: ar for aid, ar in run.agent_results.items() if ar.error
        }

        if not successful:
            run.status = RunStatus.FAILED
            run.error = "All agents failed"
            msg = "All agents failed:\n" + "\n".join(
                f"- {ar.agent_id} ({ar.role}): {ar.error}" for ar in failed.values()
            )
            run.integrated_result = msg
            return msg

        # Build integration prompt
        agent_outputs = []
        for aid, ar in successful.items():
            agent_outputs.append(
                f"### Agent: {ar.role} ({ar.agent_id})\n"
                f"Latency: {ar.latency_ms}ms | Tokens: {ar.input_tokens}+{ar.output_tokens}\n\n"
                f"{ar.content}\n"
            )
        if failed:
            agent_outputs.append(
                "### Failed Agents (partial results)\n" +
                "\n".join(f"- {ar.agent_id} ({ar.role}): {ar.error}" for ar in failed.values())
            )

        combined = "\n---\n".join(agent_outputs)

        system_prompt = (
            "You are a senior software engineer integrating analysis from multiple AI agents. "
            "Each agent had a specific role (code archaeologist, patch drafter, test writer, "
            "risk reviewer, performance reviewer). Your job:\n\n"
            "1. Synthesize their outputs into a coherent, actionable response.\n"
            "2. Resolve any conflicts between agents (e.g., one suggests X, another warns against it).\n"
            "3. Prioritize by impact: critical risks > patches > tests > nice-to-haves.\n"
            "4. Output a structured markdown response with clear sections.\n"
            "5. If agents disagree, state the disagreement and your recommendation.\n"
            "6. Be concise. Every sentence must add value.\n"
        )

        user_prompt = (
            f"## Task\n{run.task}\n\n"
            f"## Agent Results ({len(successful)} succeeded, {len(failed)} failed)\n\n"
            f"{combined}\n\n"
            f"## Instructions\n"
            f"Synthesize the above into a single actionable response. "
            f"Start with a 2-sentence summary, then provide structured details."
        )

        integrated = None

        # Primary: local Qwen3 via llm_priority_queue
        try:
            from memory.llm_priority_queue import butler_query
            integrated = butler_query(system_prompt, user_prompt, profile="deep")
        except Exception as e:
            logger.warning(f"Local LLM integration failed: {e}")

        # Fallback: DeepSeek reasoner
        if not integrated:
            logger.info("Falling back to DeepSeek reasoner for integration")
            try:
                provider = self._get_provider()
                resp = await provider.generate(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model="deepseek-reasoner",
                    max_tokens=3000,
                    temperature=0.4,
                )
                integrated = resp.get("content", "")
                run.cost_estimate.add(
                    resp.get("usage", {}).get("input_tokens", 0),
                    resp.get("usage", {}).get("output_tokens", 0),
                    resp.get("cost_estimate", 0.0),
                )
            except Exception as e:
                logger.error(f"DeepSeek reasoner integration also failed: {e}")
                integrated = (
                    f"Integration failed (both local LLM and DeepSeek reasoner unavailable).\n\n"
                    f"Raw agent outputs:\n\n{combined}"
                )

        run.integrated_result = integrated
        run.status = RunStatus.COMPLETE
        run.completed_at = time.time()

        # Persist final result to bus
        bus = self._get_bus()
        bus.put(
            f"swarm:{run.run_id}:integrated",
            {
                "result": integrated[:5000],
                "cost": run.cost_estimate.to_dict(),
                "completed_at": run.completed_at,
            },
            ttl=7200,
        )
        bus.publish(
            f"swarm:{run.run_id}:results",
            {"status": "complete", "run_id": run.run_id},
        )

        logger.info(
            f"Swarm run {run.run_id} complete. "
            f"Cost: ${run.cost_estimate.total_usd:.4f} | "
            f"Elapsed: {run.completed_at - run.created_at:.1f}s"
        )
        return integrated

    async def execute(
        self,
        task: str,
        context: Optional[dict] = None,
        roles: Optional[list] = None,
    ) -> SwarmRun:
        """Full pipeline: create → fan_out → collect → integrate.

        Args:
            task: Natural language description of the coding task.
            context: Optional dict with extra context (focus_dirs, constraints, etc.).
            roles: List of SwarmAgentRole enums. Defaults to ARCHAEOLOGIST + DRAFTER + RISK.

        Returns:
            Completed SwarmRun with integrated_result.
        """
        from memory.providers.deepseek_provider import SwarmAgentRole

        if roles is None:
            roles = [SwarmAgentRole[name] for name in _DEFAULT_ROLES_NAMES]

        run = await self.create_run(task, context)

        try:
            await self.fan_out(run, roles)
            await self.collect_results(run)
            await self.integrate(run)
        except Exception as e:
            run.status = RunStatus.FAILED
            run.error = f"{type(e).__name__}: {str(e)[:300]}"
            run.completed_at = time.time()
            logger.error(f"Swarm run {run.run_id} failed: {run.error}", exc_info=True)

        return run

    def get_run(self, run_id: str) -> Optional[SwarmRun]:
        """Get a run by ID from in-memory cache."""
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 20) -> List[dict]:
        """List recent runs, newest first."""
        runs = sorted(
            self._runs.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )
        return [r.to_dict() for r in runs[:limit]]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_controller: Optional[SwarmController] = None


def get_swarm_controller() -> SwarmController:
    """Get or create the singleton SwarmController."""
    global _controller
    if _controller is None:
        _controller = SwarmController()
    return _controller


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


if FASTAPI_AVAILABLE:

    class SwarmRunRequest(BaseModel):
        task: str = Field(..., description="Task description for the swarm")
        context: Optional[Dict[str, Any]] = Field(default=None, description="Extra context")
        roles: Optional[List[str]] = Field(
            default=None,
            description="Agent roles (CODE_ARCHAEOLOGIST, PATCH_DRAFTER, TEST_WRITER, RISK_REVIEWER, PERFORMANCE_REVIEWER)",
        )

    class SwarmRunResponse(BaseModel):
        run_id: str
        status: str
        task: str
        message: str

    def create_router() -> APIRouter:
        """Create the FastAPI router for the Swarm Controller."""
        router = APIRouter(prefix="/v1/swarm", tags=["swarm"])

        @router.post("/run", response_model=SwarmRunResponse)
        async def start_swarm_run(req: SwarmRunRequest) -> SwarmRunResponse:
            """Start a new swarm run. Returns immediately with run_id.

            The swarm executes asynchronously. Poll GET /v1/swarm/run/{run_id}
            for status and results.
            """
            from memory.providers.deepseek_provider import SwarmAgentRole

            controller = get_swarm_controller()

            # Parse role strings to enums
            parsed_roles = None
            if req.roles:
                try:
                    parsed_roles = [SwarmAgentRole[name.upper()] for name in req.roles]
                except KeyError as e:
                    valid = [r.name for r in SwarmAgentRole]
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid role: {e}. Valid roles: {valid}",
                    )

            # Create run and launch in background
            run = await controller.create_run(req.task, req.context)

            async def _background_execute():
                try:
                    from memory.providers.deepseek_provider import SwarmAgentRole as SAR
                    roles = parsed_roles or [SAR[n] for n in _DEFAULT_ROLES_NAMES]
                    await controller.fan_out(run, roles)
                    await controller.collect_results(run)
                    await controller.integrate(run)
                except Exception as e:
                    run.status = RunStatus.FAILED
                    run.error = str(e)[:300]
                    run.completed_at = time.time()
                    logger.error(f"Background swarm run {run.run_id} failed: {e}")

            asyncio.ensure_future(_background_execute())

            return SwarmRunResponse(
                run_id=run.run_id,
                status=run.status.value,
                task=req.task[:200],
                message=f"Swarm run started with {len(parsed_roles or _DEFAULT_ROLES_NAMES)} agents",
            )

        @router.get("/run/{run_id}")
        async def get_swarm_run(run_id: str):
            """Get status and results of a swarm run."""
            controller = get_swarm_controller()
            run = controller.get_run(run_id)

            if run is None:
                # Try recovering from ContextBus
                bus = controller._get_bus()
                seed = bus.get(f"swarm:{run_id}:seed")
                if seed is None:
                    raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

                integrated = bus.get(f"swarm:{run_id}:integrated")
                return {
                    "run_id": run_id,
                    "status": "complete" if integrated else "unknown",
                    "task": seed.get("task", ""),
                    "integrated_result": integrated.get("result") if integrated else None,
                    "cost_estimate": integrated.get("cost") if integrated else None,
                    "source": "context_bus_recovery",
                }

            return run.to_dict()

        @router.get("/runs")
        async def list_swarm_runs(limit: int = 20):
            """List recent swarm runs."""
            controller = get_swarm_controller()
            return {"runs": controller.list_runs(limit=limit)}

        @router.get("/health")
        async def swarm_health():
            """Swarm system health check."""
            controller = get_swarm_controller()

            # Check DeepSeek availability
            deepseek_ok = False
            deepseek_error = None
            try:
                import os
                key = os.environ.get("Context_DNA_Deepseek", "")
                deepseek_ok = len(key) > 10
                if not deepseek_ok:
                    deepseek_error = "Context_DNA_Deepseek not set or too short"
            except Exception as e:
                deepseek_error = str(e)

            # Check Librarian availability
            librarian_ok = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                    resp = await client.get("http://127.0.0.1:8080/v1/context/health")
                    librarian_ok = resp.status_code == 200
            except Exception:
                pass

            # Check local LLM via Redis health cache (no direct HTTP to 5044)
            local_llm_ok = False
            try:
                from memory.llm_priority_queue import check_llm_health
                local_llm_ok = check_llm_health()
            except Exception:
                pass

            # Check ContextBus
            bus_type = type(controller._get_bus()).__name__

            active_runs = sum(
                1 for r in controller._runs.values()
                if r.status in (RunStatus.RUNNING, RunStatus.COLLECTING, RunStatus.INTEGRATING)
            )

            return {
                "status": "ok" if deepseek_ok else "degraded",
                "deepseek_api": {"available": deepseek_ok, "error": deepseek_error},
                "librarian": {"available": librarian_ok},
                "local_llm": {"available": local_llm_ok, "purpose": "integration"},
                "context_bus": {"backend": bus_type},
                "active_runs": active_runs,
                "total_runs": len(controller._runs),
            }

        return router

    router = create_router()


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "health":
        # Quick health check without FastAPI
        import os
        key = os.environ.get("Context_DNA_Deepseek", "")
        print(f"DeepSeek API key: {'set (' + key[:8] + '...)' if key else 'NOT SET'}")

        try:
            from memory.llm_priority_queue import check_llm_health
            llm_ok = check_llm_health()
            print(f"Local LLM (5044): {'OK' if llm_ok else 'DOWN (Redis health cache)'}")
        except Exception as e:
            print(f"Local LLM (5044): DOWN ({e})")

        try:
            import requests as sync_requests
            r = sync_requests.get("http://127.0.0.1:8080/v1/context/health", timeout=3)
            print(f"Librarian (8080): {'OK' if r.ok else f'HTTP {r.status_code}'}")
        except Exception as e:
            print(f"Librarian (8080): DOWN ({e})")

        from memory.context_bus import get_context_bus
        bus = get_context_bus()
        print(f"ContextBus: {type(bus).__name__}")

    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # Run a real swarm test
        task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Find all error handling patterns in memory/*.py"
        print(f"Running swarm test: {task}")

        async def _test():
            controller = SwarmController()
            try:
                from memory.providers.deepseek_provider import SwarmAgentRole
                run = await controller.execute(
                    task=task,
                    roles=[SwarmAgentRole.CODE_ARCHAEOLOGIST, SwarmAgentRole.RISK_REVIEWER],
                )
                print(f"\n{'='*60}")
                print(f"Run ID: {run.run_id}")
                print(f"Status: {run.status.value}")
                print(f"Cost: ${run.cost_estimate.total_usd:.4f}")
                print(f"Elapsed: {(run.completed_at or time.time()) - run.created_at:.1f}s")
                print(f"{'='*60}")

                for aid, ar in run.agent_results.items():
                    status = "ERROR" if ar.error else "OK"
                    print(f"\n[{ar.role}] {status} ({ar.latency_ms}ms, ${ar.cost_usd:.4f})")
                    if ar.error:
                        print(f"  Error: {ar.error}")
                    else:
                        print(f"  Output: {ar.content[:200]}...")

                print(f"\n{'='*60}")
                print("INTEGRATED RESULT:")
                print(f"{'='*60}")
                print(run.integrated_result or "(none)")
            finally:
                await controller.close()

        asyncio.run(_test())

    else:
        print("Swarm Controller — DeepSeek agent orchestration for ContextDNA")
        print()
        print("Usage:")
        print("  python swarm_controller.py health          # Check dependencies")
        print("  python swarm_controller.py test [task]      # Run a test swarm")
        print()
        print("As FastAPI router:")
        print("  POST /v1/swarm/run       — Start a swarm run")
        print("  GET  /v1/swarm/run/{id}  — Get status/results")
        print("  GET  /v1/swarm/runs      — List runs")
        print("  GET  /v1/swarm/health    — System health")
