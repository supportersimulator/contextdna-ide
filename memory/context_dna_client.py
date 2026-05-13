"""
ContextDNA Helper for ER Simulator Voice Agent

Provides a simple interface for storing and querying learnings from the voice stack.
Uses direct HTTP calls to the Helper Agent Service (port 8080) for full functionality.

NO EXTERNAL SDK REQUIRED - Uses stdlib urllib for HTTP calls.

Usage:
    from memory.context_dna_client import ContextDNAClient

    # Initialize (connects to local Helper Agent Service)
    memory = ContextDNAClient()

    # Record a bug fix discovery
    memory.record_bug_fix(
        symptom="LLM requests taking 14s instead of 1s",
        root_cause="boto3.converse() is synchronous and blocks event loop",
        fix="Wrap in asyncio.to_thread(): await asyncio.to_thread(client.converse, **params)",
        tags=["llm", "async", "boto3", "performance"]
    )

    # Query for relevant learnings before making changes
    lessons = memory.get_relevant_learnings("async boto3 performance")

    # Get context for prompt injection
    context = memory.get_prompt_context(area="tts streaming")

Environment Variables:
    CONTEXT_DNA_BASE_URL: Helper Agent API URL (default: http://localhost:8080)
    CONTEXT_DNA_API_KEY: API key (optional for local dev)
    CONTEXT_DNA_SPACE_ID: Space ID for storing learnings (auto-generated)
"""

import os
import json
import urllib.request
import urllib.error
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime

# HTTP client is always available - no external SDK needed
CONTEXT_DNA_SDK_AVAILABLE = True
CONTEXT_DNA_AVAILABLE = True

# Environment variables - Helper Agent Service runs on port 8080
CONTEXT_DNA_BASE_URL = os.environ.get(
    "CONTEXT_DNA_BASE_URL",
    os.environ.get("ACONTEXT_BASE_URL", "http://localhost:8080")
)
CONTEXT_DNA_API_KEY = os.environ.get(
    "CONTEXT_DNA_API_KEY",
    os.environ.get("ACONTEXT_API_KEY", "")
)
CONTEXT_DNA_SPACE_ID = os.environ.get(
    "CONTEXT_DNA_SPACE_ID",
    os.environ.get("ACONTEXT_SPACE_ID", "default")
)


