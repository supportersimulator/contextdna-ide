#!/usr/bin/env python3
"""
Secret Sanitization System - Automatic Protection for Context DNA

This module provides sophisticated secret detection and sanitization:
1. Automatically detects secrets before storage
2. Replaces sensitive values with safe placeholders
3. Prevents accidental credential exposure
4. Supports custom pattern registration

SECURITY:
All content is automatically sanitized before storage:
- EC2 instance IDs → ${INSTANCE_ID}
- API keys → ${API_KEY}
- IP addresses → ${IP_ADDRESS}
- AWS ARNs → ${ARN}
- Connection strings → ${CONNECTION_STRING}
- SSH private keys → ${PRIVATE_KEY}
- Bearer tokens → ${TOKEN}

Actual secrets are NEVER stored - only placeholders.

Usage:
    from context_dna.security import sanitize_secrets, detect_secrets

    # Sanitize content before storage
    safe_content = sanitize_secrets(raw_content)

    # Check for secrets without sanitizing
    detected = detect_secrets(content)
    for secret in detected:
        print(f"Found {secret['pattern']} at position {secret['position']}")

    # Check if safe to store
    if is_safe_to_store(content):
        store(content)
    else:
        store(sanitize_secrets(content))
"""

import re
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


# =============================================================================
# SECRET PATTERNS (Ordered by specificity - most specific first)
# =============================================================================

