#!/usr/bin/env python3
"""
Repo Librarian — Unified Codebase Query Endpoint for AI Agents

POST /v1/context/query

Unifies:
- ArchitectureGraphBuilder (AST-based file/class/function graph)
- SQLiteStorage FTS5 (full-text search on learnings)
- KnowledgeGraph (hierarchical categorization)
- code_chunk_indexer (FTS5 over actual code chunks)
- LLM reranking via llm_priority_queue (P4, optional)

Design:
- FastAPI APIRouter, mountable in agent_service.py
- Non-LLM fallback always works (LLM reranking is optional enrichment)
- All heavy init is lazy (graph builder, storage singletons)
"""

import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("context_dna.librarian")

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# FastAPI imports
try:
    from fastapi import APIRouter
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

# ---------------------------------------------------------------------------
# Lazy singletons — avoid import-time side effects
# ---------------------------------------------------------------------------

_graph_builder = None
_graph = None
_sqlite_storage = None
_unified_storage = None
_knowledge_graph = None


def _get_graph_builder():
    global _graph_builder
    if _graph_builder is None:
        from memory.code_parser.graph_builder import ArchitectureGraphBuilder
        _graph_builder = ArchitectureGraphBuilder(REPO_ROOT)
    return _graph_builder


def _get_graph():
    """Load cached graph (fast) or build if needed."""
    global _graph
    if _graph is None:
        builder = _get_graph_builder()
        _graph = builder.build_graph()  # uses cache internally
    return _graph


def _get_sqlite_storage():
    global _sqlite_storage
    if _sqlite_storage is None:
        from memory.sqlite_storage import get_sqlite_storage
        _sqlite_storage = get_sqlite_storage()
    return _sqlite_storage


def _get_unified_storage():
    global _unified_storage
    if _unified_storage is None:
        from memory.unified_storage import get_storage
        _unified_storage = get_storage()
    return _unified_storage


def _get_knowledge_graph():
    global _knowledge_graph
    if _knowledge_graph is None:
        from memory.knowledge_graph import KnowledgeGraph
        _knowledge_graph = KnowledgeGraph()
    return _knowledge_graph


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class QueryIntent(str, Enum):
    locate = "locate"
    explain = "explain"
    trace = "trace"
    impact = "impact"
    tests = "tests"
    deps = "deps"
    docs = "docs"
    decision = "decision"


class ContextQueryRequest(BaseModel):
    agent_id: str = Field(..., description="Identifying the caller")
    intent: QueryIntent = Field(..., description="Query intent type")
    query: str = Field(..., description="Natural language query")
    max_files: int = Field(default=10, ge=1, le=50)
    max_snippets: int = Field(default=5, ge=1, le=20)


class FileResult(BaseModel):
    path: str
    relevance: float
    summary: str


class SnippetResult(BaseModel):
    file: str
    line_start: int
    line_end: int
    content: str
    relevance: float


class SOPResult(BaseModel):
    title: str
    summary: str
    relevance: float


class ContextQueryResponse(BaseModel):
    files: List[FileResult]
    snippets: List[SnippetResult]
    related_sops: List[SOPResult]
    confidence: float
    intent: str
    query_time_ms: float


# ---------------------------------------------------------------------------
# Core search functions
# ---------------------------------------------------------------------------

