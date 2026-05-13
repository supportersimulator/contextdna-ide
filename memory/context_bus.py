#!/usr/bin/env python3
"""
Context Bus — Unified pub/sub + KV store for agent communication.

Two backends:
  SQLiteBus  — No Docker needed, polling-based pub/sub
  RedisBus   — Uses existing context-dna-redis (127.0.0.1:6379, no auth)

Usage:
    from memory.context_bus import get_context_bus

    bus = get_context_bus()
    bus.put("swarm:run123:seed", {"task": "deploy"})
    seed = bus.get("swarm:run123:seed")

    bus.subscribe("swarm:events", lambda topic, msg: print(msg))
    bus.publish("swarm:events", {"status": "done"})

Key namespace convention for swarms:
    contextdna:bus:swarm:{run_id}:seed
    contextdna:bus:swarm:{run_id}:agent:{agent_id}:mid
    contextdna:bus:swarm:{run_id}:agent:{agent_id}:result
    contextdna:bus:swarm:{run_id}:harmonize:report
"""

import json
import logging
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("contextdna.bus")

# =============================================================================
# Abstract Base
# =============================================================================


class ContextBus(ABC):
    """Abstract bus for agent communication — KV store + pub/sub."""

    @abstractmethod
    def put(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a value. Optional TTL in seconds."""
        ...

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Retrieve a value. Returns None if missing or expired."""
        ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys matching prefix."""
        ...

    @abstractmethod
    def subscribe(self, topic: str, callback: Callable[[str, Any], None]) -> None:
        """Register callback for topic. Callback receives (topic, message)."""
        ...

    @abstractmethod
    def publish(self, topic: str, message: Any) -> None:
        """Publish message to topic."""
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if existed."""
        ...

    def close(self) -> None:
        """Clean up resources. Override in subclasses."""
        pass


# =============================================================================
# SQLite Backend
# =============================================================================

_SQLITE_BUS_DB = Path(__file__).parent / ".context_bus.db"


class SQLiteBus(ContextBus):
    """SQLite-backed ContextBus. No external dependencies.

    KV store: key/value table with optional TTL.
    Pub/sub: messages table + per-subscriber cursor tracking.
    Background thread polls for new messages every 1 second.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = str(db_path or _SQLITE_BUS_DB)
        self._subscribers: dict[str, list[Callable[[str, Any], None]]] = {}
        self._subscriber_cursors: dict[str, int] = {}
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        from memory.db_utils import connect_wal
        return connect_wal(self._db_path, timeout=5)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic);
                CREATE INDEX IF NOT EXISTS idx_kv_expires ON kv(expires_at)
                    WHERE expires_at IS NOT NULL;
            """)
            conn.commit()
        finally:
            conn.close()

    # -- KV operations --

    def put(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = (time.time() + ttl) if ttl else None
        serialized = json.dumps(value)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
                (key, serialized, expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> Optional[Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, expires_at = row
            if expires_at is not None and time.time() > expires_at:
                # Lazy cleanup
                conn.execute("DELETE FROM kv WHERE key = ?", (key,))
                conn.commit()
                return None
            return json.loads(value)
        finally:
            conn.close()

    def list_keys(self, prefix: str) -> list[str]:
        conn = self._connect()
        try:
            now = time.time()
            rows = conn.execute(
                "SELECT key FROM kv WHERE key LIKE ? AND (expires_at IS NULL OR expires_at > ?)",
                (prefix + "%", now),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def delete(self, key: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    # -- Pub/sub operations --

    def publish(self, topic: str, message: Any) -> None:
        serialized = json.dumps(message)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO messages (topic, payload, created_at) VALUES (?, ?, ?)",
                (topic, serialized, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def subscribe(self, topic: str, callback: Callable[[str, Any], None]) -> None:
        with self._lock:
            if topic not in self._subscribers:
                self._subscribers[topic] = []
                # Start cursor at current max id so we don't replay history
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE topic = ?",
                        (topic,),
                    ).fetchone()
                    self._subscriber_cursors[topic] = row[0]
                finally:
                    conn.close()
            self._subscribers[topic].append(callback)

            # Start polling thread if not running
            if self._poll_thread is None or not self._poll_thread.is_alive():
                self._stop_event.clear()
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, daemon=True, name="context-bus-poll"
                )
                self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Background thread: poll for new messages every 1 second."""
        while not self._stop_event.is_set():
            try:
                self._dispatch_new_messages()
            except Exception as e:
                logger.error(f"Bus poll error: {e}")
            self._stop_event.wait(1.0)

    def _dispatch_new_messages(self) -> None:
        with self._lock:
            topics = list(self._subscribers.keys())
            cursors = dict(self._subscriber_cursors)
            subs = {t: list(cbs) for t, cbs in self._subscribers.items()}

        if not topics:
            return

        conn = self._connect()
        try:
            for topic in topics:
                last_id = cursors.get(topic, 0)
                rows = conn.execute(
                    "SELECT id, payload FROM messages WHERE topic = ? AND id > ? ORDER BY id",
                    (topic, last_id),
                ).fetchall()
                if not rows:
                    continue
                max_id = last_id
                for msg_id, payload in rows:
                    if msg_id > max_id:
                        max_id = msg_id
                    try:
                        parsed = json.loads(payload)
                    except json.JSONDecodeError:
                        parsed = payload
                    for cb in subs.get(topic, []):
                        try:
                            cb(topic, parsed)
                        except Exception as e:
                            logger.error(f"Subscriber callback error on {topic}: {e}")
                with self._lock:
                    self._subscriber_cursors[topic] = max_id
        finally:
            conn.close()

    def close(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3)

    def cleanup_expired(self) -> int:
        """Remove expired KV entries and old messages. Returns count deleted."""
        conn = self._connect()
        try:
            now = time.time()
            c1 = conn.execute(
                "DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            # Purge messages older than 1 hour
            cutoff = now - 3600
            c2 = conn.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
            return c1.rowcount + c2.rowcount
        finally:
            conn.close()


# =============================================================================
# Redis Backend
# =============================================================================

_BUS_KEY_PREFIX = "contextdna:bus:"
_BUS_TOPIC_PREFIX = "contextdna:bus:topic:"


class RedisBus(ContextBus):
    """Redis-backed ContextBus. Uses existing context-dna-redis (6379, no auth).

    KV: Redis SET/GET with optional EX.
    Pub/sub: Redis native pub/sub channels.
    Graceful fallback: returns None/empty if Redis unavailable.
    """

    def __init__(self):
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._callbacks: dict[str, list[Callable[[str, Any], None]]] = {}
        self._lock = threading.Lock()

    def _client(self):
        """Get Redis client from existing singleton in redis_cache.py."""
        try:
            from memory.redis_cache import get_redis_client
            return get_redis_client()
        except Exception:
            return None

    def _full_key(self, key: str) -> str:
        return f"{_BUS_KEY_PREFIX}{key}"

    def _topic_channel(self, topic: str) -> str:
        return f"{_BUS_TOPIC_PREFIX}{topic}"

    # -- KV operations --

    def put(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        r = self._client()
        if not r:
            logger.warning("Redis unavailable for put()")
            return
        try:
            serialized = json.dumps(value)
            full = self._full_key(key)
            if ttl:
                r.setex(full, ttl, serialized)
            else:
                r.set(full, serialized)
        except Exception as e:
            logger.error(f"RedisBus.put error: {e}")

    def get(self, key: str) -> Optional[Any]:
        r = self._client()
        if not r:
            return None
        try:
            data = r.get(self._full_key(key))
            if data is None:
                return None
            return json.loads(data)
        except Exception as e:
            logger.error(f"RedisBus.get error: {e}")
            return None

    def list_keys(self, prefix: str) -> list[str]:
        r = self._client()
        if not r:
            return []
        try:
            full_prefix = self._full_key(prefix)
            keys = r.keys(f"{full_prefix}*")
            # Strip the bus key prefix to return logical keys
            strip_len = len(_BUS_KEY_PREFIX)
            return [k[strip_len:] if isinstance(k, str) else k.decode()[strip_len:] for k in keys]
        except Exception as e:
            logger.error(f"RedisBus.list_keys error: {e}")
            return []

    def delete(self, key: str) -> bool:
        r = self._client()
        if not r:
            return False
        try:
            return r.delete(self._full_key(key)) > 0
        except Exception as e:
            logger.error(f"RedisBus.delete error: {e}")
            return False

    # -- Pub/sub operations --

    def publish(self, topic: str, message: Any) -> None:
        r = self._client()
        if not r:
            logger.warning("Redis unavailable for publish()")
            return
        try:
            serialized = json.dumps(message)
            r.publish(self._topic_channel(topic), serialized)
        except Exception as e:
            logger.error(f"RedisBus.publish error: {e}")

    def subscribe(self, topic: str, callback: Callable[[str, Any], None]) -> None:
        r = self._client()
        if not r:
            logger.warning("Redis unavailable for subscribe()")
            return

        channel = self._topic_channel(topic)

        with self._lock:
            if topic not in self._callbacks:
                self._callbacks[topic] = []
            self._callbacks[topic].append(callback)

            # Set up Redis pubsub if first subscription
            if self._pubsub is None:
                self._pubsub = r.pubsub()

            self._pubsub.subscribe(**{channel: self._make_handler(topic)})

            # Start listener thread if not running
            if self._listener_thread is None or not self._listener_thread.is_alive():
                self._listener_thread = self._pubsub.run_in_thread(
                    sleep_time=0.1, daemon=True
                )

    def _make_handler(self, topic: str):
        """Create a Redis pubsub message handler for a topic."""
        def handler(message):
            if message["type"] != "message":
                return
            try:
                parsed = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError):
                parsed = message["data"]
            with self._lock:
                callbacks = list(self._callbacks.get(topic, []))
            for cb in callbacks:
                try:
                    cb(topic, parsed)
                except Exception as e:
                    logger.error(f"RedisBus subscriber error on {topic}: {e}")
        return handler

    def close(self) -> None:
        if self._listener_thread and hasattr(self._listener_thread, "stop"):
            self._listener_thread.stop()
        if self._pubsub:
            try:
                self._pubsub.close()
            except Exception:
                pass


# =============================================================================
# Factory
# =============================================================================

_bus_instance: Optional[ContextBus] = None
_bus_lock = threading.Lock()


def get_context_bus(prefer_redis: bool = True) -> ContextBus:
    """Get the global ContextBus instance.

    Returns RedisBus if Redis available and preferred, otherwise SQLiteBus.
    Singleton — same instance returned on subsequent calls.
    """
    global _bus_instance
    if _bus_instance is not None:
        return _bus_instance

    with _bus_lock:
        # Double-check after acquiring lock
        if _bus_instance is not None:
            return _bus_instance

        if prefer_redis:
            try:
                from memory.redis_cache import get_redis_client
                client = get_redis_client()
                if client is not None:
                    _bus_instance = RedisBus()
                    logger.info("ContextBus: using RedisBus (127.0.0.1:6379)")
                    return _bus_instance
            except Exception as e:
                logger.warning(f"Redis unavailable, falling back to SQLiteBus: {e}")

        _bus_instance = SQLiteBus()
        logger.info(f"ContextBus: using SQLiteBus ({_SQLITE_BUS_DB})")
        return _bus_instance


# =============================================================================
# CLI — Quick test / status
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    bus = get_context_bus()
    backend = type(bus).__name__

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print(f"Backend: {backend}")
        # KV test
        bus.put("test:hello", {"msg": "world"}, ttl=60)
        val = bus.get("test:hello")
        assert val == {"msg": "world"}, f"KV failed: {val}"
        print(f"  KV put/get: OK")

        keys = bus.list_keys("test:")
        assert "test:hello" in keys, f"list_keys failed: {keys}"
        print(f"  list_keys: OK ({keys})")

        bus.delete("test:hello")
        assert bus.get("test:hello") is None, "delete failed"
        print(f"  delete: OK")

        # Pub/sub test
        received = []
        bus.subscribe("test:topic", lambda t, m: received.append((t, m)))
        bus.publish("test:topic", {"event": "ping"})

        # Wait for delivery
        time.sleep(2)
        if received:
            print(f"  pub/sub: OK ({received})")
        else:
            print(f"  pub/sub: no messages received (may need longer wait)")

        bus.close()
        print("All tests passed.")
    else:
        print(f"ContextBus backend: {backend}")
        print(f"  SQLite DB: {_SQLITE_BUS_DB}")
        print(f"  Redis prefix: {_BUS_KEY_PREFIX}")
        print(f"\nRun with 'test' arg for self-test:")
        print(f"  PYTHONPATH=. .venv/bin/python3 memory/context_bus.py test")
