"""ContextDNA IDE — AI subconscious mind for persistent context memory.

This package provides the user-facing CLI seam for the mothership.
Heavy logic lives in the mothership root (memory/, scripts/, engine/);
this package is a thin, install-friendly entrypoint that any clone can
invoke after `pip install context-dna-ide` (or from the wheel build).

Zero Silent Failures (ZSF):
    Every exception in this CLI surfaces to the user with an exit code
    and a one-line "what failed / what to try" message. No bare except.
"""
from __future__ import annotations

__all__ = ["__version__"]

# Version is the single source of truth for `context-dna-ide --version`.
# Bump in lockstep with pyproject.toml [project].version. We read from
# importlib.metadata first (wheel installs), then fall back to the
# string below for editable / source-tree runs.
__version__: str = "0.1.0"

try:
    # When installed as a wheel, prefer the metadata version so the
    # CLI never drifts from pyproject.toml.
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("context-dna-ide")
    except PackageNotFoundError:
        # Source-tree run (no metadata yet) — keep the hard-coded fallback.
        pass
except Exception:  # pragma: no cover — importlib.metadata always available on 3.10+
    # ZSF: don't crash import just because version lookup failed.
    pass
