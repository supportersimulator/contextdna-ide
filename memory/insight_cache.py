"""
Insight Cache - Provides cached insights for 8th Intelligence fallback.

Used by persistent_hook_structure.py (Method 3) when Synaptic Voice API
and brain_state file are unavailable. Reads from the observability store's
claim table to surface recent high-confidence learnings.
"""
import json
from pathlib import Path
from typing import List, Dict, Any, Optional


def get_cached_insights(limit: int = 3) -> List[Dict[str, Any]]:
    """
    Retrieve recent high-confidence insights from the observability store.

    Falls back to brain_state.md parsing if the store is unavailable.

    Args:
        limit: Maximum number of insights to return

    Returns:
        List of insight dicts with 'text' and 'confidence' keys
    """
    # Method 1: Try observability store (claims with high confidence)
    try:
        from memory.observability_store import get_observability_store
        store = get_observability_store()
        cursor = store._sqlite_conn.execute("""
            SELECT statement, weighted_confidence, area, tags_json
            FROM claim
            WHERE status = 'active' AND weighted_confidence > 0.6
            ORDER BY created_at_utc DESC
            LIMIT ?
        """, (limit,))

        insights = []
        for row in cursor:
            insights.append({
                "text": row[0],
                "confidence": row[1],
                "area": row[2],
                "tags": json.loads(row[3]) if row[3] else [],
            })

        if insights:
            return insights
    except Exception as e:
        print(f"[WARN] Insight cache DB query failed: {e}")

    # Method 2: Parse brain_state.md for patterns
    try:
        brain_path = Path(__file__).parent / "brain_state.md"
        if brain_path.exists():
            content = brain_path.read_text()
            insights = []
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("- ") and len(line) > 10:
                    insights.append({
                        "text": line[2:],
                        "confidence": 0.5,
                        "area": "brain_state",
                        "tags": [],
                    })
                    if len(insights) >= limit:
                        break
            return insights
    except Exception as e:
        print(f"[WARN] Brain state insight extraction failed: {e}")

    return []
