"""Redaction helpers for provider errors, logs, and readiness artifacts.

The provider boundary must never echo a credential.  The adapter and ledger
register known secret values here so exception messages and command output can
be sanitized in one place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

REDACTED: Final = "[REDACTED]"
MAX_MESSAGE_CHARS: Final = 500

_GENERIC_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Return a bounded string with known and recognizable credentials removed."""

    text = str(value)
    for pattern in _GENERIC_SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    candidates = {secret for secret in secrets if secret and len(secret) >= 4}
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "...[truncated]"
    return text


def _redact_detail(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {str(key): _redact_detail(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_detail(item, secrets) for item in value]
    return value


class SecretRedactor:
    """Small immutable registry used by adapters and artifact builders."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def text(self, value: object) -> str:
        return redact_text(value, self._secrets)

    def details(self, value: Mapping[str, Any]) -> dict[str, Any]:
        redacted = _redact_detail(dict(value), self._secrets)
        assert isinstance(redacted, dict)
        return redacted

    def __repr__(self) -> str:
        return f"SecretRedactor(secrets={len(self._secrets)})"
