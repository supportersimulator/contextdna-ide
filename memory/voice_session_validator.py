"""
Voice Session Token Validator for Local Context DNA Server.

Validates Ed25519-signed session tokens from EC2 WITHOUT network calls.
Uses the same public key as the EC2 subscription/voice session signer.

Security Model:
- EC2 signs tokens with private key (derived from SECRET_KEY)
- Local validates with public key (hardcoded or fetched once)
- Zero network latency for validation
- Same trust model as subscription verification

Usage:
    from voice_session_validator import validate_voice_session

    token_str = "eyJ..."  # From WebSocket query params
    is_valid, user_email, error = validate_voice_session(token_str)

    if is_valid:
        # Allow WebSocket connection for user_email
        pass
    else:
        # Reject connection
        ws.close(1008, error)
"""
import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple

# Type checking imports (not executed at runtime)
if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as ed25519_types

# Load .env.local if it exists
ENV_LOCAL_PATH = Path(__file__).parent / ".env.local"
if ENV_LOCAL_PATH.exists():
    with open(ENV_LOCAL_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# Try to import cryptography for Ed25519 verification
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("[VoiceSessionValidator] WARNING: cryptography not installed - token validation disabled")

# Public key from EC2 server (same key used for subscription signing)
# This is safe to hardcode - it's the PUBLIC key
# To get this value: python -c "from payments.subscription_signer import get_server_public_key_b64; print(get_server_public_key_b64())"
EC2_PUBLIC_KEY_B64 = os.environ.get(
    "CONTEXTDNA_EC2_PUBLIC_KEY",
    # Default: Empty - will be populated from EC2 or via env var
    ""
)


def set_public_key(public_key_b64: str):
    """Set the EC2 public key for validation."""
    global EC2_PUBLIC_KEY_B64
    EC2_PUBLIC_KEY_B64 = public_key_b64


def _get_public_key() -> Optional[Any]:  # ed25519.Ed25519PublicKey when crypto available
    """Get the Ed25519 public key for verification."""
    if not CRYPTO_AVAILABLE:
        return None
    if not EC2_PUBLIC_KEY_B64:
        return None
    try:
        public_bytes = base64.b64decode(EC2_PUBLIC_KEY_B64)
        return ed25519.Ed25519PublicKey.from_public_bytes(public_bytes)
    except Exception as e:
        print(f"[VoiceSessionValidator] Failed to load public key: {e}")
        return None


def decode_token(encoded: str) -> Optional[dict]:
    """Decode a base64url-encoded token string to dict."""
    try:
        # Handle URL-safe base64
        json_bytes = base64.urlsafe_b64decode(encoded.encode("utf-8"))
        return json.loads(json_bytes.decode("utf-8"))
    except Exception:
        return None


@dataclass
class ValidatedSession:
    """Result of session token validation."""
    is_valid: bool
    user_id: Optional[str] = None      # Primary identifier (Supabase UUID)
    user_email: Optional[str] = None   # For display/debugging only
    device_token: Optional[str] = None
    session_id: Optional[str] = None
    error: str = ""
    mode: str = "unknown"

    @property
    def identity(self) -> str:
        """Return formatted identity string [user_id:device_token].

        Uses user_id (UUID) as primary identifier for:
        - Stability: UUID never changes, email can
        - Privacy: No PII in task logs
        - Referential integrity: History stays linked
        """
        if not self.user_id:
            return "anonymous"
        # Use first 8 chars of UUID and device_token for readable identity
        user_prefix = self.user_id[:8] if len(self.user_id) > 8 else self.user_id
        device = self.device_token[:8] if self.device_token else "no_device"
        return f"[{user_prefix}:{device}]"

    @property
    def display_identity(self) -> str:
        """Return human-readable identity with email (for UI/logs)."""
        if not self.user_email:
            return self.identity
        return f"{self.user_email} ({self.identity})"


def validate_voice_session(token_str: str) -> Tuple[bool, Optional[str], str]:
    """
    Validate a voice session token.

    Args:
        token_str: Base64url-encoded token from WebSocket query params

    Returns:
        Tuple of (is_valid, user_email, error_message)
        - If valid: (True, "user@email.com", "")
        - If invalid: (False, None, "Error description")
    """
    if not CRYPTO_AVAILABLE:
        return False, None, "Cryptography library not available"

    if not EC2_PUBLIC_KEY_B64:
        # If no public key configured, allow connection (dev mode)
        # In production, this should be set
        print("[VoiceSessionValidator] WARNING: No public key configured - allowing connection (dev mode)")
        return True, "dev@contextdna.io", "dev_mode"

    # Decode token
    token = decode_token(token_str)
    if not token:
        return False, None, "Invalid token format"

    # Check required fields
    required = ["session_id", "user_email", "verified_at", "valid_until", "token_type", "signature_b64"]
    for field in required:
        if field not in token:
            return False, None, f"Missing field: {field}"

    # Check token type
    if token.get("token_type") != "voice_session":
        return False, None, "Invalid token type"

    # Check expiration
    try:
        valid_until_str = token.get("valid_until", "")
        valid_until = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > valid_until:
            return False, None, "Token expired"
    except ValueError:
        return False, None, "Invalid expiration format"

    # Verify signature
    try:
        signature_b64 = token.pop("signature_b64")
        signature = base64.b64decode(signature_b64.encode("utf-8"))

        # Rebuild canonical payload (must match EC2 creation exactly)
        canonical = json.dumps(token, separators=(",", ":"), sort_keys=True).encode("utf-8")

        # Verify with public key
        public_key = _get_public_key()
        if not public_key:
            return False, None, "Public key not configured"

        public_key.verify(signature, canonical)

        # Restore signature
        token["signature_b64"] = signature_b64

        return True, token.get("user_email"), ""

    except Exception as e:
        return False, None, f"Signature verification failed: {e}"


def validate_session_full(token_str: Optional[str]) -> ValidatedSession:
    """
    Validate token and return full session details including device_token.

    This is the preferred function for getting complete authenticated identity.

    Args:
        token_str: Token string (may be None for dev connections)

    Returns:
        ValidatedSession with user_email, device_token, session_id, etc.
    """
    if not token_str:
        if not EC2_PUBLIC_KEY_B64:
            # Dev mode - no token required
            return ValidatedSession(
                is_valid=True,
                user_id="dev-user-0000-0000-000000000000",
                user_email="dev@contextdna.io",
                device_token="dev_device",
                session_id="dev_session",
                mode="dev_mode"
            )
        else:
            # Production - token required
            return ValidatedSession(
                is_valid=False,
                error="token required",
                mode="error"
            )

    if not CRYPTO_AVAILABLE:
        return ValidatedSession(
            is_valid=False,
            error="Cryptography library not available",
            mode="error"
        )

    if not EC2_PUBLIC_KEY_B64:
        # If no public key configured, allow connection (dev mode)
        return ValidatedSession(
            is_valid=True,
            user_id="dev-user-0000-0000-000000000000",
            user_email="dev@contextdna.io",
            device_token="dev_device",
            session_id="dev_session",
            mode="dev_mode"
        )

    # Decode token
    token = decode_token(token_str)
    if not token:
        return ValidatedSession(
            is_valid=False,
            error="Invalid token format",
            mode="error"
        )

    # Check required fields (user_id preferred, user_email for backwards compat)
    required_base = ["session_id", "verified_at", "valid_until", "token_type", "signature_b64"]
    for field in required_base:
        if field not in token:
            return ValidatedSession(
                is_valid=False,
                error=f"Missing field: {field}",
                mode="error"
            )

    # Need either user_id (new) or user_email (backwards compat)
    if "user_id" not in token and "user_email" not in token:
        return ValidatedSession(
            is_valid=False,
            error="Missing user identifier (user_id or user_email)",
            mode="error"
        )

    # Check token type
    if token.get("token_type") != "voice_session":
        return ValidatedSession(
            is_valid=False,
            error="Invalid token type",
            mode="error"
        )

    # Check expiration
    try:
        valid_until_str = token.get("valid_until", "")
        valid_until = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > valid_until:
            return ValidatedSession(
                is_valid=False,
                error="Token expired",
                mode="error"
            )
    except ValueError:
        return ValidatedSession(
            is_valid=False,
            error="Invalid expiration format",
            mode="error"
        )

    # Verify signature
    try:
        signature_b64 = token.pop("signature_b64")
        signature = base64.b64decode(signature_b64.encode("utf-8"))

        # Rebuild canonical payload (must match EC2 creation exactly)
        canonical = json.dumps(token, separators=(",", ":"), sort_keys=True).encode("utf-8")

        # Verify with public key
        public_key = _get_public_key()
        if not public_key:
            return ValidatedSession(
                is_valid=False,
                error="Public key not configured",
                mode="error"
            )

        public_key.verify(signature, canonical)

        # Restore signature
        token["signature_b64"] = signature_b64

        return ValidatedSession(
            is_valid=True,
            user_id=token.get("user_id"),  # Primary identifier (UUID)
            user_email=token.get("user_email"),  # For display/backwards compat
            device_token=token.get("device_token"),
            session_id=token.get("session_id"),
            mode="verified"
        )

    except Exception as e:
        return ValidatedSession(
            is_valid=False,
            error=f"Signature verification failed: {e}",
            mode="error"
        )


def validate_or_allow_dev(token_str: Optional[str]) -> Tuple[bool, str, str]:
    """
    Validate token, or allow dev connections if no token provided.

    In development (no public key configured), allows connections without token.
    In production, requires valid token.

    Args:
        token_str: Token string (may be None for dev connections)

    Returns:
        Tuple of (is_valid, user_email, mode)
        mode is one of: "verified", "dev_mode", "error"

    Note: For full session details including device_token, use validate_session_full()
    """
    session = validate_session_full(token_str)
    if session.is_valid:
        return True, session.user_email or "", session.mode
    else:
        return False, "", f"error: {session.error}"


# Export for easy import
__all__ = [
    "validate_voice_session",
    "validate_session_full",
    "validate_or_allow_dev",
    "set_public_key",
    "decode_token",
    "ValidatedSession",
]
