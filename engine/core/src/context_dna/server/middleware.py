"""Context DNA API Middleware.

Provides reusable middleware components for the API server including:
- Request validation
- Rate limiting
- Error handling
- Logging
- Request timing

Usage:
    from context_dna.server.middleware import (
        validate_request,
        RateLimiter,
        error_handler,
    )
"""

import json
import time
import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from context_dna.exceptions import (
    ContextDNAError,
    ValidationError,
    RequiredFieldError,
    InvalidTypeError,
    RateLimitExceededError,
    BadRequestError,
    ServerError,
)


# =============================================================================
# Logging Configuration
# =============================================================================

logger = logging.getLogger("context_dna.api")


# =============================================================================
# Request Validation
# =============================================================================

class RequestValidator:
    """Validates API request bodies against schema definitions.

    Example:
        validator = RequestValidator({
            "title": {"type": str, "required": True, "min_length": 1},
            "content": {"type": str, "required": False, "default": ""},
            "tags": {"type": list, "required": False, "default": []},
        })

        try:
            validated = validator.validate(request_body)
        except ValidationError as e:
            return error_response(e)
    """

    def __init__(self, schema: Dict[str, Dict[str, Any]]):
        """Initialize with field schema.

        Args:
            schema: Dictionary mapping field names to validation rules.
                Rules can include:
                - type: Expected Python type (str, int, list, dict, bool)
                - required: Whether field is required (default: False)
                - default: Default value if not provided
                - min_length: Minimum length for strings/lists
                - max_length: Maximum length for strings/lists
                - min_value: Minimum value for numbers
                - max_value: Maximum value for numbers
                - choices: List of valid values
                - pattern: Regex pattern for strings
        """
        self.schema = schema

    def validate(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate request data against schema.

        Args:
            data: Request body dictionary

        Returns:
            Validated and cleaned data with defaults applied

        Raises:
            ValidationError: If validation fails
        """
        if not isinstance(data, dict):
            raise BadRequestError("Request body must be a JSON object")

        result = {}

        for field, rules in self.schema.items():
            value = data.get(field)

            # Check required fields
            if rules.get("required", False):
                if value is None:
                    raise RequiredFieldError(field)
                if isinstance(value, str) and not value.strip():
                    raise RequiredFieldError(field)

            # Apply default if not provided
            if value is None:
                if "default" in rules:
                    result[field] = rules["default"]
                continue

            # Type checking
            expected_type = rules.get("type")
            if expected_type and not isinstance(value, expected_type):
                raise InvalidTypeError(
                    field,
                    expected_type.__name__,
                    type(value).__name__,
                )

            # String validations
            if isinstance(value, str):
                value = value.strip()  # Auto-trim strings

                min_len = rules.get("min_length")
                if min_len and len(value) < min_len:
                    raise ValidationError(
                        f"Field '{field}' must be at least {min_len} characters",
                        field=field,
                    )

                max_len = rules.get("max_length")
                if max_len and len(value) > max_len:
                    raise ValidationError(
                        f"Field '{field}' must be at most {max_len} characters",
                        field=field,
                    )

            # List validations
            if isinstance(value, list):
                min_len = rules.get("min_length")
                if min_len and len(value) < min_len:
                    raise ValidationError(
                        f"Field '{field}' must have at least {min_len} items",
                        field=field,
                    )

                max_len = rules.get("max_length")
                if max_len and len(value) > max_len:
                    raise ValidationError(
                        f"Field '{field}' must have at most {max_len} items",
                        field=field,
                    )

            # Number validations
            if isinstance(value, (int, float)):
                min_val = rules.get("min_value")
                if min_val is not None and value < min_val:
                    raise ValidationError(
                        f"Field '{field}' must be at least {min_val}",
                        field=field,
                    )

                max_val = rules.get("max_value")
                if max_val is not None and value > max_val:
                    raise ValidationError(
                        f"Field '{field}' must be at most {max_val}",
                        field=field,
                    )

            # Choices validation
            choices = rules.get("choices")
            if choices and value not in choices:
                raise ValidationError(
                    f"Field '{field}' must be one of: {choices}",
                    field=field,
                    value=value,
                )

            result[field] = value

        return result


# Pre-defined validators for common endpoints
WIN_VALIDATOR = RequestValidator({
    "title": {"type": str, "required": True, "min_length": 1, "max_length": 500},
    "content": {"type": str, "required": False, "default": "", "max_length": 10000},
    "tags": {"type": list, "required": False, "default": [], "max_length": 20},
})

FIX_VALIDATOR = RequestValidator({
    "title": {"type": str, "required": True, "min_length": 1, "max_length": 500},
    "content": {"type": str, "required": False, "default": "", "max_length": 10000},
    "tags": {"type": list, "required": False, "default": [], "max_length": 20},
})

QUERY_VALIDATOR = RequestValidator({
    "query": {"type": str, "required": True, "min_length": 1, "max_length": 1000},
    "limit": {"type": int, "required": False, "default": 10, "min_value": 1, "max_value": 100},
})

CONSULT_VALIDATOR = RequestValidator({
    "task": {"type": str, "required": True, "min_length": 1, "max_length": 2000},
})

LEARNING_VALIDATOR = RequestValidator({
    "type": {
        "type": str,
        "required": False,
        "default": "note",
        "choices": ["win", "fix", "pattern", "sop", "note", "gotcha"],
    },
    "title": {"type": str, "required": True, "min_length": 1, "max_length": 500},
    "content": {"type": str, "required": False, "default": "", "max_length": 10000},
    "tags": {"type": list, "required": False, "default": [], "max_length": 20},
})


# =============================================================================
# Rate Limiting
# =============================================================================

class RateLimiter:
    """Token bucket rate limiter with per-client tracking.

    Uses a sliding window approach to track request rates per client,
    identified by IP address.

    Example:
        limiter = RateLimiter(requests_per_minute=60)

        def handle_request(client_ip):
            if not limiter.is_allowed(client_ip):
                raise RateLimitExceededError(limiter.retry_after(client_ip))
            # Process request...
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        burst_size: int = 10,
        cleanup_interval: int = 300,
    ):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Maximum sustained request rate
            burst_size: Maximum burst above sustained rate
            cleanup_interval: Seconds between client cleanup
        """
        self.rate = requests_per_minute / 60.0  # Tokens per second
        self.burst_size = burst_size
        self.cleanup_interval = cleanup_interval

        self._buckets: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"tokens": burst_size, "last_update": time.time()}
        )
        self._lock = Lock()
        self._last_cleanup = time.time()

    def is_allowed(self, client_id: str) -> bool:
        """Check if a request from client is allowed.

        Args:
            client_id: Client identifier (usually IP address)

        Returns:
            True if request is allowed, False if rate limited
        """
        with self._lock:
            self._maybe_cleanup()

            bucket = self._buckets[client_id]
            now = time.time()

            # Refill tokens based on time elapsed
            elapsed = now - bucket["last_update"]
            bucket["tokens"] = min(
                self.burst_size,
                bucket["tokens"] + elapsed * self.rate
            )
            bucket["last_update"] = now

            # Check if token available
            if bucket["tokens"] >= 1:
                bucket["tokens"] -= 1
                return True

            return False

    def retry_after(self, client_id: str) -> int:
        """Get seconds until client can retry.

        Args:
            client_id: Client identifier

        Returns:
            Seconds to wait before retrying
        """
        with self._lock:
            bucket = self._buckets.get(client_id)
            if not bucket:
                return 0

            # Calculate time to get 1 token
            tokens_needed = 1 - bucket["tokens"]
            if tokens_needed <= 0:
                return 0

            return int(tokens_needed / self.rate) + 1

    def reset(self, client_id: str) -> None:
        """Reset rate limit for a client.

        Args:
            client_id: Client identifier to reset
        """
        with self._lock:
            if client_id in self._buckets:
                del self._buckets[client_id]

    def _maybe_cleanup(self) -> None:
        """Clean up stale client entries."""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return

        # Remove clients that haven't made requests recently
        stale_threshold = now - self.cleanup_interval
        stale_clients = [
            client_id
            for client_id, bucket in self._buckets.items()
            if bucket["last_update"] < stale_threshold
        ]

        for client_id in stale_clients:
            del self._buckets[client_id]

        self._last_cleanup = now


# Global rate limiter instance
_global_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter."""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = RateLimiter()
    return _global_limiter


