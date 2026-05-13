"""Shared test setup for tools/tests.

Keeps tests isolated — adds REPO_ROOT to sys.path so imports like
`tools.fleet_idle_watcher` and `multifleet.jetstream` resolve.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLEET_ROOT = REPO_ROOT / "multi-fleet"

for p in (REPO_ROOT, FLEET_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
