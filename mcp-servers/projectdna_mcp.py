#!/usr/bin/env python3
"""
ProjectDNA MCP Server (Movement 7)

Tool layer for .projectdna/ vault operations. Exposes 7 tools:
  read          — Read any .projectdna file
  write         — Write to .projectdna (gated via EventedWriteService)
  search        — Search within .projectdna content
  propose_patch — Propose changes to vault files (inbox → review)
  ingest_thread — Ingest conversation thread into raw/imports/
  refresh_twin  — Trigger architecture twin refresh
  promote       — Promote learnings/decisions to derived/ docs

All writes chain-hashed via EventedWriteService. Self-reference filter
applied to output when product_mode=true. Scope isolation: only .projectdna/.

MCP Configuration (.mcp.json):
    "projectdna": {
      "command": "python3",
      "args": ["/path/to/mcp-servers/projectdna_mcp.py"],
      "env": {"PYTHONPATH": "/repo/root", "REPO_ROOT": "/repo/root"}
    }
"""

import asyncio
import json
import logging
import os
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

VAULT_ROOT = REPO_ROOT / ".projectdna"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("projectdna-mcp")

# --- Scope Isolation ---

def _validate_path(path_str: str) -> Path:
    """Resolve path and ensure it's within .projectdna/. Raises ValueError if not."""
    # Allow both relative (from vault root) and absolute
    if path_str.startswith("/"):
        resolved = Path(path_str).resolve()
    else:
        resolved = (VAULT_ROOT / path_str).resolve()

    vault_resolved = VAULT_ROOT.resolve()
    if not str(resolved).startswith(str(vault_resolved)):
        raise ValueError(f"Path escapes vault: {path_str}")
    return resolved


def _rel_path(absolute: Path) -> str:
    """Return path relative to vault root."""
    return str(absolute.relative_to(VAULT_ROOT.resolve()))


# --- Self-Reference Filter ---

def _filter_output(text: str) -> str:
    """Apply self-reference filter if product_mode=true."""
    try:
        from memory.self_reference_filter import filter_content
        return filter_content(text)
    except Exception:
        return text


# --- Event Logging ---

def _log_event(store: str, method: str, summary: dict):
    """Log a chain-hashed event via EventedWriteService."""
    try:
        from memory.evented_write import EventedWriteService
        ews = EventedWriteService.get_instance()
        ews._append_event(store, method, summary)
    except Exception as e:
        logger.warning(f"Event logging failed (non-blocking): {e}")


# --- JSONL Event Emission (for Electron CapabilityBus bridge) ---

EVENTS_JSONL = VAULT_ROOT / ".events.jsonl"


def _emit_jsonl_event(event_type: str, **kwargs):
    """Append a JSONL event to .projectdna/.events.jsonl for Electron consumption."""
    try:
        event = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "mcp-server",
            **kwargs,
        }
        with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        logger.warning(f"JSONL event emission failed (non-blocking): {e}")


# --- 7 Tool Implementations ---

