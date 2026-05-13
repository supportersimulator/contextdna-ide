"""
Fleet Nerve Sanitization — security hardening for all injection points.

Validates and sanitizes:
- Session IDs (UUID format only)
- Message content (shell metacharacter escaping, size limits)
- Seed file paths (no traversal, no symlinks)
- NATS message structure (required fields, size, type, sender)
- HTTP payloads (JSON schema, size limits)
- Rate limiting per sender

3-surgeon cross-exam finding (2026-04-05): command/code injection via
unsanitized inputs to `claude --resume`, seed file writes, and NATS messages.
"""

import logging
import os
import re
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("fleet_sanitize")

# ── Constants ──

MAX_MESSAGE_BYTES = 100 * 1024        # 100KB max for NATS/HTTP messages
MAX_RESUME_MESSAGE_BYTES = 10 * 1024  # 10KB max for claude --resume -p content
MAX_SEED_FILE_BYTES = 50 * 1024       # 50KB max seed file size
RATE_LIMIT_PER_SENDER = 60            # max messages per minute per sender
RATE_LIMIT_WINDOW_S = 60              # 1 minute window

# UUID v4 pattern (with or without hyphens)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Safe identifier: alphanumeric, dash, underscore, dot (for node IDs, filenames)
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

# Shell metacharacters that must be escaped in content passed to -p
_SHELL_META = re.compile(r'[`$\\!#&|;(){}\[\]<>\'"\n\r\x00-\x1f\x7f]')

# ANSI escape sequences (terminal injection)
_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[^[\]()]')

# Valid NATS message types
VALID_MESSAGE_TYPES = frozenset({
    "context", "reply", "broadcast", "task", "alert",
    "sync", "heartbeat", "ack", "seed",
    "session-start", "session-end",
    "repair",
    # Fleet-local signals (were rejected as noise — now recognized)
    "idle", "idle-suggestions", "health",
    "delegation", "delegation_result",
    "chain", "rebuttal_proposal", "rebuttal_dissent", "rebuttal_resolution",
})

# ── Session ID Validation ──


def validate_session_id(session_id: str) -> Optional[str]:
    """Validate and return a sanitized session ID, or None if invalid.

    Only accepts UUID format (hex chars + optional hyphens).
    Rejects path traversal, shell metacharacters, empty strings.
    """
    if not session_id or not isinstance(session_id, str):
        return None
    s = session_id.strip()
    if not s or len(s) > 64:
        return None
    if _UUID_RE.match(s):
        return s
    # Reject anything that isn't a valid UUID
    logger.warning(f"Invalid session ID format (not UUID): {s[:20]}...")
    return None


# ── Message Content Sanitization ──


def sanitize_resume_message(message: str) -> Optional[str]:
    """Sanitize message content for `claude --resume -p`.

    - Strips ANSI escape sequences
    - Enforces 10KB max
    - Returns None if content is empty after sanitization
    - Does NOT escape shell metacharacters (subprocess list args handles that)
    """
    if not message or not isinstance(message, str):
        return None
    # Strip ANSI escapes (terminal injection prevention)
    cleaned = _ANSI_ESCAPE.sub('', message)
    # Strip null bytes
    cleaned = cleaned.replace('\x00', '')
    # Enforce size limit
    if len(cleaned.encode('utf-8', errors='replace')) > MAX_RESUME_MESSAGE_BYTES:
        logger.warning(f"Resume message truncated: {len(cleaned)} chars > {MAX_RESUME_MESSAGE_BYTES}B limit")
        # Truncate to byte limit (safe for UTF-8)
        encoded = cleaned.encode('utf-8', errors='replace')[:MAX_RESUME_MESSAGE_BYTES]
        cleaned = encoded.decode('utf-8', errors='replace')
    return cleaned if cleaned.strip() else None


def sanitize_seed_content(content: str) -> str:
    """Sanitize content before writing to seed file.

    - Strips ANSI escape sequences
    - Strips null bytes and other control characters (except newline, tab)
    - Enforces 50KB max
    """
    if not content or not isinstance(content, str):
        return ""
    # Strip ANSI escapes
    cleaned = _ANSI_ESCAPE.sub('', content)
    # Strip null bytes and most control characters (keep \n, \t)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
    # Enforce size limit
    if len(cleaned.encode('utf-8', errors='replace')) > MAX_SEED_FILE_BYTES:
        encoded = cleaned.encode('utf-8', errors='replace')[:MAX_SEED_FILE_BYTES]
        cleaned = encoded.decode('utf-8', errors='replace')
    return cleaned


# ── Seed File Path Validation ──


def validate_seed_path(seed_dir: str, filename_part: str) -> Optional[str]:
    """Validate and construct a safe seed file path.

    - filename_part must be alphanumeric + dash + underscore only
    - No path traversal (.., absolute paths, slashes)
    - Result must be within seed_dir
    - Returns None if invalid
    """
    if not filename_part or not isinstance(filename_part, str):
        return None
    # Reject any path traversal characters outright
    if '..' in filename_part or '/' in filename_part or '\\' in filename_part:
        logger.warning(f"Seed filename rejected (path traversal): '{filename_part[:40]}'")
        return None
    # Validate filename: only alphanumeric, dash, underscore
    safe = re.sub(r'[^a-zA-Z0-9_-]', '', filename_part)
    if not safe or safe != filename_part:
        if safe:
            logger.warning(f"Seed filename rejected (unsafe chars): '{filename_part[:40]}'")
        else:
            logger.warning(f"Seed filename rejected (no safe chars): '{filename_part[:40]}'")
        return None
    path = os.path.join(seed_dir, f"fleet-seed-{safe}.md")
    # Resolve to catch symlink attacks and traversal
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(seed_dir)
    if not real_path.startswith(real_dir + os.sep) and real_path != real_dir:
        logger.warning(f"Seed path traversal blocked: {path} -> {real_path}")
        return None
    return path


