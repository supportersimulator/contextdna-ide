"""
EventedWriteService — Chain-hashed event gate for core store mutations.

Movement 2 of 9. Zero behavioral change on day one.

Design:
- Wraps store write methods with transparent event logging
- Chain-hashed append-only JSONL log (.projectdna/events.jsonl)
- If event logging fails, original write still proceeds (fail-open)
- Event summarizes args (never stores full payloads)
- Original methods preserved as _evented_original_<name>

Gated stores (day one):
- SQLiteStorage: store_learning, record_negative_pattern
- InjectionStore: store_injection
- ObservabilityStore: record_claim_with_evidence, update_claim_status,
    record_direct_claim_outcome, auto_upgrade_evidence_grade,
    record_webhook_delivery, record_injection_event,
    record_outcome_event, record_determinism_violation

Not gated (day two):
- LearningStore: delegates to SQLiteStorage (would double-log)
- SessionHistorian: no singleton, many instantiation points
- ArtifactStore: external SeaweedFS, lower priority
"""

import functools
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

EVENTS_PATH = Path(__file__).parent.parent / ".projectdna" / "events.jsonl"

# Methods to gate per store.
# tier1 = knowledge mutations (must gate), tier2 = system events (should gate)
GATE_REGISTRY = {
    "sqlite_storage": {
        "tier1": ["store_learning", "record_negative_pattern"],
    },
    "injection_store": {
        "tier1": ["store_injection"],
    },
    "observability_store": {
        "tier1": [
            "record_claim_with_evidence",
            "update_claim_status",
            "record_direct_claim_outcome",
            "auto_upgrade_evidence_grade",
        ],
        "tier2": [
            "record_webhook_delivery",
            "record_injection_event",
            "record_outcome_event",
            "record_determinism_violation",
        ],
    },
}


