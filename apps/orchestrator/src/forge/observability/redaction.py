"""Bounded recursive redaction shared by persistence, logs, and traces."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any

from forge.domain.payload import redact_durable_text

REDACTED = "[REDACTED]"
UNSUPPORTED = "[UNSUPPORTED]"
_TRUNCATION_KEY = "__forge_truncated__"
_SECRET_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "privatekey",
    "databaseurl",
    "apikey",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RedactionPolicy:
    """Hard limits applied before an observability value crosses a boundary."""

    max_string_bytes: int = 4096
    max_collection_items: int = 100
    max_depth: int = 12
    max_nodes: int = 10_000

    def __post_init__(self) -> None:
        values = {
            "max_string_bytes": (self.max_string_bytes, 64),
            "max_collection_items": (self.max_collection_items, 2),
            "max_depth": (self.max_depth, 1),
            "max_nodes": (self.max_nodes, 1),
        }
        for name, (value, minimum) in values.items():
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}")


@dataclass(slots=True)
class _Traversal:
    nodes: int = 0
    active_ids: set[int] | None = None

    def __post_init__(self) -> None:
        if self.active_ids is None:
            self.active_ids = set()


class Redactor:
    """An instance-scoped redactor with an immutable literal-secret registry."""

    def __init__(
        self,
        *,
        secrets: Iterable[str] = (),
        policy: RedactionPolicy | None = None,
    ) -> None:
        registered: list[str] = []
        for secret in secrets:
            if not isinstance(secret, str) or not secret:
                raise ValueError("registered secrets must be nonempty strings")
            registered.append(secret)
        self._secrets = tuple(sorted(set(registered), key=lambda item: (-len(item), item)))
        self._policy = policy or RedactionPolicy()

    @property
    def policy(self) -> RedactionPolicy:
        """Expose the immutable bounds used by this redactor."""

        return self._policy

    def redact(self, value: Any) -> Any:
        """Return a detached, bounded, JSON-compatible representation."""

        return self._redact(value, depth=0, traversal=_Traversal())

    def _redact(self, value: Any, *, depth: int, traversal: _Traversal) -> Any:
        traversal.nodes += 1
        if traversal.nodes > self._policy.max_nodes:
            return "[TRUNCATED node_limit]"
        if depth > self._policy.max_depth:
            return "[TRUNCATED depth_limit]"

        if isinstance(value, str):
            return self._redact_string(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else UNSUPPORTED

        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth=depth, traversal=traversal)
        if isinstance(value, (list, tuple)):
            return self._redact_sequence(value, depth=depth, traversal=traversal)
        return UNSUPPORTED

    def _redact_mapping(
        self,
        value: Mapping[object, object],
        *,
        depth: int,
        traversal: _Traversal,
    ) -> dict[str, Any] | str:
        identity = id(value)
        assert traversal.active_ids is not None
        if identity in traversal.active_ids:
            return "[TRUNCATED cycle]"
        traversal.active_ids.add(identity)
        try:
            original_count = len(value)
            truncated = original_count > self._policy.max_collection_items
            retain_count = self._policy.max_collection_items - 1 if truncated else original_count
            retained = islice(value.items(), retain_count)
            result: dict[str, Any] = {}
            for raw_key, item in retained:
                if not isinstance(raw_key, str):
                    key = self._unique_key(result, "__forge_unsupported_key__")
                    result[key] = UNSUPPORTED
                    continue
                key = self._unique_key(result, self._redact_string(raw_key))
                if self._is_secret_key(raw_key):
                    result[key] = REDACTED
                else:
                    result[key] = self._redact(
                        item,
                        depth=depth + 1,
                        traversal=traversal,
                    )
            if truncated:
                result[self._unique_key(result, _TRUNCATION_KEY)] = original_count
            return result
        finally:
            traversal.active_ids.remove(identity)

    def _redact_sequence(
        self,
        value: list[object] | tuple[object, ...],
        *,
        depth: int,
        traversal: _Traversal,
    ) -> list[Any] | str:
        identity = id(value)
        assert traversal.active_ids is not None
        if identity in traversal.active_ids:
            return "[TRUNCATED cycle]"
        traversal.active_ids.add(identity)
        try:
            truncated = len(value) > self._policy.max_collection_items
            retained = value[: self._policy.max_collection_items - 1] if truncated else value
            result = [self._redact(item, depth=depth + 1, traversal=traversal) for item in retained]
            if truncated:
                result.append({_TRUNCATION_KEY: len(value)})
            return result
        finally:
            traversal.active_ids.remove(identity)

    def _redact_string(self, value: str) -> str:
        normalized = _normalize_string(value)
        redacted = normalized
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        redacted = redact_durable_text(redacted)
        return self._truncate_string(redacted, original_bytes=len(normalized.encode("utf-8")))

    def _truncate_string(self, value: str, *, original_bytes: int | None = None) -> str:
        value = _normalize_string(value)
        encoded = value.encode("utf-8")
        if len(encoded) <= self._policy.max_string_bytes:
            return value
        source_bytes = original_bytes if original_bytes is not None else len(encoded)
        marker = f"[TRUNCATED {source_bytes}B]"
        marker_bytes = marker.encode("ascii")
        if len(marker_bytes) >= self._policy.max_string_bytes:
            raise ValueError("max_string_bytes cannot represent the truncation marker")
        budget = self._policy.max_string_bytes - len(marker_bytes)
        prefix = encoded[:budget].decode("utf-8", errors="ignore")
        while len(prefix.encode("utf-8")) + len(marker_bytes) > self._policy.max_string_bytes:
            prefix = prefix[:-1]
        return prefix + marker

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        normalized = "".join(character for character in key.lower() if character.isalnum())
        return any(part in normalized for part in _SECRET_KEY_PARTS)

    @staticmethod
    def _unique_key(result: Mapping[str, object], preferred: str) -> str:
        candidate = preferred
        while candidate in result:
            candidate = f"_{candidate}"
        return candidate


def _normalize_string(value: str) -> str:
    """Replace UTF-16 surrogate code points before UTF-8/JSON boundaries."""

    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character for character in value
    )


def redact_value(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    policy: RedactionPolicy | None = None,
) -> Any:
    """Redact one value with an isolated registry and immutable policy."""

    return Redactor(secrets=secrets, policy=policy).redact(value)


__all__ = ["REDACTED", "RedactionPolicy", "Redactor", "redact_value"]
