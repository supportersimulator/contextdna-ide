#!/usr/bin/env python3
"""
WEBHOOK MANIFEST — Payload manifest emission for injection audit trail.

Extracted from persistent_hook_structure.py (God File Phase 1).

Functions:
- _emit_manifest() — Write manifest to .projectdna vault with chain-hashed audit
"""

import json
import logging
import sys
from pathlib import Path

from memory.webhook_types import PayloadManifest

logger = logging.getLogger('context_dna')


def _safe_log(level: str, message: str):
    """Per-section logging with stderr fallback."""
    try:
        if level == 'warning':
            logger.warning(message)
        elif level == 'debug':
            logger.debug(message)
        else:
            logger.info(message)
    except Exception:
        print(f"[{level.upper()}] {message}", file=sys.stderr)


def _emit_manifest(manifest: PayloadManifest):
    """Write manifest to .projectdna/derived/last_manifest.json. Non-blocking, fail-open."""
    try:
        vault_dir = Path(__file__).parent.parent / ".projectdna" / "derived"
        if not vault_dir.exists():
            vault_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = vault_dir / "last_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, separators=(",", ": ")) + "\n"
        )

        # Log event via EventedWriteService (chain-hashed audit)
        try:
            from memory.evented_write import EventedWriteService
            ews = EventedWriteService.get_instance()
            ews._emit_event(
                store_name="payload_manifest",
                method_name="emit",
                summary={
                    "injection_id": manifest.injection_id,
                    "injection_count": manifest.injection_count,
                    "depth": manifest.depth,
                    "included_count": len(manifest.included),
                    "excluded_count": len(manifest.excluded),
                    "total_tokens_est": manifest.total_tokens_est,
                    "movement": 3,
                },
            )
        except Exception:
            pass  # Fail-open: manifest file already written
    except Exception as e:
        _safe_log('debug', f"Manifest emit failed (non-blocking): {e}")
