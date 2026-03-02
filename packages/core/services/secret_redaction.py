"""Regex-based secret redaction before sending content to LLM."""

from __future__ import annotations

import re

REDACTION_PLACEHOLDER = "***REDACTED***"

# ── Secret patterns ──────────────────────────────────────────────────────────
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # GitHub tokens (classic PATs: ghp_, OAuth: gho_, user-to-server: ghu_, server: ghs_, refresh: ghr_)
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,255}", re.IGNORECASE)),
    # GitHub fine-grained PATs
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{82,255}", re.IGNORECASE)),
    # AWS access key IDs
    ("aws_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    # AWS secret access keys (40 char alphanum after assignment)
    (
        "aws_secret",
        re.compile(
            r"(?i)(?:aws_secret_access_key|aws_secret)\s*[=:]\s*['\"]?([A-Za-z0-9/+]{40})['\"]?"
        ),
    ),
    # Private keys
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Generic API keys / secrets in assignment context
    (
        "generic_api_key",
        re.compile(
            r'(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|pwd)\s*[=:]\s*["\']?([A-Za-z0-9_\-./+]{20,})["\']?'
        ),
    ),
    # Long hex strings (potential keys / hashes used as secrets)
    ("long_hex", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    # Long base64 strings (>40 chars, not URLs)
    (
        "long_base64",
        re.compile(r"(?<![/\w])(?:[A-Za-z0-9+/]{40,}={0,2})(?![/\w])"),
    ),
    # Connection strings with passwords (optional username before colon)
    (
        "connection_string",
        re.compile(
            r"(?i)(?:postgresql|mysql|mongodb|redis|amqp)(?:s)?://[^:@]*:[^@]+@[^\s\"'<>]+"
        ),
    ),
    # JWT tokens (three base64url segments)
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    ),
]


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Replace secrets in *text* with the redaction placeholder.

    Returns:
        (redacted_text, list_of_match_descriptions)
    """
    redacted = text
    found: list[str] = []

    for name, pattern in _PATTERNS:
        def _replacer(m: re.Match, _name: str = name) -> str:
            found.append(f"{_name}:{m.group(0)[:8]}…")
            return REDACTION_PLACEHOLDER

        new_text = pattern.sub(_replacer, redacted)
        if new_text != redacted:
            redacted = new_text

    return redacted, found


def redact(text: str) -> str:
    """Convenience wrapper that returns only the redacted text."""
    result, _ = redact_secrets(text)
    return result
