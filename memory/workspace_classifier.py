"""
Workspace classifier -- identifies workspace kind and generates stable IDs.

Workspace kinds:
  - contextdna_dev: ContextDNA's own repo (has manifest.yaml + llm_priority_queue.py)
  - user_project: A user's project (has package.json / pyproject.toml / .git)
  - unknown: Cannot determine
"""
import hashlib
from pathlib import Path
from typing import Literal

WorkspaceKind = Literal['contextdna_dev', 'user_project', 'unknown']

# Markers that identify ContextDNA's own dev repo
_CONTEXTDNA_MARKERS = [
    '.projectdna/manifest.yaml',
    'memory/llm_priority_queue.py',
    'context-dna/engine',
]

# Markers that identify any project
_PROJECT_MARKERS = [
    'package.json',
    'pyproject.toml',
    '.git',
    'Cargo.toml',
    'go.mod',
    'pom.xml',
]

# Minimum ContextDNA markers to classify as contextdna_dev
_CONTEXTDNA_THRESHOLD = 2


def classify_workspace(project_root: Path) -> WorkspaceKind:
    """Classify a workspace by examining marker files.

    Args:
        project_root: Root directory of the project

    Returns:
        WorkspaceKind: 'contextdna_dev', 'user_project', or 'unknown'
    """
    root = Path(project_root)
    if not root.is_dir():
        return 'unknown'

    # Check ContextDNA dev markers
    cdna_score = sum(
        1 for marker in _CONTEXTDNA_MARKERS
        if (root / marker).exists()
    )
    if cdna_score >= _CONTEXTDNA_THRESHOLD:
        return 'contextdna_dev'

    # Check generic project markers
    for marker in _PROJECT_MARKERS:
        if (root / marker).exists():
            return 'user_project'

    return 'unknown'


def get_workspace_id(project_root: Path) -> str:
    """Generate a stable workspace ID from the project root path.

    The ID is a 16-char hex hash of the resolved absolute path.
    Deterministic: same path always produces same ID.

    Args:
        project_root: Root directory of the project

    Returns:
        16-character hex string
    """
    resolved = str(Path(project_root).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]
