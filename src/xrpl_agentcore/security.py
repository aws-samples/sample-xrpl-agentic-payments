"""Shared redaction helpers for structured application logs."""

from __future__ import annotations

from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_LOG_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "approval_idempotency_key",
        "bank_account",
        "id_token",
        "jwt",
        "owner_sub",
        "password",
        "recipient_address",
        "refresh_token",
        "seed",
        "signed_blob",
        "token",
        "wallet_seed",
    }
)


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        normalized == sensitive or normalized.endswith(f"_{sensitive}")
        for sensitive in SENSITIVE_LOG_KEYS
    )


def redact_for_log(value: Any) -> Any:
    """Recursively redact credentials, signing material, and direct PII."""

    if isinstance(value, dict):
        return {
            str(key): REDACTED if _sensitive_key(str(key)) else redact_for_log(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_for_log(child) for child in value]
    return value
