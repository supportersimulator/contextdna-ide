"""services.shadow_mode — instrumented production shadow-mode reader.

See :mod:`services.shadow_mode.shadow_reader` for the full module docstring.
This package exists so the shadow reader is importable as
``services.shadow_mode.shadow_reader`` from anywhere on ``sys.path``.
"""

from __future__ import annotations

__all__ = ["shadow_reader"]
