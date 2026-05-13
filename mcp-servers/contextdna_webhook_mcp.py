#!/usr/bin/env python3
"""
Context DNA Webhook MCP Server

Provides per-message webhook context injection as an MCP resource.
This enables Cursor to fetch fresh context BEFORE EACH MESSAGE.

Architecture:
    User Types Message in Cursor
        ↓
    Cursor reads MCP resource: contextdna://webhook
        ↓
    This MCP server generates fresh 9-section payload
        ↓
    Context injected into message context
        ↓
    Agent receives: User message + Webhook payload

Usage in .cursorrules:
    Include this instruction:
    "Before processing ANY message, read the MCP resource: contextdna://webhook"

MCP Configuration (.mcp.json):
    {
      "mcpServers": {
        "contextdna-webhook": {
          "command": "python3",
          "args": ["/path/to/mcp-servers/contextdna_webhook_mcp.py"]
        }
      }
    }
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add repo root to path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ContextDNAWebhookMCP:
    """MCP server providing webhook context as a resource."""
    
    def __init__(self):
        self.name = "contextdna-webhook"
        self.version = "2.0.0"
    
    async def list_resources(self) -> dict:
        """List available MCP resources."""
        return {
            "resources": [
                {
                    "uri": "contextdna://webhook",
                    "name": "Context DNA Webhook Injection",
                    "description": "9-section webhook payload with session recovery, Professor wisdom, and 8th Intelligence",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "contextdna://session-recovery",
                    "name": "Session Crash Recovery",
                    "description": "Last 5 user/assistant messages from session historian",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "contextdna://professor",
                    "name": "Professor Wisdom",
                    "description": "LLM-generated domain-specific guidance",
                    "mimeType": "text/markdown"
                },
                {
                    "uri": "contextdna://8th-intelligence",
                    "name": "Synaptic's 8th Intelligence",
                    "description": "Subconscious patterns and intuitions from Synaptic",
                    "mimeType": "text/markdown"
                }
            ]
        }
    
    async def read_resource(self, uri: str) -> dict:
        """Read an MCP resource (generates fresh on each read)."""
        
        if uri == "contextdna://webhook":
            # Generate full 9-section payload
            content = await self._generate_full_webhook()
        
        elif uri == "contextdna://session-recovery":
            # Generate session rehydration content
            content = await self._generate_session_recovery()
        
        elif uri == "contextdna://professor":
            # Generate Professor wisdom only
            content = await self._generate_professor_wisdom()
        
        elif uri == "contextdna://8th-intelligence":
            # Generate 8th Intelligence only
            content = await self._generate_8th_intelligence()
        
        else:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Unknown resource: {uri}"
                }
            }
        
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": content
                }
            ]
        }
    
    async def _generate_full_webhook(self) -> str:
        """Generate full 9-section webhook payload with timeout protection."""
        try:
            from memory.persistent_hook_structure import generate_context_injection
            
            # Get context for current workspace
            prompt = "general workspace context"
            
            loop = asyncio.get_event_loop()
            
            # Add timeout protection (15 seconds max)
            timeout_seconds = int(os.getenv('CONTEXT_DNA_TIMEOUT', '15000')) / 1000
            
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    generate_context_injection,
                    prompt,
                    "hybrid",
                    f"cursor-mcp-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                ),
                timeout=timeout_seconds
            )
            
            if hasattr(result, 'full_payload') and result.full_payload:
                return result.full_payload
            elif hasattr(result, 'content') and result.content:
                return result.content
            else:
                # Fallback to basic context
                return await self._generate_fallback_context()
                
        except asyncio.TimeoutError:
            logger.error(f"Webhook generation timed out after {timeout_seconds}s")
            return await self._generate_fallback_context()
        except Exception as e:
            logger.error(f"Failed to generate webhook payload: {e}")
            return await self._generate_fallback_context()
    
    async def _generate_session_recovery(self) -> str:
        """Generate session recovery content with timeout protection."""
        try:
            from memory.session_historian import SessionHistorian
            
            historian = SessionHistorian()
            
            loop = asyncio.get_event_loop()
            
            # Add timeout protection (3 seconds max for session recovery)
            recovery = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    historian.get_structured_rehydration,
                    None,  # Latest session
                    None,  # No project filter
                    True   # Compact format
                ),
                timeout=3.0
            )
            
            if recovery:
                return f"""# SESSION CRASH RECOVERY