SECRET_PATTERNS: List[Tuple[str, str, str]] = [
    # Pattern, Replacement, Description

    # AWS EC2 Instance IDs (i-0a1b2c3d4e5f67890)
    (r'i-[0-9a-f]{8,17}', '${INSTANCE_ID}', 'AWS EC2 Instance ID'),

    # AWS ARNs (most specific AWS pattern)
    (r'arn:aws:[a-zA-Z0-9\-]+:[a-z0-9\-]*:\d*:[a-zA-Z0-9\-_/:.]+', '${ARN}', 'AWS ARN'),

    # OpenAI API Keys (various formats)
    (r'sk-proj-[a-zA-Z0-9\-_]{32,}', '${OPENAI_KEY}', 'OpenAI Project Key'),
    (r'sk-[a-zA-Z0-9]{32,}', '${OPENAI_KEY}', 'OpenAI API Key'),

    # Anthropic API Keys
    (r'sk-ant-[a-zA-Z0-9\-]{32,}', '${ANTHROPIC_KEY}', 'Anthropic API Key'),

    # AWS Access Keys
    (r'AKIA[0-9A-Z]{16}', '${AWS_ACCESS_KEY}', 'AWS Access Key ID'),
    (r'(?<![a-zA-Z0-9/+])[a-zA-Z0-9/+]{40}(?![a-zA-Z0-9/+])', '${AWS_SECRET_KEY}', 'AWS Secret Key'),

    # Google API Keys
    (r'AIza[0-9A-Za-z\-_]{35}', '${GOOGLE_API_KEY}', 'Google API Key'),

    # GitHub Tokens
    (r'ghp_[a-zA-Z0-9]{36}', '${GITHUB_TOKEN}', 'GitHub Personal Access Token'),
    (r'gho_[a-zA-Z0-9]{36}', '${GITHUB_OAUTH}', 'GitHub OAuth Token'),
    (r'ghu_[a-zA-Z0-9]{36}', '${GITHUB_USER_TOKEN}', 'GitHub User Token'),
    (r'ghs_[a-zA-Z0-9]{36}', '${GITHUB_SERVER_TOKEN}', 'GitHub Server Token'),
    (r'ghr_[a-zA-Z0-9]{36}', '${GITHUB_REFRESH_TOKEN}', 'GitHub Refresh Token'),

    # Stripe Keys
    (r'sk_live_[a-zA-Z0-9]{24,}', '${STRIPE_SECRET_KEY}', 'Stripe Live Secret Key'),
    (r'sk_test_[a-zA-Z0-9]{24,}', '${STRIPE_TEST_KEY}', 'Stripe Test Secret Key'),
    (r'pk_live_[a-zA-Z0-9]{24,}', '${STRIPE_PUBLIC_KEY}', 'Stripe Live Public Key'),
    (r'pk_test_[a-zA-Z0-9]{24,}', '${STRIPE_TEST_PUBLIC_KEY}', 'Stripe Test Public Key'),

    # Database Connection Strings
    (r'postgres(ql)?://[^\s"\'<>]+', '${DATABASE_URL}', 'PostgreSQL Connection String'),
    (r'mysql://[^\s"\'<>]+', '${DATABASE_URL}', 'MySQL Connection String'),
    (r'mongodb(\+srv)?://[^\s"\'<>]+', '${DATABASE_URL}', 'MongoDB Connection String'),
    (r'redis://[^\s"\'<>]+', '${REDIS_URL}', 'Redis Connection String'),
    (r'amqp://[^\s"\'<>]+', '${AMQP_URL}', 'AMQP Connection String'),

    # IP Addresses (preserve localhost and common internal)
    (r'(?<![\d.])(?!127\.0\.0\.)(?!0\.0\.0\.)(?!192\.168\.)(?!10\.)(?!172\.(1[6-9]|2[0-9]|3[0-1])\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])', '${IP_ADDRESS}', 'Public IP Address'),

    # Generic secrets in env vars
    (r'(PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|API_KEY)["\']?\s*[:=]\s*["\']?[a-zA-Z0-9\-_]{16,}["\']?', '${REDACTED_SECRET}', 'Generic Secret'),

    # Bearer tokens
    (r'Bearer\s+[a-zA-Z0-9\-_.]+', 'Bearer ${TOKEN}', 'Bearer Token'),

    # Basic Auth in URLs
    (r'://[a-zA-Z0-9\-_.]+:[a-zA-Z0-9\-_.]+@', '://${USER}:${PASSWORD}@', 'Basic Auth Credentials'),

    # SSH Private Keys
    (r'-----BEGIN [A-Z]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z]+ PRIVATE KEY-----', '${PRIVATE_KEY}', 'SSH Private Key'),

    # PEM Certificates (could contain private data)
    (r'-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----', '${CERTIFICATE}', 'Certificate'),

    # JSON Web Tokens (JWT)
    (r'eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*', '${JWT}', 'JSON Web Token'),

    # Slack Tokens
    (r'xox[baprs]-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*', '${SLACK_TOKEN}', 'Slack Token'),

    # Twilio
    (r'SK[a-fA-F0-9]{32}', '${TWILIO_KEY}', 'Twilio API Key'),
    (r'AC[a-fA-F0-9]{32}', '${TWILIO_SID}', 'Twilio Account SID'),

    # SendGrid
    (r'SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}', '${SENDGRID_KEY}', 'SendGrid API Key'),

    # npm tokens
    (r'npm_[a-zA-Z0-9]{36}', '${NPM_TOKEN}', 'NPM Token'),

    # PyPI tokens
    (r'pypi-[a-zA-Z0-9]{36,}', '${PYPI_TOKEN}', 'PyPI Token'),

    # Generic hex tokens (32+ chars, likely secrets)
    (r'(?<![a-fA-F0-9])[a-fA-F0-9]{32,}(?![a-fA-F0-9])', '${HEX_TOKEN}', 'Hex Token'),
]


# =============================================================================
# CORE FUNCTIONS
# =============================================================================

def sanitize_secrets(content: str, custom_patterns: List[Tuple[str, str, str]] = None) -> str:
    """
    Sanitize secrets from content before storage.

    Replaces sensitive values with placeholder tokens.
    Artifacts stored with placeholders are safe to share and search.

    Args:
        content: Raw content that may contain secrets
        custom_patterns: Additional patterns to check [(regex, replacement, description)]

    Returns:
        Sanitized content with placeholders

    Example:
        >>> raw = "API_KEY=sk-1234567890abcdef"
        >>> sanitize_secrets(raw)
        'API_KEY=${OPENAI_KEY}'
    """
    if not content:
        return content

    sanitized = content
    all_patterns = SECRET_PATTERNS + (custom_patterns or [])

    for pattern_tuple in all_patterns:
        pattern = pattern_tuple[0]
        replacement = pattern_tuple[1]
        try:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
        except re.error:
            # Skip invalid patterns
            continue

    return sanitized


