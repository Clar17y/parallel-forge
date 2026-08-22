"""Bounded recursive validation and redaction for durable JSON payloads."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_REDACTED = frozenset({"[REDACTED]", "<redacted>", "REDACTED"})
_SECRET_KEY = re.compile(
    r"^(?:password|secret|credential|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|private[_-]?key|authorization|token)$",
    re.IGNORECASE,
)
_SECRET_SUFFIX = ("password", "secret", "credential", "token")
_AUTH_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/._~=-]{8,}")
_GITHUB_TOKEN = re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_ASSIGNMENT = re.compile(
    r"(?ix)\b(?:x[-_ ]?)?(?:api[-_ ]?(?:key|token)|access[-_]?token|refresh[-_]?token|"
    r"auth(?:orization)?|password|secret|credential|token)\s*[:=]\s*"
    r"(?:[\"']([^\"']+)[\"']|([^\s,;]+))"
)
_DATABASE_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----"
)


def contains_credential(value: str) -> bool:
    """Return whether a string contains a credential-bearing representation."""

    return bool(
        _AUTH_VALUE.search(value)
        or _GITHUB_TOKEN.search(value)
        or _ASSIGNMENT.search(value)
        or _DATABASE_URL.search(value)
        or _PRIVATE_KEY.search(value)
    )


def redact_durable_text(value: str) -> str:
    """Redact known credential forms without retaining their secret values."""

    redacted = _PRIVATE_KEY.sub("[REDACTED]", value)
    redacted = _DATABASE_URL.sub(
        lambda match: re.sub(r":([^/@]+)@", ":[REDACTED]@", match.group(0)), redacted
    )
    redacted = _GITHUB_TOKEN.sub("[REDACTED]", redacted)
    redacted = _AUTH_VALUE.sub(
        lambda match: match.group(0).split(maxsplit=1)[0] + " [REDACTED]", redacted
    )
    redacted = _ASSIGNMENT.sub(
        lambda match: (
            f"{match.group(0)[: match.group(0).find('=') + 1]}[REDACTED]"
            if "=" in match.group(0)
            else f"{match.group(0)[: match.group(0).find(':') + 1]}[REDACTED]"
        ),
        redacted,
    )
    return redacted


def validate_durable_payload(value: Any) -> None:
    """Validate a JSON-compatible payload and reject embedded credentials.

    Errors intentionally contain only stable category text; neither payload keys
    nor values are included, so a rejected secret cannot be echoed in a log or
    API error by this validator.
    """

    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise TypeError("durable payload keys must be strings")
            normalized_key = item_key.replace("_", "").replace("-", "").lower()
            secret_key = _SECRET_KEY.fullmatch(item_key) or (
                normalized_key.endswith(_SECRET_SUFFIX)
                and normalized_key not in {"tokenbudget", "tokenlimit"}
            )
            if secret_key and not (
                item_value is None or (isinstance(item_value, str) and item_value in _REDACTED)
            ):
                raise ValueError("durable payload contains a raw credential")
            validate_durable_payload(item_value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_durable_payload(item)
        return
    if isinstance(value, str):
        if contains_credential(value):
            raise ValueError("durable payload contains a raw credential")
        return
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError("durable payload must contain JSON-compatible values")


def validate_durable_error(value: str | None, name: str) -> None:
    """Validate an error summary without echoing or persisting credentials."""

    if value is None:
        return
    if len(value) > 1024:
        raise ValueError(f"{name} is too long")
    if contains_credential(value):
        raise ValueError(f"{name} contains a raw credential")


__all__ = [
    "contains_credential",
    "redact_durable_text",
    "validate_durable_error",
    "validate_durable_payload",
]