# =============================================================================
# Error Handling
# =============================================================================

def format_error_response(
    error: Exception,
    include_details: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """Format an exception as JSON error response.

    Args:
        error: Exception to format
        include_details: Whether to include error details

    Returns:
        Tuple of (response_dict, http_status_code)
    """
    if isinstance(error, ContextDNAError):
        response = {
            "error": error.code,
            "message": error.message,
        }
        if include_details and error.details:
            response["details"] = error.details

        # Get HTTP status from API errors
        http_status = getattr(error, "http_status", 500)

        # Map common error types to HTTP statuses
        if isinstance(error, ValidationError):
            http_status = 400
        elif isinstance(error, RateLimitExceededError):
            http_status = 429

        return response, http_status

    # Generic exception
    return {
        "error": "SERVER_ERROR",
        "message": str(error) if include_details else "Internal server error",
    }, 500


def error_handler(func: Callable) -> Callable:
    """Decorator for handling errors in request handlers.

    Catches exceptions and returns appropriate error responses.
    Also logs errors for debugging.

    Usage:
        @error_handler
        def handle_win(self, body):
            # ...handler code...
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except json.JSONDecodeError:
            error = BadRequestError("Invalid JSON in request body")
            response, status = format_error_response(error)
            self._send_json(response, status)
        except ContextDNAError as e:
            logger.warning(f"API error: {e.code} - {e.message}")
            response, status = format_error_response(e)
            self._send_json(response, status)
        except Exception as e:
            logger.exception(f"Unexpected error in {func.__name__}")
            error = ServerError(str(e))
            response, status = format_error_response(error, include_details=False)
            self._send_json(response, status)

    return wrapper


# =============================================================================
# Request Timing
# =============================================================================

class RequestTimer:
    """Context manager for timing request processing.

    Usage:
        with RequestTimer() as timer:
            # Process request
            pass
        print(f"Request took {timer.elapsed_ms}ms")
    """

    def __init__(self):
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def __enter__(self) -> "RequestTimer":
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *args) -> None:
        self.end_time = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        if self.start_time is None:
            return 0.0
        end = self.end_time or time.perf_counter()
        return (end - self.start_time) * 1000


# =============================================================================
# Request Logging
# =============================================================================

class RequestLogger:
    """Structured logging for API requests.

    Logs request/response pairs with timing information.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("context_dna.api")

    def log_request(
        self,
        method: str,
        path: str,
        client_ip: str,
        body_size: int = 0,
    ) -> None:
        """Log incoming request."""
        self.logger.info(
            f"→ {method} {path} from {client_ip}"
            + (f" ({body_size} bytes)" if body_size else "")
        )

    def log_response(
        self,
        method: str,
        path: str,
        status: int,
        elapsed_ms: float,
    ) -> None:
        """Log outgoing response."""
        level = logging.INFO if status < 400 else logging.WARNING
        self.logger.log(
            level,
            f"← {method} {path} → {status} ({elapsed_ms:.1f}ms)"
        )

    def log_error(
        self,
        method: str,
        path: str,
        error: Exception,
    ) -> None:
        """Log error during request handling."""
        self.logger.error(
            f"✗ {method} {path} → {type(error).__name__}: {error}"
        )


# =============================================================================
# Content Sanitization Middleware
# =============================================================================

def sanitize_learning_content(body: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize learning content before storage.

    Automatically sanitizes secrets from title and content fields.

    Args:
        body: Request body with title/content fields

    Returns:
        Body with sanitized content
    """
    try:
        from context_dna.security.sanitizer import sanitize_secrets

        if "title" in body:
            body["title"] = sanitize_secrets(body["title"])
        if "content" in body:
            body["content"] = sanitize_secrets(body["content"])

        return body
    except ImportError:
        # Sanitizer not available, return as-is
        return body


# =============================================================================
# Utility Functions
# =============================================================================

def get_client_ip(headers: Dict[str, str], address: Tuple[str, int]) -> str:
    """Extract client IP from request.

    Checks X-Forwarded-For for proxied requests.

    Args:
        headers: Request headers
        address: Socket address tuple

    Returns:
        Client IP address
    """
    # Check for proxy headers
    forwarded = headers.get("X-Forwarded-For", "")
    if forwarded:
        # Take first IP in chain (original client)
        return forwarded.split(",")[0].strip()

    real_ip = headers.get("X-Real-IP", "")
    if real_ip:
        return real_ip.strip()

    # Fall back to socket address
    return address[0]


def generate_request_id() -> str:
    """Generate a unique request ID for tracing."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    hash_input = f"{timestamp}-{time.perf_counter()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:12]