def detect_secrets(content: str, custom_patterns: List[Tuple[str, str, str]] = None) -> List[Dict]:
    """
    Detect potential secrets in content without sanitizing.

    Useful for validation, warning generation, and pre-flight checks.

    Args:
        content: Content to scan
        custom_patterns: Additional patterns to check

    Returns:
        List of detected secrets with pattern info:
        [
            {
                "pattern_type": "AWS Access Key",
                "placeholder": "${AWS_ACCESS_KEY}",
                "value_preview": "AKIA1234...",
                "position": 42,
                "line_number": 3
            }
        ]

    Example:
        >>> secrets = detect_secrets("key: sk-abcdef123456789")
        >>> len(secrets)
        1
        >>> secrets[0]['pattern_type']
        'OpenAI API Key'
    """
    if not content:
        return []

    detected = []
    all_patterns = SECRET_PATTERNS + (custom_patterns or [])
    lines = content.split('\n')

    for pattern_tuple in all_patterns:
        pattern = pattern_tuple[0]
        replacement = pattern_tuple[1]
        description = pattern_tuple[2] if len(pattern_tuple) > 2 else "Unknown"

        try:
            matches = list(re.finditer(pattern, content, flags=re.IGNORECASE))
            for match in matches:
                # Calculate line number
                line_num = content[:match.start()].count('\n') + 1

                # Create safe preview (first 8 chars + ...)
                value = match.group()
                if len(value) > 8:
                    preview = value[:8] + "..." + value[-4:] if len(value) > 12 else value[:8] + "..."
                else:
                    preview = value

                detected.append({
                    "pattern_type": description,
                    "placeholder": replacement,
                    "value_preview": preview,
                    "position": match.start(),
                    "line_number": line_num,
                    "full_match_length": len(value),
                })
        except re.error:
            continue

    return detected


def is_safe_to_store(content: str, custom_patterns: List[Tuple[str, str, str]] = None) -> bool:
    """
    Check if content is safe to store without sanitization.

    Args:
        content: Content to check
        custom_patterns: Additional patterns to check

    Returns:
        True if no secrets detected, False otherwise

    Example:
        >>> is_safe_to_store("Hello world")
        True
        >>> is_safe_to_store("key: sk-secret123456789")
        False
    """
    return len(detect_secrets(content, custom_patterns)) == 0


def get_secret_report(content: str, custom_patterns: List[Tuple[str, str, str]] = None) -> str:
    """
    Generate a human-readable report of detected secrets.

    Args:
        content: Content to analyze
        custom_patterns: Additional patterns to check

    Returns:
        Formatted report string

    Example:
        >>> print(get_secret_report(code_with_secrets))
        === Secret Detection Report ===
        Found 3 potential secrets:

        1. OpenAI API Key (line 5)
           Preview: sk-abc1...7890
           Replace with: ${OPENAI_KEY}
        ...
    """
    detected = detect_secrets(content, custom_patterns)

    if not detected:
        return "=== Secret Detection Report ===\nNo secrets detected. Content is safe to store."

    lines = ["=== Secret Detection Report ===", f"Found {len(detected)} potential secret(s):", ""]

    for i, secret in enumerate(detected, 1):
        lines.append(f"{i}. {secret['pattern_type']} (line {secret['line_number']})")
        lines.append(f"   Preview: {secret['value_preview']}")
        lines.append(f"   Replace with: {secret['placeholder']}")
        lines.append("")

    lines.append("Run sanitize_secrets() to automatically replace these with safe placeholders.")

    return "\n".join(lines)


# =============================================================================
# CUSTOM PATTERN REGISTRATION
# =============================================================================

_custom_patterns: List[Tuple[str, str, str]] = []


def register_pattern(regex: str, replacement: str, description: str) -> None:
    """
    Register a custom secret pattern for the current session.

    Use this to add project-specific patterns that aren't covered by defaults.

    Args:
        regex: Regular expression pattern
        replacement: Placeholder to use (e.g., "${MY_SECRET}")
        description: Human-readable description

    Example:
        >>> register_pattern(r'MYAPP-[a-zA-Z0-9]{32}', '${MYAPP_KEY}', 'MyApp API Key')
    """
    _custom_patterns.append((regex, replacement, description))