def _search_graph_nodes(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """Search architecture graph nodes by name/path keyword match.

    Returns list of dicts with path, name, type, line_start, line_end, category.
    """
    graph = _get_graph()
    query_lower = query.lower()
    query_words = set(query_lower.split())

    scored = []
    for node in graph.nodes:
        text = f"{node.name} {node.file_path} {node.category}".lower()
        if node.metadata.get("docstring"):
            text += " " + node.metadata["docstring"].lower()

        # Score: count of query words that appear in node text
        matches = sum(1 for w in query_words if w in text)
        if matches == 0:
            continue

        # Boost exact name matches
        name_lower = node.name.lower()
        name_boost = 2.0 if query_lower in name_lower else 1.0
        # Boost path matches
        path_boost = 1.3 if any(w in node.file_path.lower() for w in query_words) else 1.0

        score = (matches / max(len(query_words), 1)) * name_boost * path_boost

        scored.append({
            "path": node.file_path,
            "name": node.name,
            "type": node.type.value,
            "line_start": node.line_start,
            "line_end": node.line_end,
            "category": node.category,
            "node_id": node.id,
            "docstring": (node.metadata.get("docstring") or "")[:200],
            "score": min(score, 1.0),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_results]


def _search_code_chunks(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search code chunks via FTS5 (code_chunk_indexer)."""
    try:
        from memory.code_chunk_indexer import search_code
        return search_code(query, limit=limit)
    except Exception as e:
        logger.debug(f"Code chunk search unavailable: {e}")
        return []


def _search_learnings(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Search learnings via SQLiteStorage FTS5 + UnifiedStorage PG fallback."""
    results = []

    # Try SQLiteStorage FTS5 first (fastest, local)
    try:
        storage = _get_sqlite_storage()
        results = storage.query(query, limit=limit)
    except Exception as e:
        logger.debug(f"SQLite FTS5 search failed: {e}")

    # If SQLite returned nothing, try UnifiedStorage (PG + Redis)
    if not results:
        try:
            unified = _get_unified_storage()
            results = unified.query_learnings(search_term=query, limit=limit)
        except Exception as e:
            logger.debug(f"Unified storage search failed: {e}")

    return results


def _trace_dependencies(query: str, graph, max_depth: int = 2) -> List[Dict[str, Any]]:
    """Trace imports/calls from a node matching the query."""
    # Find the best matching node
    nodes = _search_graph_nodes(query, max_results=1)
    if not nodes:
        return []

    target_id = nodes[0]["node_id"]
    subgraph = graph.get_subgraph(target_id, depth=max_depth)
    if not subgraph:
        return []

    results = []
    for node in subgraph.nodes:
        if node.id == target_id:
            continue
        results.append({
            "path": node.file_path,
            "name": node.name,
            "type": node.type.value,
            "line_start": node.line_start,
            "line_end": node.line_end,
            "category": node.category,
        })
    return results


def _find_callers(query: str, graph) -> List[Dict[str, Any]]:
    """Find nodes that call/import the target (impact analysis)."""
    nodes = _search_graph_nodes(query, max_results=1)
    if not nodes:
        return []

    target_id = nodes[0]["node_id"]

    # Find edges where target is the destination
    caller_ids = set()
    for edge in graph.edges:
        if edge.target == target_id:
            caller_ids.add(edge.source)

    results = []
    for node in graph.nodes:
        if node.id in caller_ids:
            results.append({
                "path": node.file_path,
                "name": node.name,
                "type": node.type.value,
                "line_start": node.line_start,
                "line_end": node.line_end,
                "category": node.category,
            })
    return results


def _llm_rerank(
    query: str,
    items: List[Dict[str, Any]],
    max_results: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Optionally rerank results using local LLM (P4 background priority).

    Returns None if LLM unavailable (caller should use original order).
    """
    if not items:
        return None

    try:
        from memory.llm_priority_queue import butler_query
    except ImportError:
        return None

    # Build a compact summary for the LLM
    items_text = "\n".join(
        f"{i+1}. [{it.get('type','?')}] {it.get('name','?')} in {it.get('path','?')}"
        + (f" — {it.get('docstring','')[:80]}" if it.get("docstring") else "")
        for i, it in enumerate(items[:15])
    )

    system = (
        "You are a code search reranker. Given a query and numbered code results, "
        "return ONLY a comma-separated list of result numbers in order of relevance "
        "to the query. Example: 3,1,5,2,4"
    )
    user = f"Query: {query}\n\nResults:\n{items_text}\n\nRerank:"

    response = butler_query(system, user, profile="coding")
    if not response:
        return None

    # Parse comma-separated indices
    try:
        indices = []
        for part in response.strip().split(","):
            part = part.strip().rstrip(".")
            if part.isdigit():
                idx = int(part) - 1  # 1-indexed -> 0-indexed
                if 0 <= idx < len(items):
                    indices.append(idx)
        if indices:
            reranked = [items[i] for i in indices]
            # Append any items not mentioned
            seen = set(indices)
            for i, it in enumerate(items):
                if i not in seen:
                    reranked.append(it)
            return reranked[:max_results]
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------

def _handle_locate(req: ContextQueryRequest) -> ContextQueryResponse:
    """Find files/symbols matching the query."""
    t0 = time.monotonic()

    graph_results = _search_graph_nodes(req.query, max_results=req.max_files * 2)
    code_chunks = _search_code_chunks(req.query, limit=req.max_snippets)
    learnings = _search_learnings(req.query, limit=5)

    # Try LLM reranking on graph results
    reranked = _llm_rerank(req.query, graph_results, max_results=req.max_files)
    if reranked:
        graph_results = reranked

    # Deduplicate files by path
    seen_paths = set()
    files = []
    for r in graph_results:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            summary = f"[{r['type']}] {r['name']}"
            if r.get("docstring"):
                summary += f" — {r['docstring'][:100]}"
            files.append(FileResult(
                path=r["path"],
                relevance=round(r.get("score", 0.5), 2),
                summary=summary,
            ))
        if len(files) >= req.max_files:
            break

    snippets = [
        SnippetResult(
            file=c["file_path"],
            line_start=c.get("start_line", 0),
            line_end=c.get("end_line", 0),
            content=c.get("content", "")[:500],
            relevance=round(0.8 - i * 0.05, 2),
        )
        for i, c in enumerate(code_chunks[:req.max_snippets])
    ]

    sops = _learnings_to_sops(learnings, req.query)

    # Confidence: based on whether we found anything useful
    confidence = 0.0
    if files:
        confidence += 0.5
    if snippets:
        confidence += 0.3
    if sops:
        confidence += 0.2

    return ContextQueryResponse(
        files=files,
        snippets=snippets,
        related_sops=sops,
        confidence=round(min(confidence, 1.0), 2),
        intent=req.intent.value,
        query_time_ms=round((time.monotonic() - t0) * 1000, 1),
    )


def _handle_explain(req: ContextQueryRequest) -> ContextQueryResponse:
    """Get file content + related learnings for understanding."""
    t0 = time.monotonic()

    graph_results = _search_graph_nodes(req.query, max_results=req.max_files)
    learnings = _search_learnings(req.query, limit=10)
    code_chunks = _search_code_chunks(req.query, limit=req.max_snippets)

    files = []
    seen_paths = set()
    for r in graph_results:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            summary = f"[{r['type']}] {r['name']}"
            if r.get("docstring"):
                summary += f" — {r['docstring'][:150]}"
            if r.get("category"):
                summary += f" (category: {r['category']})"
            files.append(FileResult(
                path=r["path"],
                relevance=round(r.get("score", 0.5), 2),
                summary=summary,
            ))
        if len(files) >= req.max_files:
            break

    snippets = [
        SnippetResult(
            file=c["file_path"],
            line_start=c.get("start_line", 0),
            line_end=c.get("end_line", 0),
            content=c.get("content", "")[:500],
            relevance=round(0.85 - i * 0.05, 2),
        )
        for i, c in enumerate(code_chunks[:req.max_snippets])
    ]

    sops = _learnings_to_sops(learnings, req.query)

    confidence = min(0.4 + 0.1 * len(files) + 0.1 * len(sops), 1.0)

    return ContextQueryResponse(
        files=files,
        snippets=snippets,
        related_sops=sops,
        confidence=round(confidence, 2),
        intent=req.intent.value,
        query_time_ms=round((time.monotonic() - t0) * 1000, 1),
    )


def _handle_trace(req: ContextQueryRequest) -> ContextQueryResponse:
    """Trace dependencies of a symbol."""
    t0 = time.monotonic()

    graph = _get_graph()
    deps = _trace_dependencies(req.query, graph)
    origin_nodes = _search_graph_nodes(req.query, max_results=1)

    files = []
    seen_paths = set()

    # Include the origin node
    for r in origin_nodes:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            files.append(FileResult(
                path=r["path"],
                relevance=1.0,
                summary=f"[ORIGIN] [{r['type']}] {r['name']}",
            ))

    # Include dependencies
    for r in deps:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            files.append(FileResult(
                path=r["path"],
                relevance=round(0.7, 2),
                summary=f"[DEP] [{r['type']}] {r['name']}",
            ))
        if len(files) >= req.max_files:
            break

    learnings = _search_learnings(req.query, limit=3)
    sops = _learnings_to_sops(learnings, req.query)

    confidence = 0.9 if origin_nodes else 0.3

    return ContextQueryResponse(
        files=files,
        snippets=[],
        related_sops=sops,
        confidence=round(confidence, 2),
        intent=req.intent.value,
        query_time_ms=round((time.monotonic() - t0) * 1000, 1),
    )


def _handle_impact(req: ContextQueryRequest) -> ContextQueryResponse:
    """Find callers/dependents of a symbol."""
    t0 = time.monotonic()

    graph = _get_graph()
    callers = _find_callers(req.query, graph)
    origin_nodes = _search_graph_nodes(req.query, max_results=1)

    files = []
    seen_paths = set()

    for r in origin_nodes:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            files.append(FileResult(
                path=r["path"],
                relevance=1.0,
                summary=f"[TARGET] [{r['type']}] {r['name']}",
            ))

    for r in callers:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            files.append(FileResult(
                path=r["path"],
                relevance=round(0.8, 2),
                summary=f"[CALLER] [{r['type']}] {r['name']}",
            ))
        if len(files) >= req.max_files:
            break

    learnings = _search_learnings(req.query, limit=3)
    sops = _learnings_to_sops(learnings, req.query)

    confidence = 0.85 if origin_nodes else 0.25

    return ContextQueryResponse(
        files=files,
        snippets=[],
        related_sops=sops,
        confidence=round(confidence, 2),
        intent=req.intent.value,
        query_time_ms=round((time.monotonic() - t0) * 1000, 1),
    )


def _handle_generic(req: ContextQueryRequest) -> ContextQueryResponse:
    """Generic handler for tests, deps, docs, decision intents.

    Combines FTS5 search + knowledge graph categorization.
    """
    t0 = time.monotonic()

    # Categorize the query for context
    kg = _get_knowledge_graph()
    category = kg.categorize(req.query)

    graph_results = _search_graph_nodes(req.query, max_results=req.max_files)
    code_chunks = _search_code_chunks(req.query, limit=req.max_snippets)
    learnings = _search_learnings(req.query, limit=10)

    # For "tests" intent, boost test files
    if req.intent == QueryIntent.tests:
        # Also search for test files specifically
        test_query = req.query + " test spec"
        extra = _search_code_chunks(test_query, limit=req.max_snippets)
        code_chunks = code_chunks + extra

    files = []
    seen_paths = set()
    for r in graph_results:
        if r["path"] not in seen_paths:
            seen_paths.add(r["path"])
            summary = f"[{r['type']}] {r['name']}"
            if r.get("category"):
                summary += f" ({r['category']})"
            files.append(FileResult(
                path=r["path"],
                relevance=round(r.get("score", 0.5), 2),
                summary=summary,
            ))
        if len(files) >= req.max_files:
            break

    snippets = [
        SnippetResult(
            file=c["file_path"],
            line_start=c.get("start_line", 0),
            line_end=c.get("end_line", 0),
            content=c.get("content", "")[:500],
            relevance=round(0.75 - i * 0.05, 2),
        )
        for i, c in enumerate(code_chunks[:req.max_snippets])
    ]

    sops = _learnings_to_sops(learnings, req.query)

    # Add category context to the first SOP if we have one
    if category and category != "Gotchas":
        cat_desc = kg.get_category_description(category)
        sops.insert(0, SOPResult(
            title=f"Category: {category}",
            summary=cat_desc or category,
            relevance=0.6,
        ))

    confidence = min(0.3 + 0.1 * len(files) + 0.1 * len(snippets) + 0.05 * len(sops), 1.0)

    return ContextQueryResponse(
        files=files,
        snippets=snippets,
        related_sops=sops,
        confidence=round(confidence, 2),
        intent=req.intent.value,
        query_time_ms=round((time.monotonic() - t0) * 1000, 1),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _learnings_to_sops(learnings: List[Dict], query: str) -> List[SOPResult]:
    """Convert learning results to SOPResult list."""
    query_lower = query.lower()
    sops = []
    for lr in learnings:
        title = lr.get("title", "")
        content = lr.get("content", "")
        lr_type = lr.get("type", "learning")

        # Simple relevance: how many query words appear
        text = f"{title} {content}".lower()
        query_words = set(query_lower.split())
        if query_words:
            overlap = sum(1 for w in query_words if w in text)
            relevance = min(overlap / len(query_words), 1.0)
        else:
            relevance = 0.5

        sops.append(SOPResult(
            title=f"[{lr_type}] {title}",
            summary=content[:200],
            relevance=round(relevance, 2),
        ))

    sops.sort(key=lambda s: s.relevance, reverse=True)
    return sops[:5]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_INTENT_HANDLERS = {
    QueryIntent.locate: _handle_locate,
    QueryIntent.explain: _handle_explain,
    QueryIntent.trace: _handle_trace,
    QueryIntent.impact: _handle_impact,
    QueryIntent.tests: _handle_generic,
    QueryIntent.deps: _handle_generic,
    QueryIntent.docs: _handle_generic,
    QueryIntent.decision: _handle_generic,
}


def create_router() -> "APIRouter":
    """Create the FastAPI router for the Repo Librarian endpoint."""
    router = APIRouter(prefix="/v1/context", tags=["librarian"])

    @router.post("/query", response_model=ContextQueryResponse)
    async def query_context(req: ContextQueryRequest) -> ContextQueryResponse:
        """Unified codebase query endpoint for AI agents.

        Searches architecture graph, code chunks, learnings, and knowledge
        graph to answer agent queries about the codebase.
        """
        logger.info(f"Librarian query from {req.agent_id}: [{req.intent.value}] {req.query[:80]}")

        handler = _INTENT_HANDLERS.get(req.intent, _handle_generic)
        try:
            response = handler(req)
        except Exception as e:
            logger.error(f"Librarian query failed: {e}", exc_info=True)
            response = ContextQueryResponse(
                files=[],
                snippets=[],
                related_sops=[],
                confidence=0.0,
                intent=req.intent.value,
                query_time_ms=0.0,
            )

        logger.info(
            f"Librarian response: {len(response.files)} files, "
            f"{len(response.snippets)} snippets, "
            f"confidence={response.confidence}, "
            f"{response.query_time_ms}ms"
        )
        return response

    @router.get("/health")
    async def librarian_health():
        """Health check for the librarian subsystem."""
        graph_ok = False
        sqlite_ok = False
        kg_ok = False
        try:
            g = _get_graph()
            graph_ok = g is not None and len(g.nodes) > 0
        except Exception:
            pass
        try:
            s = _get_sqlite_storage()
            sqlite_ok = s is not None
        except Exception:
            pass
        try:
            k = _get_knowledge_graph()
            kg_ok = k is not None
        except Exception:
            pass

        return {
            "status": "ok" if (graph_ok or sqlite_ok) else "degraded",
            "graph_nodes": len(g.nodes) if graph_ok else 0,
            "graph_edges": len(g.edges) if graph_ok else 0,
            "sqlite_fts5": sqlite_ok,
            "knowledge_graph": kg_ok,
        }

    return router


# Convenience: pre-built router instance for import
if FASTAPI_AVAILABLE:
    router = create_router()