def tool_read(path: str) -> dict:
    """Read a .projectdna file."""
    try:
        resolved = _validate_path(path)
        if not resolved.exists():
            return {"error": f"Not found: {_rel_path(resolved)}"}
        if resolved.is_dir():
            entries = []
            for p in sorted(resolved.iterdir()):
                kind = "dir" if p.is_dir() else "file"
                size = p.stat().st_size if p.is_file() else 0
                entries.append({"name": p.name, "type": kind, "size": size})
            return {"path": _rel_path(resolved), "type": "directory", "entries": entries}
        content = resolved.read_text(encoding="utf-8", errors="replace")
        # Truncate very large files
        if len(content) > 50_000:
            content = content[:50_000] + f"\n\n... [truncated at 50K chars, full size: {resolved.stat().st_size}]"
        return {
            "path": _rel_path(resolved),
            "type": "file",
            "size": resolved.stat().st_size,
            "content": _filter_output(content),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Read failed: {e}"}


def tool_write(path: str, content: str, reason: str = "") -> dict:
    """Write to a .projectdna file (event-logged)."""
    try:
        resolved = _validate_path(path)
        rel = _rel_path(resolved)

        # Prevent writing to events.jsonl (append-only via EventedWriteService)
        if resolved.name == "events.jsonl":
            return {"error": "events.jsonl is append-only via EventedWriteService"}

        # Create parent dirs
        resolved.parent.mkdir(parents=True, exist_ok=True)

        existed = resolved.exists()
        old_hash = ""
        if existed:
            old_content = resolved.read_bytes()
            old_hash = hashlib.md5(old_content).hexdigest()[:8]

        resolved.write_text(content, encoding="utf-8")
        new_hash = hashlib.md5(content.encode()).hexdigest()[:8]

        _log_event("projectdna_vault", "write", {
            "path": rel,
            "action": "update" if existed else "create",
            "old_hash": old_hash,
            "new_hash": new_hash,
            "reason": reason,
            "size": len(content),
        })

        _emit_jsonl_event("vault.file.changed", path=rel)

        return {
            "path": rel,
            "action": "updated" if existed else "created",
            "size": len(content),
            "hash": new_hash,
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Write failed: {e}"}


def tool_search(query: str, glob_pattern: str = "**/*") -> dict:
    """Search within .projectdna content."""
    import re
    try:
        results = []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        vault = VAULT_ROOT.resolve()

        for p in vault.glob(glob_pattern):
            if not p.is_file():
                continue
            # Skip binary/large files
            if p.stat().st_size > 500_000 or p.suffix in (".db", ".sqlite", ".bak"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            matches = []
            for i, line in enumerate(text.split("\n"), 1):
                if pattern.search(line):
                    matches.append({"line": i, "text": line.strip()[:200]})
                    if len(matches) >= 5:
                        break
            if matches:
                results.append({
                    "path": _rel_path(p),
                    "matches": matches,
                })
            if len(results) >= 20:
                break

        return {
            "query": query,
            "results_count": len(results),
            "results": [
                {**r, "matches": [
                    {**m, "text": _filter_output(m["text"])} for m in r["matches"]
                ]} for r in results
            ],
        }
    except Exception as e:
        return {"error": f"Search failed: {e}"}


def tool_propose_patch(path: str, patch_content: str, description: str) -> dict:
    """Propose a change to a vault file — writes to inbox/ for review."""
    try:
        _validate_path(path)  # Validate target exists in vault scope
        rel = path if not path.startswith("/") else _rel_path(Path(path).resolve())

        inbox_dir = VAULT_ROOT / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_name = rel.replace("/", "_").replace(".", "_")
        patch_file = inbox_dir / f"patch_{safe_name}_{ts}.json"

        patch_data = {
            "target": rel,
            "description": description,
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending_review",
            "content": patch_content,
        }
        patch_file.write_text(json.dumps(patch_data, indent=2), encoding="utf-8")

        _log_event("projectdna_vault", "propose_patch", {
            "target": rel,
            "patch_file": patch_file.name,
            "description": description[:200],
        })

        _emit_jsonl_event("vault.file.changed", path=rel)

        return {
            "patch_file": str(patch_file.relative_to(VAULT_ROOT)),
            "target": rel,
            "status": "pending_review",
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Propose patch failed: {e}"}


def tool_ingest_thread(thread_content: str, source: str = "unknown", title: str = "") -> dict:
    """Ingest a conversation thread into raw/imports/."""
    try:
        imports_dir = VAULT_ROOT / "raw" / "imports"
        imports_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_source = source.replace("/", "_").replace(" ", "_")[:30]
        filename = f"thread_{safe_source}_{ts}.md"

        header = f"# Imported Thread: {title or source}\n"
        header += f"> Source: {source}\n"
        header += f"> Imported: {datetime.now(timezone.utc).isoformat()}\n\n---\n\n"

        full_content = header + thread_content
        target = imports_dir / filename
        target.write_text(full_content, encoding="utf-8")

        content_hash = hashlib.md5(thread_content.encode()).hexdigest()[:8]

        _log_event("projectdna_vault", "ingest_thread", {
            "file": filename,
            "source": source,
            "title": title,
            "size": len(thread_content),
            "hash": content_hash,
        })

        return {
            "file": f"raw/imports/{filename}",
            "size": len(full_content),
            "hash": content_hash,
        }
    except Exception as e:
        return {"error": f"Ingest failed: {e}"}


def tool_refresh_twin() -> dict:
    """Trigger architecture twin refresh."""
    try:
        from memory.refresh_architecture_twin import refresh_architecture_twin
        result = refresh_architecture_twin()

        _log_event("projectdna_vault", "refresh_twin", {
            "nodes": result.get("nodes", 0),
            "edges": result.get("edges", 0),
            "gaps": result.get("gaps_found", 0),
        })

        _emit_jsonl_event("vault.twin.refreshed")

        return {
            "status": "refreshed",
            "nodes": result.get("nodes", 0),
            "edges": result.get("edges", 0),
            "gaps": result.get("gaps_found", []),
            "output": result.get("output_file", ""),
        }
    except Exception as e:
        return {"error": f"Twin refresh failed: {e}"}


def tool_promote(source_type: str, content: str, target_doc: str = "decisions.md") -> dict:
    """Promote a learning/decision to a derived/ document."""
    allowed_targets = {
        "decisions.md", "next-steps.md", "open-questions.md",
        "architecture.current.md", "architecture.planned.md", "spec.md",
    }
    if target_doc not in allowed_targets:
        return {"error": f"Target must be one of: {', '.join(sorted(allowed_targets))}"}

    try:
        target_path = VAULT_ROOT / "derived" / target_doc
        if not target_path.exists():
            return {"error": f"Target doc not found: derived/{target_doc}"}

        existing = target_path.read_text(encoding="utf-8")

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n### [{source_type.upper()}] Promoted {ts}\n{content}\n"

        target_path.write_text(existing + entry, encoding="utf-8")

        _log_event("projectdna_vault", "promote", {
            "source_type": source_type,
            "target": target_doc,
            "content_preview": content[:100],
        })

        return {
            "target": f"derived/{target_doc}",
            "source_type": source_type,
            "appended_chars": len(entry),
        }
    except Exception as e:
        return {"error": f"Promote failed: {e}"}


# --- Tool Registry ---

TOOLS = [
    {
        "name": "projectdna_read",
        "description": "Read a file or list a directory within .projectdna/ vault",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within .projectdna/ (e.g., 'manifest.yaml', 'derived/decisions.md')"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "projectdna_write",
        "description": "Write/update a file in .projectdna/ vault (event-logged, chain-hashed)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path within .projectdna/"},
                "content": {"type": "string", "description": "File content to write"},
                "reason": {"type": "string", "description": "Reason for the write (for audit trail)"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "projectdna_search",
        "description": "Search within .projectdna/ vault content by keyword",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (case-insensitive)"},
                "glob_pattern": {"type": "string", "description": "File glob pattern (default: **/*)", "default": "**/*"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "projectdna_propose_patch",
        "description": "Propose a change to a vault file — saved to inbox/ for human review",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path within .projectdna/"},
                "patch_content": {"type": "string", "description": "Proposed new content"},
                "description": {"type": "string", "description": "What this patch does and why"},
            },
            "required": ["path", "patch_content", "description"],
        },
    },
    {
        "name": "projectdna_ingest_thread",
        "description": "Import a conversation thread into .projectdna/raw/imports/ for later processing",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_content": {"type": "string", "description": "The conversation content to import"},
                "source": {"type": "string", "description": "Source identifier (e.g., 'claude-session-abc', 'slack-thread-123')"},
                "title": {"type": "string", "description": "Human-readable thread title"},
            },
            "required": ["thread_content", "source"],
        },
    },
    {
        "name": "projectdna_refresh_twin",
        "description": "Trigger architecture twin refresh — regenerates architecture.map.json from code analysis",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "projectdna_promote",
        "description": "Promote a learning, decision, or finding to a .projectdna/derived/ document",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string", "description": "Type: learning, decision, finding, question, sop", "enum": ["learning", "decision", "finding", "question", "sop"]},
                "content": {"type": "string", "description": "The content to promote"},
                "target_doc": {"type": "string", "description": "Target document in derived/ (default: decisions.md)", "default": "decisions.md",
                    "enum": ["decisions.md", "next-steps.md", "open-questions.md", "architecture.current.md", "architecture.planned.md", "spec.md"]},
            },
            "required": ["source_type", "content"],
        },
    },
]