def clear_custom_patterns() -> None:
    """Clear all registered custom patterns."""
    _custom_patterns.clear()


def get_all_patterns() -> List[Tuple[str, str, str]]:
    """Get all patterns (default + custom)."""
    return SECRET_PATTERNS + _custom_patterns


# =============================================================================
# FILE-LEVEL SANITIZATION
# =============================================================================

def sanitize_file(file_path: str, output_path: str = None, dry_run: bool = False) -> Dict:
    """
    Sanitize secrets in a file.

    Args:
        file_path: Path to file to sanitize
        output_path: Path to write sanitized content (default: overwrite)
        dry_run: If True, don't write, just report

    Returns:
        Result dict with detected secrets and action taken

    Example:
        >>> result = sanitize_file("config.py", dry_run=True)
        >>> print(f"Found {result['secrets_found']} secrets")
    """
    from pathlib import Path

    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    content = path.read_text()
    detected = detect_secrets(content)
    sanitized = sanitize_secrets(content)

    result = {
        "file": file_path,
        "secrets_found": len(detected),
        "secrets": detected,
        "dry_run": dry_run,
        "changed": content != sanitized,
    }

    if not dry_run and content != sanitized:
        output = Path(output_path) if output_path else path
        output.write_text(sanitized)
        result["written_to"] = str(output)

    return result


# =============================================================================
# CLI INTERFACE
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Context DNA Secret Sanitizer")
        print()
        print("Commands:")
        print("  scan <file>        - Scan file for secrets")
        print("  sanitize <file>    - Sanitize file (dry-run)")
        print("  sanitize <file> -w - Sanitize file (write changes)")
        print("  test               - Run self-test")
        print()
        print("Examples:")
        print("  python sanitizer.py scan config.py")
        print("  python sanitizer.py sanitize .env -w")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "scan":
        if len(sys.argv) < 3:
            print("Usage: scan <file>")
            sys.exit(1)

        file_path = sys.argv[2]
        from pathlib import Path

        if not Path(file_path).exists():
            print(f"File not found: {file_path}")
            sys.exit(1)

        content = Path(file_path).read_text()
        print(get_secret_report(content))

    elif cmd == "sanitize":
        if len(sys.argv) < 3:
            print("Usage: sanitize <file> [-w]")
            sys.exit(1)

        file_path = sys.argv[2]
        write = len(sys.argv) > 3 and sys.argv[3] == "-w"

        result = sanitize_file(file_path, dry_run=not write)

        if "error" in result:
            print(result["error"])
            sys.exit(1)

        if result["secrets_found"] == 0:
            print(f"No secrets found in {file_path}")
        else:
            print(f"Found {result['secrets_found']} secrets:")
            for s in result["secrets"]:
                print(f"  - {s['pattern_type']}: {s['value_preview']}")

            if write:
                print(f"\n✅ Sanitized content written to {result.get('written_to', file_path)}")
            else:
                print("\nDry run - use -w flag to write changes")

    elif cmd == "test":
        # Self-test
        test_cases = [
            ("sk-proj-abc123def456ghi789jkl012mno345pqr678", "OpenAI"),
            ("AKIAIOSFODNN7EXAMPLE", "AWS Access Key"),
            ("postgres://user:pass@host:5432/db", "PostgreSQL"),
            ("192.168.1.1", "Internal IP (should NOT match)"),
            ("54.123.45.67", "Public IP"),
            ("ghp_1234567890abcdefghijklmnopqrstuvwxyz", "GitHub Token"),
        ]

        print("=== Secret Sanitizer Self-Test ===\n")
        for test_value, expected_type in test_cases:
            detected = detect_secrets(test_value)
            sanitized = sanitize_secrets(test_value)

            if detected:
                print(f"✅ {expected_type}:")
                print(f"   Input:     {test_value[:30]}...")
                print(f"   Detected:  {detected[0]['pattern_type']}")
                print(f"   Sanitized: {sanitized}")
            else:
                print(f"⚠️ {expected_type}:")
                print(f"   Input:     {test_value}")
                print(f"   Detected:  None")
            print()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
