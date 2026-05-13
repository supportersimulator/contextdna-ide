#!/usr/bin/env python3
"""
Quick CLI to query Context DNA memory with RECENCY WEIGHTING.

Usage:
    python memory/query.py "async boto3 performance"
    python memory/query.py "WebRTC cloudflare"
    python memory/query.py "docker environment"

Recency Weighting:
    - SOPs from last 24h: +20% relevance boost
    - SOPs from last week: +10% relevance boost
    - SOPs from last month: +5% relevance boost
    - Older SOPs: no boost (pure semantic relevance)
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.context_dna_client import ContextDNAClient, CONTEXT_DNA_AVAILABLE


def get_sop_timestamps(sop_ids: list) -> dict:
    """Get created_at timestamps for SOPs from database.

    Falls back gracefully if docker/database isn't available.
    Recency weighting is a nice-to-have, not a requirement.

    PERFORMANCE: Uses fast-fail check - returns {} in <0.5s if docker unavailable.
    """
    if not sop_ids:
        return {}

    try:
        import shutil
        # Check if docker is available
        if not shutil.which('docker'):
            return {}  # No docker, skip recency weighting

        # FAST-FAIL: Check if container is running BEFORE trying exec
        # This avoids the 5+ second hang when container is not running
        check = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Running}}', 'contextdna-pg'],
            capture_output=True, text=True, timeout=1
        )
        if check.returncode != 0 or check.stdout.strip() != 'true':
            return {}  # Container not running, skip recency weighting

        id_list = ','.join(f"'{id}'" for id in sop_ids)
        result = subprocess.run([
            'docker', 'exec', 'contextdna-pg',
            'psql', '-U', 'postgres', '-d', 'acontext', '-t', '-A', '-c',
            f"SELECT id, created_at FROM blocks WHERE id IN ({id_list})"
        ], capture_output=True, text=True, timeout=2)  # Reduced timeout to 2s

        if result.returncode != 0:
            return {}  # Docker command failed, skip recency weighting

        timestamps = {}
        for line in result.stdout.strip().split('\n'):
            if '|' in line:
                parts = line.split('|')
                try:
                    # Parse timestamp (e.g., "2026-01-23 14:59:15.385196+00")
                    ts_str = parts[1].split('+')[0].split('.')[0]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    ts = ts.replace(tzinfo=timezone.utc)
                    timestamps[parts[0]] = ts
                except Exception as e:
                    print(f"[WARN] Timestamp parse failed: {e}")
        return timestamps
    except Exception:
        return {}  # Any error, skip recency weighting gracefully


def apply_recency_boost(lessons: list, timestamps: dict) -> list:
    """Apply recency boost to lesson scores.

    Boost scheme:
    - Last 24h: +20% relevance
    - Last week: +10% relevance
    - Last month: +5% relevance
    """
    now = datetime.now(timezone.utc)

    for lesson in lessons:
        sop_id = lesson.get('id')
        if sop_id in timestamps:
            created_at = timestamps[sop_id]
            age = now - created_at

            # Calculate boost based on recency
            boost = 0
            if age < timedelta(hours=24):
                boost = 0.20  # 20% boost for last 24h
                lesson['recency'] = '🔥 <24h'
            elif age < timedelta(days=7):
                boost = 0.10  # 10% boost for last week
                lesson['recency'] = '📅 <1w'
            elif age < timedelta(days=30):
                boost = 0.05  # 5% boost for last month
                lesson['recency'] = '📆 <1mo'
            else:
                lesson['recency'] = ''

            # Adjust distance (lower is better, so subtract boost)
            # distance 0 = perfect match, distance 1 = no match
            original_distance = lesson.get('distance', 0.5)
            lesson['original_distance'] = original_distance
            lesson['distance'] = max(0, original_distance - boost)

    # Re-sort by adjusted distance
    lessons.sort(key=lambda x: x.get('distance', 1))
    return lessons


def confidence_tier(learning: dict) -> str:
    """Classify learning provenance: unverified / observed / confirmed.

    - unverified: gold-mined <48h ago with no quality_score
    - observed: gold-mined but aged 48h+ OR quality_score >= 0.6
    - confirmed: not gold-mined (human, auto_capture, manual, etc.)
    """
    source = (learning.get('source') or '')
    if not source.startswith('gold_pass'):
        return 'confirmed'
    meta = learning.get('metadata') or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    if meta.get('quality_score', 0) >= 0.6:
        return 'observed'
    ts = learning.get('timestamp') or learning.get('created_at') or ''
    if ts:
        try:
            created = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - created) > timedelta(hours=48):
                return 'observed'
        except Exception:
            pass
    return 'unverified'


_TIER_RANK = {'confirmed': 2, 'observed': 1, 'unverified': 0}


def query_learnings(query: str, limit: int = 10, min_confidence: str = None) -> list:
    """
    Query Context DNA memory and return relevant learnings.

    This is the programmatic interface for the query system.
    Used by agent_service.py for automatic context injection.

    Args:
        query: Search query text
        limit: Maximum number of results

    Returns:
        List of formatted learning strings
    """
    try:
        memory = ContextDNAClient()
    except Exception:
        return []

    # Get initial results
    lessons = memory.get_relevant_learnings(query, limit=limit + 5)

    if not lessons:
        return []

    # Provenance filter: exclude unverified gold-mined learnings
    if min_confidence and min_confidence in _TIER_RANK:
        min_rank = _TIER_RANK[min_confidence]
        lessons = [l for l in lessons if _TIER_RANK.get(confidence_tier(l), 0) >= min_rank]

    # Apply recency weighting
    sop_ids = [l.get('id') for l in lessons if l.get('id')]
    timestamps = get_sop_timestamps(sop_ids)
    lessons = apply_recency_boost(lessons, timestamps)

    # Format results for injection
    results = []
    for lesson in lessons[:limit]:
        recency = lesson.get('recency', '')
        title = lesson.get('title', '')
        use_when = lesson.get('use_when', '')
        preferences = lesson.get('preferences', '')

        # Build formatted string
        parts = [title]
        if recency:
            parts[0] = f"{recency} {title}"
        if use_when:
            parts.append(f"When: {use_when[:100]}")
        if preferences:
            parts.append(f"Key: {preferences[:150]}")

        results.append(" | ".join(parts))

    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python memory/query.py <search query>")
        print("\nExamples:")
        print('  python memory/query.py "async boto3"')
        print('  python memory/query.py "GPU IP networking"')
        print('  python memory/query.py "docker restart"')
        sys.exit(1)

    query = " ".join(sys.argv[1:])

    try:
        memory = ContextDNAClient()
    except Exception as e:
        print(f"Error: Could not connect to Context DNA. Is it running?")
        print(f"Start with: ./scripts/context-dna up")
        print(f"\nDetails: {e}")
        sys.exit(1)

    print(f"Searching for: '{query}'\n")
    print("=" * 60)

    # Get initial results
    lessons = memory.get_relevant_learnings(query, limit=10)  # Get more, then filter

    if not lessons:
        print("No relevant learnings found.")
        print("\nTry broader search terms or check if Context DNA has been seeded:")
        print("  python memory/seed_acontext.py")
        sys.exit(0)

    # Apply recency weighting
    sop_ids = [l.get('id') for l in lessons if l.get('id')]
    timestamps = get_sop_timestamps(sop_ids)
    lessons = apply_recency_boost(lessons, timestamps)

    # Show top 5 after recency adjustment
    for i, lesson in enumerate(lessons[:5], 1):
        recency = lesson.get('recency', '')
        title = lesson['title']
        print(f"\n{i}. {title}")
        if recency:
            print(f"   {recency} (recency boost applied)")
        print(f"   Type: {lesson['type']}")
        print(f"   When: {lesson.get('use_when', 'N/A')}")
        if lesson.get('preferences'):
            pref = lesson['preferences']
            print(f"   Key: {pref[:150]}..." if len(pref) > 150 else f"   Key: {pref}")
        if 'distance' in lesson:
            relevance = 1 - lesson['distance']
            print(f"   Relevance: {relevance:.1%}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