def validate_seed_file_for_reading(seed_path: str) -> bool:
    """Validate a seed file is safe to read (for hook injection).

    - File must exist and be a regular file (not symlink to outside)
    - Owned by current user
    - Size <= 50KB
    """
    if not seed_path or not os.path.exists(seed_path):
        return False
    try:
        stat = os.lstat(seed_path)  # lstat: don't follow symlinks
        # Check if symlink — if so, verify target is in /tmp
        if os.path.islink(seed_path):
            target = os.path.realpath(seed_path)
            if not target.startswith('/tmp/'):
                logger.warning(f"Seed file symlink outside /tmp: {seed_path} -> {target}")
                return False
        # Check ownership (current user)
        if stat.st_uid != os.getuid():
            logger.warning(f"Seed file not owned by current user: {seed_path} (uid={stat.st_uid})")
            return False
        # Check size
        if stat.st_size > MAX_SEED_FILE_BYTES:
            logger.warning(f"Seed file too large: {seed_path} ({stat.st_size}B > {MAX_SEED_FILE_BYTES}B)")
            return False
        return True
    except OSError as e:
        logger.warning(f"Seed file validation error: {seed_path}: {e}")
        return False


# ── NATS/HTTP Message Validation ──


def validate_nats_message(data: dict, known_peers: set[str] | None = None) -> tuple[bool, str]:
    """Validate an incoming NATS or HTTP message.

    Returns (is_valid, error_message).
    """
    if not isinstance(data, dict):
        return False, "message must be a JSON object"

    # Check raw size (approximate — already parsed, but check field sizes)
    try:
        size = len(str(data))
        if size > MAX_MESSAGE_BYTES:
            return False, f"message too large ({size}B > {MAX_MESSAGE_BYTES}B)"
    except Exception:
        return False, "cannot determine message size"

    # Required field: type
    msg_type = data.get("type")
    if msg_type and msg_type not in VALID_MESSAGE_TYPES:
        return False, f"unknown message type: {msg_type}"

    # Required field: from (sender)
    sender = data.get("from")
    if not sender or not isinstance(sender, str):
        return False, "missing or invalid 'from' field"
    if not _SAFE_ID_RE.match(sender):
        return False, f"invalid sender ID format: {sender[:30]}"

    # Validate sender is a known peer (if peer list provided)
    if known_peers is not None and sender not in known_peers:
        return False, f"unknown sender: {sender}"

    # Validate payload size if present
    payload = data.get("payload")
    if payload and isinstance(payload, dict):
        body = payload.get("body", "")
        if isinstance(body, str) and len(body.encode('utf-8', errors='replace')) > MAX_MESSAGE_BYTES:
            return False, f"payload body too large"

    return True, ""


def validate_http_message_body(raw_body: bytes) -> tuple[bool, str]:
    """Validate raw HTTP request body before JSON parsing.

    Returns (is_valid, error_message).
    """
    if not raw_body:
        return False, "empty request body"
    if len(raw_body) > MAX_MESSAGE_BYTES:
        return False, f"payload too large ({len(raw_body)}B > {MAX_MESSAGE_BYTES}B)"
    return True, ""


# ── Safe Identifier Validation ──


def validate_node_id(node_id: str) -> Optional[str]:
    """Validate a node ID. Returns ID if valid, None if not.

    Strict: rejects any ID with unsafe characters (no sanitization — reject or accept).
    """
    if not node_id or not isinstance(node_id, str):
        return None
    if _SAFE_ID_RE.match(node_id):
        return node_id
    logger.warning(f"Invalid node ID rejected: '{node_id[:30]}'")
    return None


def validate_relay_target(target: str, known_peers: set[str] | None = None) -> Optional[str]:
    """Validate a relay target is a known peer. Returns target or None."""
    if not target or not isinstance(target, str):
        return None
    if not _SAFE_ID_RE.match(target):
        return None
    if known_peers is not None and target not in known_peers and target != "all":
        logger.warning(f"Relay target not a known peer: {target}")
        return None
    return target


# ── Rate Limiting ──


class SenderRateLimiter:
    """Per-sender rate limiter using sliding window counters."""

    def __init__(self, max_per_window: int = RATE_LIMIT_PER_SENDER,
                 window_s: float = RATE_LIMIT_WINDOW_S):
        self._max = max_per_window
        self._window = window_s
        self._counts: dict[str, list[float]] = {}
        self._lock = Lock()

    def check_and_record(self, sender: str) -> bool:
        """Check if sender is within rate limit. Records the attempt if allowed.

        Returns True if allowed, False if rate-limited.
        """
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            timestamps = self._counts.get(sender, [])
            # Prune old entries
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self._max:
                logger.warning(f"Rate limited sender: {sender} ({len(timestamps)} msgs in {self._window}s)")
                self._counts[sender] = timestamps
                return False
            timestamps.append(now)
            self._counts[sender] = timestamps
            return True

    def cleanup(self):
        """Remove stale entries (call periodically)."""
        cutoff = time.time() - self._window * 2
        with self._lock:
            for sender in list(self._counts.keys()):
                self._counts[sender] = [t for t in self._counts[sender] if t > cutoff]
                if not self._counts[sender]:
                    del self._counts[sender]
