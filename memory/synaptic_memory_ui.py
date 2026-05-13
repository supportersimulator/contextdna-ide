#!/usr/bin/env python3
"""
Synaptic Memory Interface - NOT a chatbot

Direct access to Synaptic's memory systems:
- Session intents (what you're working on)
- Learnings (past discoveries)
- Patterns (active patterns)
- Brain state (current architecture)

No LLM. Just data. You already have Claude.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="Synaptic Memory Interface")

# =============================================================================
# DATA ACCESS
# =============================================================================

def get_session_intents(limit: int = 20) -> List[Dict]:
    """Get recent session intents."""
    db_path = Path.home() / ".context-dna" / ".session_intents.db"
    if not db_path.exists():
        return []

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("""
            SELECT session_id, intent, started_at, ended_at, outcome, tags
            FROM sessions
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [{
        "id": r[0],
        "intent": r[1],
        "started": r[2],
        "ended": r[3],
        "outcome": r[4],
        "tags": json.loads(r[5]) if r[5] else []
    } for r in rows]


def get_learnings(query: str = None, limit: int = 20) -> List[Dict]:
    """Get learnings from pattern evolution DB."""
    db_path = Path.home() / ".context-dna" / ".pattern_evolution.db"
    if not db_path.exists():
        return []

    with sqlite3.connect(str(db_path)) as conn:
        if query:
            rows = conn.execute("""
                SELECT id, title, category, created_at, content
                FROM learnings
                WHERE title LIKE ? OR content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, title, category, created_at, content
                FROM learnings
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

    return [{
        "id": r[0],
        "title": r[1],
        "category": r[2],
        "created": r[3],
        "content": r[4][:200] + "..." if r[4] and len(r[4]) > 200 else r[4]
    } for r in rows]


def get_brain_state() -> str:
    """Get current brain state."""
    brain_path = Path.home() / ".context-dna" / "brain_state.md"
    if brain_path.exists():
        return brain_path.read_text()[:2000]
    return "No brain state found."


def get_current_context() -> Dict:
    """Get current working context."""
    intents = get_session_intents(5)
    current = intents[0] if intents else None

    return {
        "current_session": current,
        "recent_sessions": intents[1:4] if len(intents) > 1 else [],
        "timestamp": datetime.now().isoformat()
    }


def search_all(query: str) -> Dict:
    """Search across all memory systems."""
    return {
        "sessions": get_session_intents(10),  # Would filter by query
        "learnings": get_learnings(query, 10),
        "query": query
    }


# =============================================================================
# HTML INTERFACE - Clean, data-focused
# =============================================================================

HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Synaptic Memory</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
            max-width: 900px;
            margin: 0 auto;
        }
        h1 { color: #58a6ff; margin-bottom: 20px; font-size: 24px; }
        h2 { color: #8b949e; margin: 20px 0 10px; font-size: 16px; text-transform: uppercase; }

        .search {
            width: 100%;
            padding: 12px 16px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            color: #c9d1d9;
            font-size: 16px;
            margin-bottom: 20px;
        }
        .search:focus { outline: none; border-color: #58a6ff; }

        .card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .card.current {
            border-left: 3px solid #3fb950;
        }
        .card-title {
            color: #f0f6fc;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .card-meta {
            color: #8b949e;
            font-size: 13px;
        }
        .card-content {
            margin-top: 8px;
            font-size: 14px;
            line-height: 1.5;
        }
        .tag {
            display: inline-block;
            background: #21262d;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            margin-right: 4px;
            color: #8b949e;
        }
        .outcome {
            color: #3fb950;
            margin-top: 8px;
        }
        .section {
            margin-bottom: 30px;
        }
        .brain-state {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 16px;
            font-family: monospace;
            font-size: 13px;
            white-space: pre-wrap;
            max-height: 300px;
            overflow-y: auto;
        }
        .refresh-btn {
            background: #21262d;
            border: 1px solid #30363d;
            color: #c9d1d9;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            float: right;
        }
        .refresh-btn:hover { background: #30363d; }
        .empty { color: #8b949e; font-style: italic; }
    </style>
</head>
<body>
    <h1>Synaptic Memory <button class="refresh-btn" onclick="loadAll()">Refresh</button></h1>

    <input type="text" class="search" placeholder="Search memories..." id="search" onkeyup="handleSearch(event)">

    <div class="section">
        <h2>Current Work Session</h2>
        <div id="current"></div>
    </div>

    <div class="section">
        <h2>Recent Sessions</h2>
        <div id="sessions"></div>
    </div>

    <div class="section">
        <h2>Recent Learnings</h2>
        <div id="learnings"></div>
    </div>

    <div class="section">
        <h2>Brain State</h2>
        <div id="brain" class="brain-state"></div>
    </div>

<script>
async function loadAll() {
    // Load current context
    const ctx = await fetch('/api/context').then(r => r.json());

    if (ctx.current_session) {
        document.getElementById('current').innerHTML = renderSession(ctx.current_session, true);
    } else {
        document.getElementById('current').innerHTML = '<div class="empty">No active session. Start one with: python memory/session_intent.py start "your intent"</div>';
    }

    if (ctx.recent_sessions.length > 0) {
        document.getElementById('sessions').innerHTML = ctx.recent_sessions.map(s => renderSession(s)).join('');
    } else {
        document.getElementById('sessions').innerHTML = '<div class="empty">No recent sessions</div>';
    }

    // Load learnings
    const learnings = await fetch('/api/learnings').then(r => r.json());
    if (learnings.length > 0) {
        document.getElementById('learnings').innerHTML = learnings.map(renderLearning).join('');
    } else {
        document.getElementById('learnings').innerHTML = '<div class="empty">No learnings recorded yet</div>';
    }

    // Load brain state
    const brain = await fetch('/api/brain').then(r => r.json());
    document.getElementById('brain').textContent = brain.content;
}

function renderSession(s, isCurrent = false) {
    const tags = s.tags ? s.tags.map(t => `<span class="tag">${t}</span>`).join('') : '';
    const outcome = s.outcome ? `<div class="outcome">→ ${s.outcome}</div>` : '';
    const date = s.started ? new Date(s.started).toLocaleDateString() : '';

    return `
        <div class="card ${isCurrent ? 'current' : ''}">
            <div class="card-title">${s.intent || 'No intent'}</div>
            <div class="card-meta">${date} ${tags}</div>
            ${outcome}
        </div>
    `;
}

function renderLearning(l) {
    return `
        <div class="card">
            <div class="card-title">${l.title || 'Untitled'}</div>
            <div class="card-meta">${l.category || 'general'} • ${l.created ? new Date(l.created).toLocaleDateString() : ''}</div>
            ${l.content ? `<div class="card-content">${l.content}</div>` : ''}
        </div>
    `;
}

async function handleSearch(event) {
    if (event.key === 'Enter') {
        const query = document.getElementById('search').value;
        const results = await fetch('/api/search?q=' + encodeURIComponent(query)).then(r => r.json());

        document.getElementById('learnings').innerHTML = results.learnings.length > 0
            ? results.learnings.map(renderLearning).join('')
            : '<div class="empty">No results</div>';
    }
}

// Load on start
loadAll();
</script>
</body>
</html>"""


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/context")
async def api_context():
    return get_current_context()


@app.get("/api/sessions")
async def api_sessions(limit: int = 20):
    return get_session_intents(limit)


@app.get("/api/learnings")
async def api_learnings(q: str = None, limit: int = 20):
    return get_learnings(q, limit)


@app.get("/api/brain")
async def api_brain():
    return {"content": get_brain_state()}


@app.get("/api/search")
async def api_search(q: str):
    return search_all(q)


@app.get("/health")
async def health():
    return {"status": "ready", "type": "memory_interface"}


if __name__ == "__main__":
    print("Synaptic Memory Interface")
    print("Not a chatbot. Just data.")
    print("http://localhost:8889")
    uvicorn.run(app, host="0.0.0.0", port=8889, log_level="warning")
