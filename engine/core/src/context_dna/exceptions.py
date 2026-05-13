"""Context DNA Exception Classes.

Structured exception hierarchy for clean error handling throughout
the Context DNA system. All exceptions inherit from ContextDNAError
for easy catching and handling.

Usage:
    from context_dna.exceptions import (
        ValidationError,
        StorageError,
        LLMError,
    )

    try:
        brain.win("", "content")  # Empty title
    except ValidationError as e:
        print(f"Invalid input: {e}")
    except ContextDNAError as e:
        print(f"Context DNA error: {e}")
"""

from typing import Any, Dict, Optional


class ContextDNAError(Exception):
    """Base exception for all Context DNA errors.

    All Context DNA exceptions inherit from this class, making it easy
    to catch any Context DNA-related error with a single except clause.

    Attributes:
        message: Human-readable error message
        code: Machine-readable error code (e.g., "VALIDATION_ERROR")
        details: Optional dictionary with additional context
    """

    code: str = "CONTEXT_DNA_ERROR"

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for JSON serialization."""
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


# =============================================================================
# Validation Errors
# =============================================================================

class ValidationError(ContextDNAError):
    """Raised when input validation fails.

    Examples:
        - Empty required fields (title, content)
        - Invalid learning type
        - Malformed data format
    """

    code = "VALIDATION_ERROR"

    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        value: Optional[Any] = None,
    ):
        details = {}
        if field:
            details["field"] = field
        if value is not None:
            details["value"] = str(value)[:100]  # Truncate for safety
        super().__init__(message, details=details)
        self.field = field
        self.value = value


class RequiredFieldError(ValidationError):
    """Raised when a required field is missing or empty."""

    code = "REQUIRED_FIELD_ERROR"

    def __init__(self, field: str):
        super().__init__(
            f"Required field '{field}' is missing or empty",
            field=field,
        )


class InvalidTypeError(ValidationError):
    """Raised when a field has an invalid type."""

    code = "INVALID_TYPE_ERROR"

    def __init__(self, field: str, expected: str, received: str):
        super().__init__(
            f"Field '{field}' expected {expected}, got {received}",
            field=field,
        )
        self.expected = expected
        self.received = received


class InvalidLearningTypeError(ValidationError):
    """Raised when an invalid learning type is specified."""

    code = "INVALID_LEARNING_TYPE"

    def __init__(self, learning_type: str, valid_types: list):
        super().__init__(
            f"Invalid learning type '{learning_type}'",
            field="type",
            value=learning_type,
        )
        self.valid_types = valid_types
        self.details["valid_types"] = valid_types


# =============================================================================
# Storage Errors
# =============================================================================

class StorageError(ContextDNAError):
    """Base class for storage-related errors."""

    code = "STORAGE_ERROR"


class StorageConnectionError(StorageError):
    """Raised when storage backend connection fails."""

    code = "STORAGE_CONNECTION_ERROR"

    def __init__(self, backend: str, reason: Optional[str] = None):
        message = f"Failed to connect to {backend} storage"
        if reason:
            message += f": {reason}"
        super().__init__(message, details={"backend": backend})
        self.backend = backend


class StorageReadError(StorageError):
    """Raised when reading from storage fails."""

    code = "STORAGE_READ_ERROR"

    def __init__(self, operation: str, reason: Optional[str] = None):
        message = f"Failed to read from storage during {operation}"
        if reason:
            message += f": {reason}"
        super().__init__(message, details={"operation": operation})


class StorageWriteError(StorageError):
    """Raised when writing to storage fails."""

    code = "STORAGE_WRITE_ERROR"

    def __init__(self, operation: str, reason: Optional[str] = None):
        message = f"Failed to write to storage during {operation}"
        if reason:
            message += f": {reason}"
        super().__init__(message, details={"operation": operation})


class LearningNotFoundError(StorageError):
    """Raised when a requested learning is not found."""

    code = "LEARNING_NOT_FOUND"

    def __init__(self, learning_id: str):
        super().__init__(
            f"Learning not found: {learning_id}",
            details={"learning_id": learning_id},
        )
        self.learning_id = learning_id


class DuplicateLearningError(StorageError):
    """Raised when attempting to create a duplicate learning."""

    code = "DUPLICATE_LEARNING"

    def __init__(self, title: str, existing_id: Optional[str] = None):
        details = {"title": title}
        if existing_id:
            details["existing_id"] = existing_id
        super().__init__(
            f"Similar learning already exists: {title}",
            details=details,
        )


# =============================================================================
# LLM/Provider Errors
# =============================================================================

class LLMError(ContextDNAError):
    """Base class for LLM-related errors."""

    code = "LLM_ERROR"


class ProviderNotFoundError(LLMError):
    """Raised when a requested LLM provider is not available."""

    code = "PROVIDER_NOT_FOUND"

    def __init__(self, provider: str, available: list = None):
        details = {"provider": provider}
        if available:
            details["available"] = available
        super().__init__(
            f"LLM provider not available: {provider}",
            details=details,
        )


class ProviderConfigurationError(LLMError):
    """Raised when LLM provider is misconfigured."""

    code = "PROVIDER_CONFIGURATION_ERROR"

    def __init__(self, provider: str, missing: list = None):
        message = f"Provider '{provider}' is not properly configured"
        details = {"provider": provider}
        if missing:
            message += f". Missing: {', '.join(missing)}"
            details["missing"] = missing
        super().__init__(message, details=details)


class LLMRateLimitError(LLMError):
    """Raised when LLM rate limit is exceeded."""

    code = "LLM_RATE_LIMIT"

    def __init__(self, provider: str, retry_after: Optional[int] = None):
        message = f"Rate limit exceeded for {provider}"
        details = {"provider": provider}
        if retry_after:
            message += f". Retry after {retry_after} seconds"
            details["retry_after"] = retry_after
        super().__init__(message, details=details)


class LLMResponseError(LLMError):
    """Raised when LLM returns an unexpected response."""

    code = "LLM_RESPONSE_ERROR"

    def __init__(self, provider: str, reason: str):
        super().__init__(
            f"Unexpected response from {provider}: {reason}",
            details={"provider": provider, "reason": reason},
        )


# =============================================================================
# Security Errors
# =============================================================================

class SecurityError(ContextDNAError):
    """Base class for security-related errors."""

    code = "SECURITY_ERROR"


class SecretDetectedError(SecurityError):
    """Raised when a secret is detected in content that should be sanitized."""

    code = "SECRET_DETECTED"

    def __init__(self, secret_type: str, count: int = 1):
        message = f"Detected {secret_type} secret"
        if count > 1:
            message = f"Detected {count} {secret_type} secrets"
        super().__init__(
            message + " - content will be sanitized",
            details={"secret_type": secret_type, "count": count},
        )


class UnauthorizedError(SecurityError):
    """Raised when access is denied."""

    code = "UNAUTHORIZED"

    def __init__(self, resource: str = "resource"):
        super().__init__(
            f"Access denied to {resource}",
            details={"resource": resource},
        )


# =============================================================================
# Configuration Errors
# =============================================================================

class ConfigurationError(ContextDNAError):
    """Base class for configuration-related errors."""

    code = "CONFIGURATION_ERROR"


class MissingConfigurationError(ConfigurationError):
    """Raised when required configuration is missing."""

    code = "MISSING_CONFIGURATION"

    def __init__(self, config_key: str, hint: Optional[str] = None):
        message = f"Missing required configuration: {config_key}"
        details = {"config_key": config_key}
        if hint:
            message += f". {hint}"
            details["hint"] = hint
        super().__init__(message, details=details)


class InvalidConfigurationError(ConfigurationError):
    """Raised when configuration value is invalid."""

    code = "INVALID_CONFIGURATION"

    def __init__(self, config_key: str, reason: str):
        super().__init__(
            f"Invalid configuration for '{config_key}': {reason}",
            details={"config_key": config_key, "reason": reason},
        )


# =============================================================================
# API/Server Errors
# =============================================================================

class APIError(ContextDNAError):
    """Base class for API-related errors."""

    code = "API_ERROR"
    http_status: int = 500


class BadRequestError(APIError):
    """Raised for malformed API requests."""

    code = "BAD_REQUEST"
    http_status = 400


class NotFoundError(APIError):
    """Raised when requested resource is not found."""

    code = "NOT_FOUND"
    http_status = 404

    def __init__(self, resource: str = "resource"):
        super().__init__(f"{resource} not found")


class RateLimitExceededError(APIError):
    """Raised when API rate limit is exceeded."""

    code = "RATE_LIMIT_EXCEEDED"
    http_status = 429

    def __init__(self, retry_after: Optional[int] = None):
        message = "Rate limit exceeded"
        details = {}
        if retry_after:
            message += f". Retry after {retry_after} seconds"
            details["retry_after"] = retry_after
        super().__init__(message, details=details)


class ServerError(APIError):
    """Raised for internal server errors."""

    code = "SERVER_ERROR"
    http_status = 500


# =============================================================================
# Hook/Integration Errors
# =============================================================================

class HookError(ContextDNAError):
    """Base class for hook-related errors."""

    code = "HOOK_ERROR"


class HookInstallationError(HookError):
    """Raised when hook installation fails."""

    code = "HOOK_INSTALLATION_ERROR"

    def __init__(self, hook_type: str, reason: str):
        super().__init__(
            f"Failed to install {hook_type} hook: {reason}",
            details={"hook_type": hook_type, "reason": reason},
        )


class HookExecutionError(HookError):
    """Raised when hook execution fails."""

    code = "HOOK_EXECUTION_ERROR"

    def __init__(self, hook_type: str, reason: str):
        super().__init__(
            f"Hook {hook_type} failed: {reason}",
            details={"hook_type": hook_type, "reason": reason},
        )


# =============================================================================
# Utility Functions
# =============================================================================

def wrap_exception(
    exc: Exception,
    error_class: type = ContextDNAError,
    message: Optional[str] = None,
) -> ContextDNAError:
    """Wrap a generic exception in a Context DNA exception.

    Args:
        exc: Original exception
        error_class: Context DNA exception class to use
        message: Custom message (defaults to str(exc))

    Returns:
        Wrapped ContextDNAError instance

    Example:
        try:
            risky_operation()
        except Exception as e:
            raise wrap_exception(e, StorageError, "Operation failed")
    """
    wrapped = error_class(
        message or str(exc),
        details={"original_error": type(exc).__name__},
    )
    wrapped.__cause__ = exc
    return wrapped
