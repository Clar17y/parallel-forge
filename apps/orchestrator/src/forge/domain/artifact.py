"""Immutable content-addressed artifact descriptors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import UUID

from forge.observability.redaction import redact_value

_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_MEDIA_TYPE_MAX = 255
_PRODUCER_TYPE_MAX = 96
_SCHEMA_VERSION = 1
_CANONICAL_PREFIX = "sha256/"


def canonical_storage_pointer(digest: str) -> str:
    """Return the only relative storage pointer accepted for a digest."""

    validate_artifact_digest(digest)
    return f"{_CANONICAL_PREFIX}{digest[:2]}/{digest[2:]}.blob"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_metadata(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactDescriptor:
    """Immutable metadata describing one verified content blob."""

    digest: str
    media_type: str
    byte_count: int
    storage_path: Path
    producer_type: str = "filesystem"
    producer_id: UUID | None = None
    run_id: UUID | None = None
    parent_digests: tuple[str, ...] = ()
    schema_version: int = _SCHEMA_VERSION
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: Mapping[str, object] = field(default_factory=dict)
    truncated: bool = False
    original_byte_count: int | None = None
    truncation_policy: str = "none"

    def __post_init__(self) -> None:
        validate_artifact_digest(self.digest)
        _require_bounded_nonempty(self.media_type, "media type", _MEDIA_TYPE_MAX)
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("artifact byte count must be a nonnegative integer")
        if not isinstance(self.storage_path, Path):
            object.__setattr__(self, "storage_path", Path(self.storage_path))
        _validate_storage_path(self.storage_path, self.digest)
        _require_bounded_nonempty(self.producer_type, "producer type", _PRODUCER_TYPE_MAX)
        if self.producer_id is not None and not isinstance(self.producer_id, UUID):
            raise TypeError("artifact producer id must be a UUID")
        if self.run_id is not None and not isinstance(self.run_id, UUID):
            raise TypeError("artifact run id must be a UUID")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("artifact schema version must be positive")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("artifact creation time must be timezone-aware")
        parents = tuple(self.parent_digests)
        if parents != tuple(sorted(set(parents))):
            raise ValueError("artifact parent digests must be unique and sorted")
        for parent in parents:
            validate_artifact_digest(parent)
            if parent == self.digest:
                raise ValueError("artifact cannot parent itself")
        if type(self.truncated) is not bool:
            raise TypeError("artifact truncation flag must be boolean")
        original = self.byte_count if self.original_byte_count is None else self.original_byte_count
        if type(original) is not int or original < 0:
            raise ValueError("artifact original byte count must be nonnegative")
        if self.truncated:
            if original <= self.byte_count or not self.truncation_policy:
                raise ValueError("truncated artifacts require a larger source and policy")
        elif original != self.byte_count or self.truncation_policy != "none":
            raise ValueError("untruncated artifacts must have coherent source metadata")
        object.__setattr__(self, "parent_digests", parents)
        object.__setattr__(self, "original_byte_count", original)
        # Keep descriptor metadata detached and bounded before it can be persisted/logged.
        bounded = redact_value(dict(self.metadata))
        if not isinstance(bounded, Mapping):
            raise TypeError("artifact metadata must be an object")
        object.__setattr__(self, "metadata", _freeze(dict(bounded)))

    @property
    def storage_pointer(self) -> str:
        """Return the canonical relative pointer persisted by repositories."""

        return canonical_storage_pointer(self.digest)


def validate_artifact_digest(value: str) -> None:
    """Reject anything except one canonical lowercase ASCII SHA-256 digest."""

    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("artifact digest must be lowercase hexadecimal SHA-256")


def _require_bounded_nonempty(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1-{maximum} characters")


def _validate_storage_path(value: Path, digest: str) -> None:
    expected = PurePosixPath(canonical_storage_pointer(digest))
    if value.is_absolute():
        parts = tuple(value.parts[-3:])
        if parts != tuple(expected.parts):
            raise ValueError("artifact storage path is not canonical")
        return
    if value.as_posix() != expected.as_posix():
        raise ValueError("artifact storage path is not canonical")


__all__ = [
    "ArtifactDescriptor",
    "canonical_storage_pointer",
    "thaw_metadata",
    "validate_artifact_digest",
]