{recovery}

---
**Run this command for full rehydration**:
```bash
cd {REPO_ROOT}
PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate
```
"""
            else:
                return "# No archived sessions yet (normal for first run)"
                
        except asyncio.TimeoutError:
            logger.error("Session recovery timed out after 3s")
            return "# Session recovery timed out (DB might be locked)"
        except Exception as e:
            logger.error(f"Failed to generate session recovery: {e}")
            return f"# Session recovery unavailable: {str(e)}"
    
    async def _generate_professor_wisdom(self) -> str:
        """Generate Professor wisdom only."""
        try:
            # Call helper agent for Professor wisdom
            import aiohttp
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://127.0.0.1:8080/consult/formatted",
                    json={"prompt": "workspace context", "risk_level": "moderate"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("formatted", "# Professor wisdom unavailable")
            
            return "# Professor wisdom unavailable"
            
        except Exception as e:
            logger.error(f"Failed to get Professor wisdom: {e}")
            return f"# Professor unavailable: {str(e)}"
    
    async def _generate_8th_intelligence(self) -> str:
        """Generate 8th Intelligence content with timeout protection."""
        try:
            # Call Synaptic directly
            from memory.synaptic_voice import SynapticVoice
            
            synaptic = SynapticVoice()
            
            loop = asyncio.get_event_loop()
            
            # Add timeout protection (5 seconds max for Synaptic)
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    synaptic.consult,
                    "current workspace context"
                ),
                timeout=5.0
            )
            
            if response and response.synaptic_perspective:
                return f"""# 8TH INTELLIGENCE - Synaptic's Subconscious Voice

[START: Synaptic to Aaron]

{response.synaptic_perspective}

[END: Synaptic to Aaron]

---
**Synaptic is always present, always aware, never sleeps.**
"""
            else:
                return "# 8th Intelligence: Synaptic listening..."
                
        except asyncio.TimeoutError:
            logger.error("8th Intelligence generation timed out after 5s")
            return "# 8th Intelligence timed out (LLM might be slow/offline)"
        except Exception as e:
            logger.error(f"Failed to generate 8th Intelligence: {e}")
            return f"# 8th Intelligence unavailable: {str(e)}"
    
    async def _generate_fallback_context(self) -> str:
        """Fallback when full generation fails."""
        return f"""# Context DNA Webhook (Fallback Mode)

**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

## Session Recovery

Run first on new session:
```bash
cd {REPO_ROOT}
PYTHONPATH=. .venv/bin/python3 memory/session_historian.py rehydrate
```

## Manual Context Fetch

For task-specific context:
```bash
.cursor/contextdna-bridge.sh "your task description"
```

## Status Check

```bash
curl http://127.0.0.1:8080/health
```

---
**Note**: Full webhook generation failed. Using fallback mode.
Check helper agent status and retry.
"""
    
    async def handle_message(self, message: dict) -> dict:
        """Handle MCP protocol messages."""
        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")
        
        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "resources": {"subscribe": False, "listChanged": False}
                        },
                        "serverInfo": {
                            "name": self.name,
                            "version": self.version
                        }
                    }
                }
            
            elif method == "resources/list":
                resources = await self.list_resources()
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": resources
                }
            
            elif method == "resources/read":
                uri = params.get("uri")
                result = await self.read_resource(uri)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                }
            
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}"
                    }
                }
        
        except Exception as e:
            logger.error(f"Error handling {method}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {str(e)}"
                }
            }
    
    async def run(self):
        """Run MCP server on stdio."""
        logger.info(f"{self.name} v{self.version} starting...")
        
        while True:
            try:
                # Read JSON-RPC message from stdin
                line = await asyncio.get_event_loop().run_in_executor(
                    None,
                    sys.stdin.readline
                )
                
                if not line:
                    break
                
                message = json.loads(line.strip())
                response = await self.handle_message(message)
                
                # Write response to stdout
                print(json.dumps(response), flush=True)
                
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
                continue
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                continue


if __name__ == "__main__":
    server = ContextDNAWebhookMCP()
    asyncio.run(server.run())
