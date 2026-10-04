"""Security helpers for AI Key Scout v5.

Secrets are treated as sensitive data: reports/logs should use fingerprints or
masked values, while the original secret remains in memory only when needed for
an explicitly authorized API check.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

SECRET_KEYS = {
    "key", "api_key", "apikey", "token", "access_token", "refresh_token",
    "authorization", "password", "secret", "credential",
}

KEY_ASSIGNMENT_RE = re.compile(
    r"""(?i)(\b(?:api[_-]?key|token|secret|password|authorization|credential)
    \s*[:=]\s*)(['"]?)([^\s,;'"}]+)(\2)"""
)

def fingerprint(secret: str, length: int = 12) -> str:
    """Return a stable non-reversible identifier for a credential."""
    return hashlib.sha256(secret.encode("utf-8", "ignore")).hexdigest()[:length]

def mask_secret(secret: str, visible: int = 4) -> str:
    """Return a UI-safe representation such as abcd…wxyz."""
    if not secret:
        return ""
    if len(secret) <= visible * 2:
        return "•" * len(secret)
    return f"{secret[:visible]}…{secret[-visible:]}"

def redact_text(value: Any) -> str:
    """Redact common credential assignments without exposing the secret."""
    text = str(value)
    return KEY_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(4)}",
        text,
    )

def safe_record(record: Any) -> dict[str, Any]:
    """Convert a key record to a JSON-safe, secret-free summary."""
    key = getattr(record, "key", "") or ""
    return {
        "fingerprint": fingerprint(key) if key else "",
        "masked_key": mask_secret(key),
        "provider": getattr(record, "provider", ""),
        "status": getattr(record, "status", ""),
        "score": getattr(record, "score", 0),
    }
