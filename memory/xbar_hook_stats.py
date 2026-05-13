#!/usr/bin/env python3
"""
xbar Hook Stats - Fast data provider for xbar menu
Outputs structured data that xbar can easily parse
"""

import sqlite3
import json
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / '.pattern_evolution.db'

def get_hook_performance():
    """Get hook variant performance stats for xbar display."""
    if not DB_PATH.exists():
        return {"error": "Database not found"}

    from memory.db_utils import connect_wal
    conn = connect_wal(str(DB_PATH))
    cursor = conn.cursor()

    results = {
        "variants": [],
        "running_tests": [],
        "pattern_stats": {},
        "wisdom_injections": 0
    }

    try:
        # Get variant performance
        cursor.execute('''
            SELECT
                hv.variant_id,
                hv.variant_name,
                hv.hook_type,
                hv.ab_group,
                hv.change_magnitude,
                hv.is_active,
                COUNT(ho.id) as samples,
                SUM(CASE WHEN ho.outcome = 'positive' THEN 1 ELSE 0 END) as positive,
                SUM(CASE WHEN ho.outcome = 'negative' THEN 1 ELSE 0 END) as negative
            FROM hook_variants hv
            LEFT JOIN hook_outcomes ho ON hv.variant_id = ho.variant_id
            WHERE hv.is_active = 1
            GROUP BY hv.variant_id
            ORDER BY samples DESC
        ''')

        for row in cursor.fetchall():
            samples = row[6] or 0
            positive = row[7] or 0
            rate = (positive / samples * 100) if samples > 0 else 0
            results["variants"].append({
                "id": row[0],
                "name": row[1],
                "hook_type": row[2],
                "group": row[3] or "control",
                "magnitude": row[4] or "baseline",
                "samples": samples,
                "positive": positive,
                "rate": round(rate, 1)
            })

        # Get running A/B tests
        cursor.execute('''
            SELECT test_name, hook_type,
                   control_variant_id, variant_a_id, variant_b_id, variant_c_id,
                   min_samples_per_variant
            FROM hook_ab_tests
            WHERE status = 'running'
        ''')

        for row in cursor.fetchall():
            test = {
                "name": row[0],
                "hook_type": row[1],
                "control": row[2],
                "variant_a": row[3],
                "variant_b": row[4],
                "variant_c": row[5],
                "min_samples": row[6]
            }
            results["running_tests"].append(test)

        # Get prompt pattern stats
        cursor.execute('SELECT COUNT(*) FROM prompt_patterns WHERE is_active = 1')
        results["pattern_stats"]["active_patterns"] = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM prompt_pattern_outcomes')
        results["pattern_stats"]["total_outcomes"] = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*) FROM prompt_pattern_outcomes WHERE outcome = 'positive'
        ''')
        positive_outcomes = cursor.fetchone()[0]
        total_outcomes = results["pattern_stats"]["total_outcomes"]
        results["pattern_stats"]["positive_rate"] = round(
            (positive_outcomes / total_outcomes * 100) if total_outcomes > 0 else 0, 1
        )

        # Get wisdom injection count
        cursor.execute('SELECT COUNT(*) FROM wisdom_injections')
        results["wisdom_injections"] = cursor.fetchone()[0]

        # Get contextual learning stats
        cursor.execute('SELECT COUNT(DISTINCT primary_context) FROM session_context')
        results["pattern_stats"]["contexts_learned"] = cursor.fetchone()[0]

    except sqlite3.OperationalError as e:
        results["error"] = str(e)
    finally:
        conn.close()

    return results


def get_dedup_candidates():
    """Find potential duplicate learnings."""
    if not DB_PATH.exists():
        return {"duplicates": [], "error": "Database not found"}

    from memory.db_utils import connect_wal
    conn = connect_wal(str(DB_PATH))
    cursor = conn.cursor()

    duplicates = []

    try:
        # Find patterns with similar names
        cursor.execute('''
            SELECT p1.name, p2.name, p1.category
            FROM prompt_patterns p1
            JOIN prompt_patterns p2 ON p1.id < p2.id
            WHERE p1.category = p2.category
            AND (
                p1.name LIKE '%' || p2.name || '%'
                OR p2.name LIKE '%' || p1.name || '%'
                OR LENGTH(p1.name) - LENGTH(REPLACE(LOWER(p1.name), LOWER(p2.name), '')) > 3
            )
        ''')

        for row in cursor.fetchall():
            duplicates.append({
                "pattern1": row[0],
                "pattern2": row[1],
                "category": row[2],
                "type": "similar_name"
            })

        # Future: Add semantic similarity checking with embeddings

    except sqlite3.OperationalError as e:
        return {"duplicates": [], "error": str(e)}
    finally:
        conn.close()

    return {"duplicates": duplicates, "count": len(duplicates)}


def print_xbar_format():
    """Print stats in xbar-friendly format for shell parsing."""
    stats = get_hook_performance()

    if "error" in stats:
        print(f"ERROR:{stats['error']}")
        return

    # Summary line
    active_tests = len(stats["running_tests"])
    total_samples = sum(v["samples"] for v in stats["variants"])
    avg_rate = sum(v["rate"] for v in stats["variants"]) / len(stats["variants"]) if stats["variants"] else 0

    print(f"TESTS:{active_tests}")
    print(f"SAMPLES:{total_samples}")
    print(f"AVGRATE:{avg_rate:.0f}")
    print(f"PATTERNS:{stats['pattern_stats'].get('active_patterns', 0)}")
    print(f"WISDOM:{stats['wisdom_injections']}")
    print(f"CONTEXTS:{stats['pattern_stats'].get('contexts_learned', 0)}")

    # Variant details
    for v in stats["variants"]:
        print(f"VARIANT:{v['name']}|{v['group']}|{v['samples']}|{v['rate']}")

    # Running tests
    for t in stats["running_tests"]:
        print(f"TEST:{t['name']}|{t['hook_type']}|{t['min_samples']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--json":
            print(json.dumps(get_hook_performance(), indent=2))
        elif sys.argv[1] == "--dedup":
            print(json.dumps(get_dedup_candidates(), indent=2))
        elif sys.argv[1] == "--xbar":
            print_xbar_format()
    else:
        print_xbar_format()