TOOL_DISPATCH = {
    "projectdna_read": lambda args: tool_read(args["path"]),
    "projectdna_write": lambda args: tool_write(args["path"], args["content"], args.get("reason", "")),
    "projectdna_search": lambda args: tool_search(args["query"], args.get("glob_pattern", "**/*")),
    "projectdna_propose_patch": lambda args: tool_propose_patch(args["path"], args["patch_content"], args["description"]),
    "projectdna_ingest_thread": lambda args: tool_ingest_thread(args["thread_content"], args.get("source", "unknown"), args.get("title", "")),
    "projectdna_refresh_twin": lambda args: tool_refresh_twin(),
    "projectdna_promote": lambda args: tool_promote(args["source_type"], args["content"], args.get("target_doc", "decisions.md")),
}


# --- MCP Protocol Handler ---

class ProjectDNAMCP:
    """MCP server providing .projectdna/ vault operations."""

    def __init__(self):
        self.name = "projectdna"
        self.version = "1.0.0"

    async def handle_message(self, message: dict) -> dict:
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
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": self.name, "version": self.version},
                })

            elif method == "notifications/initialized":
                return None  # No response for notifications

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
                    timeout=30.0,
                )

                is_error = "error" in result
                return self._ok(msg_id, {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(result, indent=2, default=str),
                    }],
                    "isError": is_error,
                })

            elif method == "resources/list":
                return self._ok(msg_id, {"resources": [
                    {
                        "uri": "projectdna://vault-status",
                        "name": "ProjectDNA Vault Status",
                        "description": "Current vault health: file counts, event chain integrity, manifest summary",
                        "mimeType": "application/json",
                    },
                ]})

            elif method == "resources/read":
                uri = params.get("uri")
                if uri == "projectdna://vault-status":
                    status = self._vault_status()
                    return self._ok(msg_id, {
                        "contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(status, indent=2)}]
                    })
                return self._error(msg_id, -32602, f"Unknown resource: {uri}")

            else:
                return self._error(msg_id, -32601, f"Method not found: {method}")

        except asyncio.TimeoutError:
            return self._error(msg_id, -32603, "Tool execution timed out (30s)")
        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return self._error(msg_id, -32603, f"Internal error: {e}")

    def _vault_status(self) -> dict:
        """Generate vault health status."""
        vault = VAULT_ROOT.resolve()
        file_count = sum(1 for _ in vault.rglob("*") if _.is_file())
        dir_count = sum(1 for _ in vault.rglob("*") if _.is_dir())

        events_file = vault / "events.jsonl"
        event_count = 0
        if events_file.exists():
            event_count = sum(1 for _ in events_file.open())

        manifest_ok = (vault / "manifest.yaml").exists()

        # Check chain integrity (last 5 events)
        chain_ok = True
        try:
            from memory.evented_write import EventedWriteService
            ews = EventedWriteService.get_instance()
            verification = ews.verify_chain()
            chain_ok = verification.get("valid", False)
        except Exception:
            chain_ok = None  # Can't verify

        return {
            "vault_path": str(vault),
            "files": file_count,
            "directories": dir_count,
            "events": event_count,
            "manifest_present": manifest_ok,
            "chain_integrity": chain_ok,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

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
    server = ProjectDNAMCP()
    asyncio.run(server.run())