class EventedWriteService:
    """
    Chain-hashed event gate for all core store mutations.

    Wraps store methods transparently. If event logging fails,
    the original write still proceeds (fail-open for safety).
    """

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self, event_log_path=None):
        self.event_log_path = Path(event_log_path) if event_log_path else EVENTS_PATH
        self._write_lock = threading.Lock()
        self._gated_stores = {}
        self._event_count = 0
        self._enabled = True

        # Ensure parent dir exists
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Load chain tip or write GENESIS
        if not self.event_log_path.exists() or self.event_log_path.stat().st_size == 0:
            self._prev_hash = "0" * 64
            self._write_genesis()
        else:
            self._prev_hash = self._load_chain_tip()

    def _write_genesis(self):
        """Write the GENESIS event — first link in the chain."""
        genesis = {
            "id": "evt_genesis",
            "ts": datetime.now(timezone.utc).isoformat(),
            "store": "system",
            "method": "genesis",
            "summary": {"message": "EventedWriteService initialized", "movement": 2},
            "prev_hash": "0" * 64,
        }
        payload = json.dumps(genesis, sort_keys=True, separators=(",", ":"))
        genesis["hash"] = hashlib.sha256(
            f"{'0' * 64}:{payload}".encode()
        ).hexdigest()
        self._prev_hash = genesis["hash"]

        with open(self.event_log_path, "w") as f:
            f.write(json.dumps(genesis, separators=(",", ":")) + "\n")

        logger.info(
            f"EventedWriteService: GENESIS written → {self.event_log_path}"
        )

    def _load_chain_tip(self):
        """Load the hash from the last event in the log."""
        try:
            last_line = None
            with open(self.event_log_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                event = json.loads(last_line)
                return event.get("hash", "0" * 64)
        except Exception as e:
            logger.warning(f"EventedWriteService: chain tip load failed: {e}")
        return "0" * 64

    def _emit_event(self, store_name, method_name, summary, error=None):
        """Append a chain-hashed event to the log. Thread-safe, fail-open."""
        if not self._enabled:
            return None

        try:
            with self._write_lock:
                event = {
                    "id": f"evt_{uuid4().hex[:12]}",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "store": store_name,
                    "method": method_name,
                    "summary": summary,
                    "prev_hash": self._prev_hash,
                }
                if error:
                    event["error"] = str(error)[:200]

                # Chain hash: sha256(prev_hash + canonical_json)
                payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
                event["hash"] = hashlib.sha256(
                    f"{self._prev_hash}:{payload}".encode()
                ).hexdigest()
                self._prev_hash = event["hash"]
                self._event_count += 1

                with open(self.event_log_path, "a") as f:
                    f.write(json.dumps(event, separators=(",", ":")) + "\n")

                return event
        except Exception as e:
            # Fail-open: never block the actual write
            logger.warning(f"EventedWriteService: emit failed: {e}")
            return None

    def gate(self, store, store_name, methods=None):
        """
        Wrap specified methods on a store instance with event logging.

        The store object is modified in-place (methods replaced).
        Original methods preserved as _evented_original_<name>.
        If any wrapping fails, that method is left unchanged.

        Args:
            store: The store instance to gate
            store_name: Name for event log (e.g., "sqlite_storage")
            methods: List of method names. If None, uses GATE_REGISTRY.

        Returns:
            The same store instance (modified in-place)
        """
        if methods is None:
            registry = GATE_REGISTRY.get(store_name, {})
            methods = registry.get("tier1", []) + registry.get("tier2", [])

        gated = []
        for method_name in methods:
            original = getattr(store, method_name, None)
            if original is None:
                continue

            # Don't double-gate
            if hasattr(store, f"_evented_original_{method_name}"):
                continue

            try:
                # Preserve original
                setattr(store, f"_evented_original_{method_name}", original)

                # Create and attach wrapper
                wrapper = self._make_wrapper(store_name, method_name, original)
                setattr(store, method_name, wrapper)
                gated.append(method_name)
            except Exception as e:
                logger.warning(
                    f"EventedWriteService: failed to gate "
                    f"{store_name}.{method_name}: {e}"
                )

        self._gated_stores[store_name] = gated
        if gated:
            logger.info(
                f"EventedWriteService: gated {store_name} → {gated}"
            )
        return store

    def _make_wrapper(self, store_name, method_name, original):
        """Create a transparent wrapper that logs events around the original."""

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            summary = _summarize_call(store_name, method_name, args, kwargs)
            try:
                result = original(*args, **kwargs)
                result_info = _summarize_result(result)
                summary.update(result_info)
                self._emit_event(store_name, method_name, summary)
                return result
            except Exception as e:
                self._emit_event(store_name, method_name, summary, error=e)
                raise  # Always re-raise — we're transparent

        return wrapper

    def verify_chain(self):
        """Verify the integrity of the entire chain-hashed event log."""
        if not self.event_log_path.exists():
            return {"valid": False, "error": "Event log not found"}

        prev_hash = "0" * 64
        count = 0
        errors = []

        with open(self.event_log_path, "r") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError as e:
                    errors.append(f"Line {line_num}: invalid JSON: {e}")
                    continue

                stored_hash = event.pop("hash", None)
                expected_prev = event.get("prev_hash")

                if expected_prev != prev_hash:
                    errors.append(
                        f"Line {line_num}: prev_hash mismatch "
                        f"(expected {prev_hash[:12]}…, got {(expected_prev or '?')[:12]}…)"
                    )

                payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
                computed = hashlib.sha256(
                    f"{prev_hash}:{payload}".encode()
                ).hexdigest()

                if stored_hash != computed:
                    errors.append(
                        f"Line {line_num}: hash mismatch "
                        f"(expected {computed[:12]}…, got {(stored_hash or '?')[:12]}…)"
                    )

                prev_hash = stored_hash or computed
                count += 1

        return {
            "valid": len(errors) == 0,
            "events": count,
            "chain_tip": prev_hash[:16] + "…",
            "errors": errors[:10] if errors else [],
        }

    def stats(self):
        """Return event log statistics."""
        if not self.event_log_path.exists():
            return {"events": 0, "gated_stores": {}}

        stores = {}
        total = 0
        with open(self.event_log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    store = event.get("store", "unknown")
                    stores[store] = stores.get(store, 0) + 1
                    total += 1
                except json.JSONDecodeError:
                    pass

        return {
            "events": total,
            "by_store": stores,
            "gated_stores": self._gated_stores,
            "chain_tip": self._prev_hash[:16] + "…",
        }


# =========================================================================
# Call summarizers — extract identifying fields, never full payloads
# =========================================================================


def _summarize_call(store_name, method_name, args, kwargs):
    """Extract key identifying fields from method arguments."""
    summary = {}

    try:
        if store_name == "sqlite_storage":
            if method_name == "store_learning":
                data = args[0] if args else kwargs.get("learning_data", {})
                summary = {
                    "type": data.get("type"),
                    "title": (data.get("title", ""))[:80],
                    "tags": (data.get("tags", ""))[:60],
                }
            elif method_name == "record_negative_pattern":
                summary = {
                    "pattern_key": (
                        args[0] if args else kwargs.get("pattern_key", "")
                    )[:80]
                }

        elif store_name == "injection_store":
            if method_name == "store_injection":
                data = args[0] if args else kwargs.get("injection_data", {})
                session_id = data.get("trigger", {}).get("session_id", "")
                summary = {
                    "session_id": session_id[:16],
                    "sha256": data.get("payload_sha256", "")[:16],
                }

        elif store_name == "observability_store":
            if method_name == "record_claim_with_evidence":
                summary = {
                    "claim": (
                        args[0] if args else kwargs.get("claim_text", "")
                    )[:80],
                    "evidence_type": (
                        args[1]
                        if len(args) > 1
                        else kwargs.get("evidence_type", "")
                    ),
                }
            elif method_name == "update_claim_status":
                summary = {
                    "claim_id": (
                        args[0] if args else kwargs.get("claim_id", "")
                    )[:16]
                }
            elif method_name == "record_direct_claim_outcome":
                summary = {
                    "claim_id": (
                        args[0] if args else kwargs.get("claim_id", "")
                    )[:16]
                }
            elif method_name == "auto_upgrade_evidence_grade":
                summary = {
                    "claim_id": (
                        args[0] if args else kwargs.get("claim_id", "")
                    )[:16]
                }
            elif method_name == "record_webhook_delivery":
                summary = {
                    "session_id": (
                        args[0] if args else kwargs.get("session_id", "")
                    )[:16]
                }
            elif method_name == "record_injection_event":
                summary = {
                    "session_id": (
                        args[0] if args else kwargs.get("session_id", "")
                    )[:16]
                }
            elif method_name == "record_outcome_event":
                summary = {
                    "claim_id": (
                        args[0] if args else kwargs.get("claim_id", "")
                    )[:16]
                }
            elif method_name == "record_determinism_violation":
                summary = {"type": "determinism_violation"}

    except Exception:
        summary = {"_summarize_error": True}

    return summary


def _summarize_result(result):
    """Extract key fields from method result. Keep minimal."""
    if result is None:
        return {"status": "ok"}

    if isinstance(result, dict):
        out = {"status": "ok"}
        if "_duplicate" in result:
            out["dedup"] = "duplicate"
        elif "_consolidated" in result:
            out["dedup"] = "consolidated"
        if "id" in result:
            out["id"] = str(result["id"])[:16]
        return out

    if isinstance(result, (int, float)):
        return {"status": "ok", "value": result}

    if isinstance(result, str):
        return {"status": "ok", "id": result[:16]}

    if isinstance(result, list):
        return {"status": "ok", "count": len(result)}

    return {"status": "ok"}


# =========================================================================
# Singleton accessor
# =========================================================================


def get_evented_write_service():
    """Get the singleton EventedWriteService instance."""
    return EventedWriteService.get_instance()


# =========================================================================
# CLI for verification and stats
# =========================================================================

if __name__ == "__main__":
    import sys

    ews = get_evented_write_service()

    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        result = ews.verify_chain()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["valid"] else 1)

    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        result = ews.stats()
        print(json.dumps(result, indent=2, default=str))
        sys.exit(0)

    # Default: show stats
    result = ews.stats()
    print(json.dumps(result, indent=2, default=str))
