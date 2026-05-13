#!/usr/bin/env python3
"""
ContextDNA Engine MCP Server

Diagnostic/testing tools for the ContextDNA TypeScript injection engine.
Wraps context-dna/engine/builder/_diagnostics.ts via subprocess, exposing
8 tools for health checks, testing, tracing, and debugging.

Tools:
  engine_health       — Quick health check of all engine modules
  engine_run_tests    — Run test suite (all or specific module)
  engine_pipeline_trace — Full pipeline trace with per-stage debug
  engine_quality_check  — Evaluate quality gate on sample data
  engine_choke_inspect  — Test outbound choke + hash determinism
  engine_wal_verify     — Verify WAL chain hash integrity
  engine_templates      — Dump all section templates with config
  engine_ledger_sim     — Simulate delivery ledger operations

MCP Configuration (.mcp.json):
    "contextdna-engine": {
      "command": "python3",
      "args": ["/path/to/mcp-servers/contextdna_engine_mcp.py"],
      "env": {"REPO_ROOT": "/repo/root"}
    }
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).parent.parent)))
ENGINE_DIR = REPO_ROOT / "context-dna" / "engine" / "builder"
DIAGNOSTICS_TS = ENGINE_DIR / "_diagnostics.ts"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("contextdna-engine-mcp")

# ---------------------------------------------------------------------------
# Subprocess bridge to _diagnostics.ts
# ---------------------------------------------------------------------------

def _run_diagnostic(command: str, args: dict | None = None, timeout: float = 60.0) -> dict:
    """Run a diagnostic command via npx tsx and return parsed JSON."""
    cmd = ["npx", "tsx", str(DIAGNOSTICS_TS), command]
    if args:
        cmd.append(json.dumps(args))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ENGINE_DIR),
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            return {
                "error": f"Diagnostic exited with code {result.returncode}",
                "stderr": stderr[-500:] if stderr else None,
                "stdout": stdout[-500:] if stdout else None,
            }

        # Parse JSON from stdout (skip any non-JSON preamble lines)
        json_start = stdout.find("{")
        if json_start == -1:
            return {"error": "No JSON in output", "raw": stdout[-500:]}

        return json.loads(stdout[json_start:])

    except subprocess.TimeoutExpired:
        return {"error": f"Diagnostic timed out after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON from diagnostic: {e}", "raw": stdout[-300:] if stdout else None}
    except FileNotFoundError:
        return {"error": "npx not found — ensure Node.js is installed and in PATH"}
    except Exception as e:
        return {"error": f"Subprocess error: {e}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

def handle_health(arguments: dict) -> dict:
    """Quick health check of all engine modules."""
    return _run_diagnostic("health")


def handle_run_tests(arguments: dict) -> dict:
    """Run engine test suite. Optional module filter."""
    args = {}
    module = arguments.get("module")
    if module:
        args["module"] = module
    return _run_diagnostic("run-tests", args if args else None, timeout=120.0)


def handle_pipeline_trace(arguments: dict) -> dict:
    """Full pipeline trace with per-stage debug output."""
    args = {}
    depth = arguments.get("depth")
    turn = arguments.get("turn")
    if depth:
        args["depth"] = depth
    if turn is not None:
        args["turn"] = turn
    return _run_diagnostic("pipeline-trace", args if args else None)


def handle_quality_check(arguments: dict) -> dict:
    """Evaluate quality gate on sample or provided sections."""
    args = {}
    sections = arguments.get("sections")
    if sections:
        args["sections"] = sections
    return _run_diagnostic("quality-check", args if args else None)


def handle_choke_inspect(arguments: dict) -> dict:
    """Test outbound choke — hash determinism, envelope validation."""
    args = {}
    depth = arguments.get("depth")
    project_id = arguments.get("projectId")
    if depth:
        args["depth"] = depth
    if project_id:
        args["projectId"] = project_id
    return _run_diagnostic("choke-inspect", args if args else None)


def handle_wal_verify(arguments: dict) -> dict:
    """Verify WAL chain hash integrity."""
    return _run_diagnostic("wal-verify")


def handle_templates(arguments: dict) -> dict:
    """Dump all section templates with budget config."""
    return _run_diagnostic("templates")


def handle_ledger_sim(arguments: dict) -> dict:
    """Simulate delivery ledger operations."""
    args = {}
    scenario = arguments.get("scenario")
    if scenario:
        args["scenario"] = scenario
    return _run_diagnostic("ledger-sim", args if args else None)


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "engine_health",
        "description": "Quick health check of all ContextDNA engine modules (template-loader, quality-gate, outbound-choke, wal-evidence, delivery-ledger, context-builder, ranker-budget). Returns module status and import verification.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "engine_run_tests",
        "description": "Run the ContextDNA engine test suite. Returns pass/fail counts per file. Optionally filter to a specific module (e.g., 'choke', 'quality', 'wal', 'builder', 'ledger', 'concurrency').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": "Optional module filter: choke, quality, wal, builder, ledger, concurrency. Omit to run all.",
                },
            },
        },
    },
    {
        "name": "engine_pipeline_trace",
        "description": "Full pipeline trace through the ContextDNA injection engine. Shows gather → filter → rank/fit → selfref → assemble → manifest stages with timing and health status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "depth": {
                    "type": "string",
                    "enum": ["FULL", "ABBREVIATED"],
                    "description": "Injection depth. Default: auto-detected from turn number.",
                },
                "turn": {
                    "type": "number",
                    "description": "Simulated turn number (affects depth cycling). Default: 3.",
                },
            },
        },
    },
    {
        "name": "engine_quality_check",
        "description": "Evaluate the quality gate on sample or provided section data. Returns verdict (OK/DEGRADED/BLOCKED), overall score, per-dimension scores, and any block reasons.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "number",
                    "description": "Number of sample sections to generate for evaluation. Default: 5.",
                },
            },
        },
    },
    {
        "name": "engine_choke_inspect",
        "description": "Test the outbound choke point — assembles sections, computes payload hash, verifies hash determinism (same input → same hash). Optionally test with specific depth or project ID for multi-project isolation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "depth": {
                    "type": "string",
                    "enum": ["FULL", "ABBREVIATED"],
                    "description": "Injection depth. Default: FULL.",
                },
                "projectId": {
                    "type": "string",
                    "description": "Project ID for envelope scoping. Default: 'diag-project'.",
                },
            },
        },
    },
    {
        "name": "engine_wal_verify",
        "description": "Verify WAL (Write-Ahead Log) chain hash integrity. Records sample entries, verifies the hash chain is unbroken, and shows sample entries with parent hash linkage.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "engine_templates",
        "description": "Dump all section templates with configuration — assembly order, header templates, strip priority, alwaysFull/requiresLlm flags, and payload budget limits (FULL/ABBREVIATED).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "engine_ledger_sim",
        "description": "Simulate delivery ledger operations — records deliveries into the ring buffer, tests wrap behavior, and reports per-project/destination stats. Use 'stress' scenario for high-volume simulation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {
                    "type": "string",
                    "enum": ["basic", "stress"],
                    "description": "Simulation scenario. 'basic' = 10 records, 'stress' = 100 records with failures. Default: stress.",
                },
            },
        },
    },
]

TOOL_DISPATCH = {
    "engine_health": handle_health,
    "engine_run_tests": handle_run_tests,
    "engine_pipeline_trace": handle_pipeline_trace,
    "engine_quality_check": handle_quality_check,
    "engine_choke_inspect": handle_choke_inspect,
    "engine_wal_verify": handle_wal_verify,
    "engine_templates": handle_templates,
    "engine_ledger_sim": handle_ledger_sim,
}


# ---------------------------------------------------------------------------
# MCP Protocol Handler
# ---------------------------------------------------------------------------

class ContextDNAEngineMCP:
    """MCP server for ContextDNA TypeScript engine diagnostics."""

    def __init__(self):
        self.name = "contextdna-engine"
        self.version = "1.0.0"

    async def handle_message(self, message: dict) -> dict | None:
        """Handle MCP JSON-RPC messages."""
        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")

        try:
            if method == "initialize":
                return self._ok(msg_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {"name": self.name, "version": self.version},
                })

            elif method == "notifications/initialized":
                return None

            elif method == "tools/list":
                return self._ok(msg_id, {"tools": TOOLS})

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                handler = TOOL_DISPATCH.get(tool_name)
                if not handler:
                    return self._error(msg_id, -32602, f"Unknown tool: {tool_name}")

                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, handler, arguments),
                    timeout=120.0,  # Tests can take longer
                )

                is_error = isinstance(result, dict) and "error" in result
                return self._ok(msg_id, {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(result, indent=2, default=str),
                    }],
                    "isError": is_error,
                })

            else:
                return self._error(msg_id, -32601, f"Method not found: {method}")

        except asyncio.TimeoutError:
            return self._error(msg_id, -32603, "Tool execution timed out (120s)")
        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return self._error(msg_id, -32603, f"Internal error: {e}")

    @staticmethod
    def _ok(msg_id, result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    async def run(self):
        """Run MCP server on stdio (JSON-RPC over stdin/stdout)."""
        logger.info(f"{self.name} v{self.version} starting...")

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break

                message = json.loads(line.decode().strip())
                response = await self.handle_message(message)

                if response is not None:
                    print(json.dumps(response), flush=True)

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
                continue
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                continue


if __name__ == "__main__":
    server = ContextDNAEngineMCP()
    asyncio.run(server.run())
