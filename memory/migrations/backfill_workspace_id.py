"""Backfill workspace_id for existing learnings.

All pre-existing learnings are tagged as 'contextdna_dev' since they were
created during ContextDNA development (before workspace isolation existed).
"""
import sqlite3


def backfill_workspace_id(conn: sqlite3.Connection, default_workspace: str = 'contextdna_dev') -> int:
    """Tag all learnings with empty workspace_id.

    Args:
        conn: SQLite connection
        default_workspace: workspace_id to assign (default: 'contextdna_dev')

    Returns:
        Number of rows updated
    """
    cursor = conn.execute(
        "UPDATE learnings SET workspace_id = ? WHERE workspace_id = ''",
        (default_workspace,)
    )
    return cursor.rowcount