class ContextDNAClient:
    """Helper for storing and querying voice stack learnings via HTTP API.

    Uses direct HTTP calls to the Helper Agent Service - no external SDK required.

    PERFORMANCE: Uses short timeout (2s) and fast-fail pattern.
    If service is unavailable, fails quickly rather than blocking webhook generation.
    """

    DEFAULT_BASE_URL = "http://localhost:8080"
    DEFAULT_USER = "ersim-voice-agent"
    TIMEOUT = 2  # REDUCED from 10s - fail fast if service unavailable

    # Cache service availability to avoid repeated connection attempts
    _service_available = None
    _service_check_time = 0

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        space_id: str = None,
        user: str = None
    ):
        """Initialize ContextDNA connection.

        Args:
            base_url: Helper Agent API URL (or CONTEXT_DNA_BASE_URL env var)
            api_key: API key (optional for local dev)
            space_id: Space ID for learnings (or CONTEXT_DNA_SPACE_ID env var)
            user: User identifier for attribution
        """
        self.base_url = (base_url or os.getenv("CONTEXT_DNA_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.getenv("CONTEXT_DNA_API_KEY", "")
        self.space_id = space_id or os.getenv("CONTEXT_DNA_SPACE_ID", "default")
        self.user = user or os.getenv("CONTEXT_DNA_USER", self.DEFAULT_USER)

    def _http_get(self, endpoint: str, params: Dict = None) -> Dict:
        """Make an HTTP GET request."""
        url = f"{self.base_url}{endpoint}"
        if params:
            # URL-encode parameter values to handle spaces and special characters
            from urllib.parse import quote
            query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        try:
            req = urllib.request.Request(url)
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Content-Type", "application/json")

            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.URLError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def _http_post(self, endpoint: str, data: Dict = None, params: Dict = None) -> Dict:
        """Make an HTTP POST request."""
        url = f"{self.base_url}{endpoint}"
        if params:
            # URL-encode parameter values to handle spaces and special characters
            from urllib.parse import quote
            query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        try:
            body = json.dumps(data or {}).encode() if data else b""
            req = urllib.request.Request(url, data=body, method="POST")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            req.add_header("Content-Type", "application/json")

            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.URLError as e:
            return {"error": str(e), "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def _generate_session_id(self) -> str:
        """Generate a unique session ID."""
        return f"session_{uuid.uuid4().hex[:12]}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def record_bug_fix(
        self,
        symptom: str,
        root_cause: str,
        fix: str,
        tags: List[str] = None,
        file_path: str = None,
        additional_context: str = None
    ) -> str:
        """Record a bug fix discovery.

        Args:
            symptom: What was observed (the problem)
            root_cause: Why it happened
            fix: How it was resolved
            tags: Relevant keywords for search
            file_path: Path to relevant file
            additional_context: Any extra information

        Returns:
            Learning ID
        """
        content = f"""**Symptom:** {symptom}

**Root Cause:** {root_cause}

**Fix Applied:** {fix}

**Status:** ✅ RESOLVED - Fix verified and working.
"""
        if file_path:
            content += f"\n**File:** {file_path}"
        if additional_context:
            content += f"\n**Additional Context:** {additional_context}"

        learning = {
            "type": "fix",
            "title": f"Fix: {symptom[:80]}",
            "content": content,
            "tags": tags or [],
        }

        result = self._http_post("/api/learnings", learning)
        return result.get("id", result.get("learning_id", ""))

    def record_architecture_decision(
        self,
        decision: str,
        rationale: str,
        alternatives: List[str] = None,
        consequences: str = None
    ) -> str:
        """Record an architecture decision.

        Args:
            decision: What was decided
            rationale: Why this approach was chosen
            alternatives: Other options considered
            consequences: Impact/tradeoffs of the decision

        Returns:
            Learning ID
        """
        content = f"""**Decision:** {decision}

**Rationale:** {rationale}
"""
        if alternatives:
            content += "\n**Alternatives Considered:**\n"
            for alt in alternatives:
                content += f"- {alt}\n"
        if consequences:
            content += f"\n**Consequences/Tradeoffs:** {consequences}"

        learning = {
            "type": "pattern",
            "title": f"Architecture: {decision[:80]}",
            "content": content,
            "tags": ["architecture", "decision"],
        }

        result = self._http_post("/api/learnings", learning)
        return result.get("id", result.get("learning_id", ""))

    def record_performance_lesson(
        self,
        metric: str,
        before: str,
        after: str,
        technique: str,
        file_path: str = None,
        tags: List[str] = None
    ) -> str:
        """Record a performance optimization lesson.

        Args:
            metric: What was measured
            before: Value before optimization
            after: Value after optimization
            technique: How the improvement was achieved
            file_path: Relevant file
            tags: Keywords for search

        Returns:
            Learning ID
        """
        content = f"""**Metric:** {metric}
**Before:** {before}
**After:** {after}

**Technique:** {technique}
"""
        if file_path:
            content += f"\n**File:** {file_path}"

        learning = {
            "type": "win",
            "title": f"Performance: {metric[:80]}",
            "content": content,
            "tags": (tags or []) + ["performance", "optimization"],
        }

        result = self._http_post("/api/learnings", learning)
        return result.get("id", result.get("learning_id", ""))

    def _is_service_available(self) -> bool:
        """Fast check if the service is available (cached for 30s)."""
        import time
        now = time.time()

        # Use cached result if checked within last 30 seconds
        if ContextDNAClient._service_available is not None:
            if (now - ContextDNAClient._service_check_time) < 30:
                return ContextDNAClient._service_available

        # Quick health check with very short timeout
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=0.5) as response:
                ContextDNAClient._service_available = response.status == 200
        except Exception:
            ContextDNAClient._service_available = False

        ContextDNAClient._service_check_time = now
        return ContextDNAClient._service_available


    def _sqlite_fts5_fallback(self, query, limit=5, debug=False):
        """SQLite FTS5 fallback when API unavailable or returns empty."""
        try:
            from memory.sqlite_storage import get_sqlite_storage
            local_store = get_sqlite_storage()  # Singleton — prevents FD leak
            sqlite_results = local_store.query(query, limit=limit)
            if not sqlite_results:
                query_lower = query.lower()
                for word in query_lower.split():
                    if len(word) >= 3:
                        word_results = local_store.query(word, limit=limit)
                        if word_results:
                            sqlite_results = word_results
                            break
            if sqlite_results:
                if debug:
                    print(f'[DEBUG] SQLite FTS5 fallback: {len(sqlite_results)} results')

            # SEMANTIC RESCUE: If FTS5 returned <3 results, augment with
            # semantic similarity search (sentence-transformers).
            # Silently degrades if sentence-transformers not installed.
            if len(sqlite_results) < 3:
                try:
                    from memory.semantic_search import rescue_search
                    rescued = rescue_search(query, sqlite_results, min_results=3, top_k=limit)
                    if len(rescued) > len(sqlite_results):
                        if debug:
                            semantic_added = len(rescued) - len(sqlite_results)
                            print(f'[DEBUG] Semantic rescue added {semantic_added} results')
                        return rescued[:limit]
                except Exception as e:
                    if debug:
                        print(f'[DEBUG] Semantic rescue failed (graceful): {e}')

            return sqlite_results[:limit]
        except Exception as e:
            if debug:
                print(f'[DEBUG] SQLite FTS5 fallback failed: {e}')
        return []

    def get_relevant_learnings(self, query: str, limit: int = 5, debug: bool = False) -> List[Dict]:
        """Search for relevant learnings based on a query.

        Args:
            query: Search query (e.g., "async boto3 performance")
            limit: Max results to return
            debug: If True, print diagnostic information

        Returns:
            List of relevant learning dicts with title, content, type, and distance

        PERFORMANCE: Fast-fails if service unavailable (returns [] in <0.5s).
        """
        # FAST-FAIL: Check if service is available before making requests
        if not self._is_service_available():
            if debug:
                print("[DEBUG] Service unavailable, falling back to SQLite")
            return self._sqlite_fts5_fallback(query, limit, debug)

        diagnostics = {"query": query, "steps": [], "roadblocks": []}

        # Try the /api/query endpoint first (semantic search with improved keyword fallback)
        query_result = self._http_post("/api/query", {"query": query, "limit": limit})
        diagnostics["steps"].append({
            "step": "api_query_endpoint",
            "count": query_result.get("count", 0),
            "source": query_result.get("source", "unknown"),
            "error": query_result.get("error"),
        })

        if query_result.get("results") and not query_result.get("error"):
            results = query_result.get("results", [])
            # Normalize the response format
            learnings = []
            for r in results:
                learnings.append({
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "type": r.get("type", ""),
                    "tags": r.get("tags", []),
                    "distance": 1.0 - r.get("score", 0.5),  # Convert score to distance
                })
            if debug:
                print(f"[DEBUG] /api/query returned {len(learnings)} results via {query_result.get('source')}")
            if learnings:
                # SEMANTIC RESCUE: If API returned <3 results, augment with
                # semantic similarity search before returning.
                if len(learnings) < 3:
                    try:
                        from memory.semantic_search import rescue_search
                        rescued = rescue_search(query, learnings, min_results=3, top_k=limit)
                        if len(rescued) > len(learnings):
                            if debug:
                                print(f'[DEBUG] Semantic rescue added {len(rescued) - len(learnings)} results to API results')
                            return rescued[:limit]
                    except Exception as e:
                        if debug:
                            print(f'[DEBUG] Semantic rescue (api path) failed (graceful): {e}')
                return learnings

        # Fallback: Try the consult endpoint which does semantic search
        result = self._http_post("/consult", params={"prompt": query})
        diagnostics["steps"].append({
            "step": "consult_endpoint",
            "result_keys": list(result.keys()) if isinstance(result, dict) else "not_dict",
            "has_sops": bool(result.get("sops")),
            "has_layers": bool(result.get("layers")),
            "error": result.get("error"),
        })

        # Check layers for learnings (newer API structure)
        layers = result.get("layers", {})
        if layers:
            # Check for learnings in layers
            layer_learnings = layers.get("learnings", [])
            if layer_learnings:
                diagnostics["steps"].append({"step": "layers_learnings_found", "count": len(layer_learnings)})
                learnings = []
                for item in layer_learnings[:limit]:
                    if isinstance(item, str):
                        learnings.append({
                            "title": item[:100],
                            "content": item,
                            "type": "sop",
                            "distance": 0.3,
                        })
                    elif isinstance(item, dict):
                        learnings.append({
                            "title": item.get("title", ""),
                            "content": item.get("content", item.get("preferences", "")),
                            "type": item.get("type", "sop"),
                            "distance": 0.3,
                        })
                if learnings:
                    if debug:
                        print(f"[DEBUG] Diagnostics: {diagnostics}")
                    return learnings

            # Check for errors in layers - report as roadblocks
            for key, value in layers.items():
                if "error" in key.lower():
                    diagnostics["roadblocks"].append({
                        "layer": key.replace("_error", ""),
                        "error": str(value)[:200],
                    })

        # Legacy: check for sops key (older API structure)
        if "error" not in result and result.get("sops"):
            learnings = []
            for sop_type, sop_list in result.get("sops", {}).items():
                for sop in sop_list[:limit]:
                    learnings.append({
                        "title": sop.get("title", ""),
                        "content": sop.get("content", ""),
                        "type": sop_type,
                        "distance": 0.5,
                    })
            if learnings:
                if debug:
                    diagnostics["steps"].append({"step": "sops_found", "count": len(learnings)})
                    print(f"[DEBUG] Diagnostics: {diagnostics}")
                return learnings

        # Fallback: try the recent learnings endpoint
        result = self._http_get("/api/learnings/recent", {"limit": limit * 2})
        learnings = result.get("learnings", [])
        diagnostics["steps"].append({
            "step": "recent_learnings_fallback",
            "total_recent": len(learnings),
            "error": result.get("error"),
        })

        if not learnings:
            diagnostics["roadblocks"].append({
                "layer": "recent_learnings",
                "error": "No recent learnings available in memory store",
            })

        # Basic keyword filtering
        query_lower = query.lower()
        query_words = set(query_lower.split())

        filtered = []
        for learning in learnings:
            title = learning.get("title", "").lower()
            content = learning.get("content", "").lower()
            tags = [t.lower() for t in learning.get("tags", [])]

            # Check if any query word matches
            text = f"{title} {content} {' '.join(tags)}"
            if any(word in text for word in query_words):
                filtered.append({
                    "id": learning.get("id", ""),
                    "title": learning.get("title", ""),
                    "content": learning.get("content", ""),
                    "type": learning.get("type", ""),
                    "distance": 0.5,
                })

        if len(filtered) == 0 and learnings:
            diagnostics["roadblocks"].append({
                "layer": "keyword_filter",
                "error": f"No learnings matched query words: {list(query_words)}. "
                         f"Available learnings ({len(learnings)}) don't contain these terms.",
            })

        diagnostics["steps"].append({
            "step": "keyword_filter",
            "query_words": list(query_words),
            "filtered_count": len(filtered),
            "reason": "no_match" if len(filtered) == 0 else "matched",
        })

        if debug:
            print(f"[DEBUG] Diagnostics: {diagnostics}")
            if diagnostics["roadblocks"]:
                print(f"[ROADBLOCKS] {diagnostics['roadblocks']}")

        # SQLITE FTS5 FALLBACK: API up but returned empty -> try local SQLite.
        # Covers: 8080 UP, 8029/3456 DOWN -> API keyword search too weak -> 0 results.
        if len(filtered) == 0:
            sqlite_results = self._sqlite_fts5_fallback(query, limit, debug)
            if sqlite_results:
                if debug:
                    print(f'[DEBUG] SQLite FTS5 rescued empty API with {len(sqlite_results)} results')
                return sqlite_results

        # SEMANTIC RESCUE: Final fallback — if all paths returned <3 results,
        # try semantic similarity search. This catches the ~15% of queries
        # where user says "deploy" but knowledge stored under "rollout".
        final_results = filtered[:limit]
        if len(final_results) < 3:
            try:
                from memory.semantic_search import rescue_search
                rescued = rescue_search(query, final_results, min_results=3, top_k=limit)
                if len(rescued) > len(final_results):
                    if debug:
                        semantic_added = len(rescued) - len(final_results)
                        print(f'[DEBUG] Semantic rescue (final) added {semantic_added} results')
                    return rescued[:limit]
            except Exception as e:
                if debug:
                    print(f'[DEBUG] Semantic rescue (final) failed (graceful): {e}')

        return final_results

    def get_prompt_context(self, area: str = None, max_items: int = 10) -> str:
        """Generate context for prompt injection.

        Args:
            area: Specific area to focus on (e.g., "tts streaming")
            max_items: Max learnings to include

        Returns:
            Formatted string for prompt injection
        """
        if area:
            learnings = self.get_relevant_learnings(area, limit=max_items)
        else:
            result = self._http_get("/api/learnings/recent", {"limit": max_items})
            learnings = result.get("learnings", [])

        if not learnings:
            return ""

        output = ["## Relevant Project Learnings\n"]
        for i, learning in enumerate(learnings, 1):
            title = learning.get("title", "")
            content = learning.get("content", "")
            if title or content:
                output.append(f"### Learning {i}: {title}")
                output.append(content[:500] + "..." if len(content) > 500 else content)
                output.append("")

        return "\n".join(output)

    def ping(self) -> bool:
        """Check if Helper Agent Service is reachable."""
        result = self._http_get("/health")
        return result.get("status") == "healthy" or "error" not in result

    # =========================================================================
    # AGENT SESSION MANAGEMENT - For explicit task completion tracking
    # =========================================================================

    def start_agent_session(self, task_description: str, agent_name: str = "atlas") -> str:
        """Start a new agent session for task tracking.

        Args:
            task_description: What the agent is trying to accomplish
            agent_name: Name of the agent (atlas, hermes, navigator, etc.)

        Returns:
            Session ID for use with complete_task()
        """
        return self._generate_session_id()

    def log_agent_step(self, session_id: str, step: str, result: str = None):
        """Log an intermediate step in the agent session.

        Persists step to the observability store's task_run_event table
        for session replay and debugging.

        Args:
            session_id: Session ID from start_agent_session()
            step: Description of what was done
            result: Optional result or output
        """
        try:
            from memory.observability_store import get_observability_store
            store = get_observability_store()
            store.record_task_run(
                task_name=f"agent_step:{session_id[:12]}",
                run_type="agent_step",
                status="success",
                duration_ms=0,
                details={"session_id": session_id, "step": step, "result": result}
            )
        except Exception:
            pass  # Non-blocking telemetry

    def complete_task(
        self,
        session_id: str,
        success: bool,
        summary: str,
        learnings: List[str] = None
    ) -> str:
        """Mark a task as complete and record the learning.

        Args:
            session_id: Session ID from start_agent_session()
            success: Whether the task was successful
            summary: Summary of what was accomplished
            learnings: Optional list of key learnings to highlight

        Returns:
            Learning ID if successful
        """
        if not success:
            print(f"❌ Task marked FAILED - No learning recorded for session {session_id}")
            return session_id

        content = f"**Summary:** {summary}\n\n**Status:** ✅ COMPLETED SUCCESSFULLY"
        if learnings:
            content += "\n\n**Key Learnings:**\n"
            for learning in learnings:
                content += f"- {learning}\n"

        learning = {
            "type": "win",
            "title": f"Task Success: {summary[:60]}",
            "content": content,
            "tags": ["task", "success"],
        }

        result = self._http_post("/api/learnings", learning)
        learning_id = result.get("id", result.get("learning_id", session_id))

        print(f"✅ Task marked SUCCESS - Learning recorded: {learning_id}")
        return learning_id

    def record_agent_success(
        self,
        task: str,
        approach: str,
        result: str,
        agent_name: str = "atlas",
        tags: List[str] = None
    ) -> str:
        """Record a successful task completion with CLEAN format.

        PHILOSOPHY: No verbose templates, no prefixes.
        Output should be human-readable like "Split panel UI working!"

        Args:
            task: What was accomplished (used as title directly - NO PREFIX)
            approach: How it was done (used as content)
            result: Outcome (appended to content if different from approach)
            agent_name: Which agent (added to tags, not content)
            tags: Keywords for search

        Returns:
            Learning ID
        """
        # CLEAN title - just the task, no "Agent Success:" prefix
        title = task.strip()

        # CLEAN content - just the approach and result, no verbose template
        content = approach.strip() if approach else title
        if result and result.strip() != "Successfully completed":
            content = f"{content}\n\nResult: {result.strip()}"

        learning = {
            "type": "win",
            "title": title,
            "content": content,
            "tags": (tags or []) + [agent_name],
        }

        resp = self._http_post("/api/learnings", learning)
        learning_id = resp.get("id", resp.get("learning_id", ""))

        print(f"✅ Success recorded: {learning_id}")
        return learning_id

    def query_8th_intelligence(self, subtask: str, agent_id: str = "") -> Dict[str, Any]:
        """
        SUPERHERO MODE: Query Synaptic's 8th Intelligence for agent guidance.

        Agents call this mid-task to receive:
        - patterns: Relevant patterns from past work
        - gotchas: Warnings before hitting known issues
        - intuitions: Professor's wisdom for this task
        - stop_signal: If set, STOP and verify with user

        Args:
            subtask: What the agent is currently working on
            agent_id: Optional agent identifier

        Returns:
            Dict with synaptic_response containing patterns, gotchas, intuitions, stop_signal

        Example:
            response = memory.query_8th_intelligence("deploy terraform to production", "agent-001")
            if response.get("synaptic_response", {}).get("stop_signal"):
                print("⚠️ STOP:", response["synaptic_response"]["stop_signal"])
            for gotcha in response.get("synaptic_response", {}).get("gotchas", []):
                print("⚠️ Gotcha:", gotcha)
        """
        import urllib.parse
        encoded_subtask = urllib.parse.quote(subtask)
        endpoint = f"/contextdna/8th-intelligence?subtask={encoded_subtask}&agent_id={agent_id}"

        try:
            resp = self._http_post(endpoint, {})
            return resp
        except Exception as e:
            # Graceful degradation - return empty response
            return {
                "agent_id": agent_id,
                "subtask": subtask,
                "synaptic_response": {
                    "patterns": [],
                    "gotchas": [f"8th Intelligence unavailable: {str(e)[:50]}"],
                    "intuitions": [],
                    "stop_signal": None
                },
                "superhero_mode": False
            }


# Convenience function for SUPERHERO MODE
def ask_synaptic(subtask: str, agent_id: str = "") -> Dict[str, Any]:
    """
    Quick function to query Synaptic's 8th Intelligence.

    Usage:
        from memory.context_dna_client import ask_synaptic
        response = ask_synaptic("deploy terraform")
        print(response["synaptic_response"]["intuitions"])
    """
    return ContextDNAClient().query_8th_intelligence(subtask, agent_id)


def ask_synaptic_midtask(subtask: str, agent_id: str = "") -> Dict[str, Any]:
    """
    SUPERHERO MODE: Query Synaptic mid-task for urgent guidance.

    Lightweight, non-blocking query for agents mid-execution to check:
    - stop_signal: STOP and verify with human immediately
    - gotchas: Warnings before proceeding
    - patterns: Has this problem been solved before?
    - intuitions: Synaptic's wisdom for this task

    Args:
        subtask: Current task/problem (e.g., "WebRTC connection failing")
        agent_id: Optional identifier for this agent

    Returns:
        Dict with keys: stop_signal, gotchas, patterns, intuitions, confidence, available

    Guarantees:
        - Non-blocking: <2s response time (or empty dict on timeout)
        - Graceful: Returns safe defaults on any error
        - Safe: Never throws exceptions

    Example:
        response = ask_synaptic_midtask("deploying to production", "atlas")
        if response["stop_signal"]:
            print(f"⚠️ STOP: {response['stop_signal']}")
            return False
        for gotcha in response["gotchas"]:
            print(f"⚠️ {gotcha}")
    """
    client = ContextDNAClient()
    full_response = client.query_8th_intelligence(subtask, agent_id)

    # Extract synaptic response
    synaptic = full_response.get("synaptic_response", {})

    # Format for easy mid-task consumption
    return {
        "stop_signal": synaptic.get("stop_signal"),
        "gotchas": synaptic.get("gotchas", []),
        "patterns": synaptic.get("patterns", []),
        "intuitions": synaptic.get("intuitions", []),
        "confidence": synaptic.get("confidence", 0),
        "available": full_response.get("superhero_mode", False)
    }


def format_synaptic_guidance(response: Dict[str, Any], compact: bool = False) -> str:
    """
    Format Synaptic response for display.

    Args:
        response: Response from ask_synaptic_midtask()
        compact: If True, one-liner format. If False, full format.

    Returns:
        Formatted string for printing
    """
    if not response.get("available"):
        return "⚠️ Synaptic unavailable (proceeding with caution)"

    if response.get("stop_signal"):
        return f"🛑 STOP: {response['stop_signal']}"

    output = []
    if response.get("gotchas"):
        output.append("⚠️ Gotchas:")
        for gotcha in response["gotchas"][:3]:
            output.append(f"  - {gotcha}")

    if response.get("patterns"):
        output.append("📊 Similar problems solved:")
        for pattern in response["patterns"][:2]:
            output.append(f"  - {pattern[:70]}...")

    if compact:
        return " | ".join(output[:1])  # First line only
    return "\n".join(output) if output else "✅ No warnings - proceed"


# Convenience function for quick access
def get_memory() -> ContextDNAClient:
    """Get a configured ContextDNAClient instance."""
    return ContextDNAClient()


if __name__ == "__main__":
    # Test connection
    memory = ContextDNAClient()
    print(f"ContextDNA connected: {memory.ping()}")
    print(f"Space ID: {memory.space_id}")
    print(f"Base URL: {memory.base_url}")

    # Test querying
    print("\nSearching for 'async' learnings...")
    learnings = memory.get_relevant_learnings("async", limit=3)
    print(f"Found {len(learnings)} learnings")
    for l in learnings:
        print(f"  - {l.get('title', 'No title')}")
