"""Context DNA REST API Server.

A lightweight HTTP server that provides unified access to Context DNA.
Uses Python's built-in http.server to avoid extra dependencies.
"""

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from context_dna.brain import Brain

# API Authentication Token (optional - if not set, runs in dev mode)
API_TOKEN = os.getenv("CONTEXT_DNA_API_TOKEN")

# Routes that don't require authentication
PUBLIC_ROUTES = {"/api/health", "/"}


class CORSRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with CORS support and JSON API."""

    # Shared brain instance
    brain: Optional[Brain] = None
    # Connected WebSocket clients for real-time updates
    ws_clients: List = []
    # Lock for thread safety
    lock = threading.Lock()

    def log_message(self, format: str, *args) -> None:
        """Suppress default logging."""
        pass

    def _set_cors_headers(self) -> None:
        """Set CORS headers for cross-origin requests."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _check_auth(self, path: str) -> bool:
        """Check if request is authenticated.

        Returns True if:
        - Path is in PUBLIC_ROUTES (no auth needed)
        - No API_TOKEN configured (dev mode)
        - Valid bearer token provided

        Returns False if authentication fails.
        """
        # Public routes don't need auth
        if path in PUBLIC_ROUTES:
            return True

        # Dev mode - no token configured
        if not API_TOKEN:
            return True

        # Check bearer token
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Constant-time comparison to prevent timing attacks
        return secrets.compare_digest(token, API_TOKEN)

    def _send_json(self, data: Any, status: int = 200) -> None:
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_error(self, message: str, status: int = 400) -> None:
        """Send error response."""
        self._send_json({"error": message}, status)

    def _get_json_body(self) -> Dict:
        """Parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode())

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Check authentication
        if not self._check_auth(path):
            self._send_error("Unauthorized - Bearer token required", 401)
            return

        routes = {
            "/api/health": self._handle_health,
            "/api/stats": self._handle_stats,
            "/api/learnings": self._handle_list_learnings,
            "/api/recent": self._handle_recent,
            "/api/providers": self._handle_providers,
            "/api/setup/status": self._handle_setup_status,
            "/api/setup/missing": self._handle_setup_missing,
            "/api/keys": self._handle_api_keys,
            "/": self._handle_dashboard_redirect,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler(query)
            except Exception as e:
                self._send_error(str(e), 500)
        else:
            self._send_error("Not found", 404)

    def do_POST(self) -> None:
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Check authentication
        if not self._check_auth(path):
            self._send_error("Unauthorized - Bearer token required", 401)
            return

        routes = {
            "/api/win": self._handle_win,
            "/api/fix": self._handle_fix,
            "/api/query": self._handle_query,
            "/api/consult": self._handle_consult,
            "/api/learning": self._handle_add_learning,
            "/api/setup/ai-help": self._handle_ai_setup_help,
            "/api/notify": self._handle_send_notification,
        }

        handler = routes.get(path)
        if handler:
            try:
                body = self._get_json_body()
                handler(body)
            except json.JSONDecodeError:
                self._send_error("Invalid JSON")
            except Exception as e:
                self._send_error(str(e), 500)
        else:
            self._send_error("Not found", 404)

    # -------------------------------------------------------------------------
    # GET Handlers
    # -------------------------------------------------------------------------

    def _handle_health(self, query: Dict) -> None:
        """Health check endpoint."""
        self._send_json({
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "0.1.0",
        })

    def _handle_stats(self, query: Dict) -> None:
        """Quick stats for menu bars and widgets."""
        with self.lock:
            brain = self._get_brain()
            learnings = brain.backend.list_learnings(limit=1000)

        wins = sum(1 for l in learnings if l.learning_type == "win")
        fixes = sum(1 for l in learnings if l.learning_type == "fix")
        patterns = sum(1 for l in learnings if l.learning_type == "pattern")

        # Get today's count
        today = datetime.now().date()
        today_count = sum(
            1 for l in learnings
            if l.created_at and l.created_at.date() == today
        )

        # Get streak (consecutive days with learnings)
        streak = self._calculate_streak(learnings)

        self._send_json({
            "total": len(learnings),
            "wins": wins,
            "fixes": fixes,
            "patterns": patterns,
            "today": today_count,
            "streak": streak,
            "last_updated": datetime.now().isoformat(),
        })

    def _handle_list_learnings(self, query: Dict) -> None:
        """List learnings with pagination."""
        limit = int(query.get("limit", [50])[0])
        offset = int(query.get("offset", [0])[0])
        learning_type = query.get("type", [None])[0]

        with self.lock:
            brain = self._get_brain()
            learnings = brain.backend.list_learnings(
                limit=limit + offset,
                learning_type=learning_type
            )

        # Apply offset
        learnings = learnings[offset:offset + limit]

        self._send_json({
            "learnings": [
                {
                    "id": l.id,
                    "type": l.learning_type,
                    "title": l.title,
                    "content": l.content,
                    "tags": l.tags,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                }
                for l in learnings
            ],
            "count": len(learnings),
            "offset": offset,
            "limit": limit,
        })

    def _handle_recent(self, query: Dict) -> None:
        """Recent activity for dashboards."""
        limit = int(query.get("limit", [10])[0])

        with self.lock:
            brain = self._get_brain()
            learnings = brain.backend.list_learnings(limit=limit)

        self._send_json({
            "recent": [
                {
                    "id": l.id,
                    "type": l.learning_type,
                    "title": l.title,
                    "content": l.content[:200] + "..." if len(l.content) > 200 else l.content,
                    "tags": l.tags,
                    "created_at": l.created_at.isoformat() if l.created_at else None,
                }
                for l in learnings
            ],
        })

    def _handle_providers(self, query: Dict) -> None:
        """List available LLM providers."""
        try:
            from context_dna.llm.manager import ProviderManager
            manager = ProviderManager()
            providers = manager.list_available_providers()
            self._send_json({
                "providers": [
                    {
                        "name": p.name,
                        "model": p.model,
                        "available": True,
                    }
                    for p in providers
                ],
            })
        except ImportError:
            self._send_json({"providers": []})

    def _handle_setup_status(self, query: Dict) -> None:
        """Get complete setup status."""
        try:
            from context_dna.setup.checker import check_all
            status = check_all()
            self._send_json(status.to_dict())
        except ImportError:
            self._send_json({
                "error": "Setup module not available",
                "is_complete": False,
            })

    def _handle_setup_missing(self, query: Dict) -> None:
        """Get missing configurations with fix commands."""
        try:
            from context_dna.setup.checker import get_missing_configs
            missing = get_missing_configs()
            self._send_json({
                "missing": missing,
                "count": len(missing),
            })
        except ImportError:
            self._send_json({"missing": [], "count": 0})

    def _handle_api_keys(self, query: Dict) -> None:
        """Detect configured API keys (masked for security)."""
        try:
            from context_dna.setup.notifications import detect_api_keys
            keys = detect_api_keys()
            self._send_json({
                "keys": keys,
                "any_configured": any(v is not None for v in keys.values()),
            })
        except ImportError:
            self._send_json({"keys": {}, "any_configured": False})

    def _handle_dashboard_redirect(self, query: Dict) -> None:
        """Redirect root to dashboard info."""
        self._send_json({
            "message": "Context DNA API Server",
            "version": "0.1.0",
            "endpoints": {
                "GET /api/health": "Health check",
                "GET /api/stats": "Quick stats for widgets",
                "GET /api/learnings": "List learnings (paginated)",
                "GET /api/recent": "Recent activity",
                "GET /api/providers": "Available LLM providers",
                "POST /api/win": "Record a win",
                "POST /api/fix": "Record a fix",
                "POST /api/query": "Semantic search",
                "POST /api/consult": "Get context for task",
                "POST /api/learning": "Add any learning type",
            },
            "dashboard": "http://localhost:3457",  # Next.js dashboard port
        })

    # -------------------------------------------------------------------------
    # POST Handlers
    # -------------------------------------------------------------------------

    def _handle_win(self, body: Dict) -> None:
        """Record a win."""
        title = body.get("title")
        content = body.get("content", "")
        tags = body.get("tags", [])

        if not title:
            self._send_error("Title is required")
            return

        with self.lock:
            brain = self._get_brain()
            learning_id = brain.win(title, content, tags)

        self._send_json({
            "success": True,
            "id": learning_id,
            "message": f"Win recorded: {title}",
        })

    def _handle_fix(self, body: Dict) -> None:
        """Record a fix."""
        title = body.get("title")
        content = body.get("content", "")
        tags = body.get("tags", [])

        if not title:
            self._send_error("Title is required")
            return

        with self.lock:
            brain = self._get_brain()
            learning_id = brain.fix(title, content, tags)

        self._send_json({
            "success": True,
            "id": learning_id,
            "message": f"Fix recorded: {title}",
        })

    def _handle_query(self, body: Dict) -> None:
        """Semantic search."""
        query_text = body.get("query", body.get("q", ""))
        limit = body.get("limit", 10)

        if not query_text:
            self._send_error("Query is required")
            return

        with self.lock:
            brain = self._get_brain()
            results = brain.query(query_text, limit=limit)

        self._send_json({
            "query": query_text,
            "results": [
                {
                    "id": r.id,
                    "type": r.learning_type,
                    "title": r.title,
                    "content": r.content,
                    "tags": r.tags,
                    "score": getattr(r, "score", None),
                }
                for r in results
            ],
            "count": len(results),
        })

    def _handle_consult(self, body: Dict) -> None:
        """Get context for a task."""
        task = body.get("task", "")

        if not task:
            self._send_error("Task description is required")
            return

        with self.lock:
            brain = self._get_brain()
            context = brain.consult(task)

        self._send_json({
            "task": task,
            "context": context,
        })

    def _handle_add_learning(self, body: Dict) -> None:
        """Add any type of learning."""
        learning_type = body.get("type", "note")
        title = body.get("title")
        content = body.get("content", "")
        tags = body.get("tags", [])

        if not title:
            self._send_error("Title is required")
            return

        with self.lock:
            brain = self._get_brain()
            learning_id = brain.record(
                learning_type=learning_type,
                title=title,
                content=content,
                tags=tags,
            )

        self._send_json({
            "success": True,
            "id": learning_id,
            "type": learning_type,
            "message": f"Learning recorded: {title}",
        })

    def _handle_ai_setup_help(self, body: Dict) -> None:
        """Get AI-powered setup guidance."""
        component = body.get("component", "")
        error = body.get("error", None)

        if not component:
            self._send_error("Component is required")
            return

        try:
            from context_dna.setup.notifications import get_ai_setup_guidance
            guidance = get_ai_setup_guidance(component, error)

            self._send_json({
                "component": component,
                "guidance": guidance or "AI assistance not available. Please check LLM configuration.",
                "ai_available": guidance is not None,
            })
        except Exception as e:
            self._send_json({
                "component": component,
                "guidance": f"Error getting AI guidance: {e}",
                "ai_available": False,
            })

    def _handle_send_notification(self, body: Dict) -> None:
        """Send a system notification."""
        title = body.get("title", "Context DNA")
        message = body.get("message", "")
        notification_type = body.get("type", "info")  # info, success, error, setup

        if not message:
            self._send_error("Message is required")
            return

        try:
            from context_dna.setup.notifications import (
                notify, notify_success, notify_error, notify_setup_needed
            )

            if notification_type == "success":
                success = notify_success(title, message)
            elif notification_type == "error":
                success = notify_error(title, message)
            elif notification_type == "setup":
                success = notify_setup_needed(title, message)
            else:
                success = notify(title, message)

            self._send_json({
                "sent": success,
                "type": notification_type,
            })
        except Exception as e:
            self._send_json({
                "sent": False,
                "error": str(e),
            })

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_brain(self) -> Brain:
        """Get or create shared Brain instance."""
        if CORSRequestHandler.brain is None:
            CORSRequestHandler.brain = Brain()
        return CORSRequestHandler.brain

    def _calculate_streak(self, learnings: List) -> int:
        """Calculate consecutive days streak."""
        if not learnings:
            return 0

        # Get unique dates
        dates = set()
        for l in learnings:
            if l.created_at:
                dates.add(l.created_at.date())

        if not dates:
            return 0

        # Check consecutive days from today
        today = datetime.now().date()
        streak = 0

        from datetime import timedelta
        current = today

        while current in dates:
            streak += 1
            current = current - timedelta(days=1)

        return streak


def create_app(brain: Optional[Brain] = None) -> type:
    """Create request handler with optional pre-configured brain."""
    if brain:
        CORSRequestHandler.brain = brain
    return CORSRequestHandler


def run_server(
    host: str = "127.0.0.1",
    port: int = 3456,
    brain: Optional[Brain] = None,
    daemon: bool = False,
) -> Optional[HTTPServer]:
    """Run the Context DNA API server.

    Args:
        host: Host to bind to (default: localhost only)
        port: Port to listen on (default: 3456)
        brain: Pre-configured Brain instance (optional)
        daemon: If True, run in background thread and return server

    Returns:
        HTTPServer instance if daemon=True, else runs forever
    """
    handler = create_app(brain)
    server = HTTPServer((host, port), handler)

    print(f"Context DNA API Server starting on http://{host}:{port}")
    if API_TOKEN:
        print("Authentication: ENABLED (bearer token required)")
    else:
        print("Authentication: DISABLED (dev mode - set CONTEXT_DNA_API_TOKEN to enable)")
    print("Endpoints:")
    print("  GET  /api/health     - Health check (public)")
    print("  GET  /api/stats      - Quick stats for widgets")
    print("  GET  /api/learnings  - List learnings")
    print("  GET  /api/recent     - Recent activity")
    print("  POST /api/win        - Record a win")
    print("  POST /api/fix        - Record a fix")
    print("  POST /api/query      - Semantic search")
    print("  POST /api/consult    - Get context")
    print()

    if daemon:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            server.shutdown()
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Context DNA API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=3456, help="Port to listen on")

    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
