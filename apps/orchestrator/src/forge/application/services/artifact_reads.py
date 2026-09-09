"""Bounded, integrity-checked reads of immutable artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from forge.application.ports.artifact_queries import ArtifactReadQuery
from forge.application.ports.artifacts import ArtifactStore
from forge.domain.artifact import ArtifactDescriptor, validate_artifact_digest

TEXT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-diff",
        "text/x-patch",
        "text/x-log",
        "application/json",
        "application/ld+json",
    }
)
MAX_TEXT_BYTES = 1_048_576
MAX_DOWNLOAD_BYTES = 16_777_216


class ArtifactReadError(RuntimeError):
    """A stored artifact cannot be safely returned."""


class ArtifactReadNotFound(ArtifactReadError):
    """No persisted lineage exists for the requested digest."""


class ArtifactNotRepresentable(ArtifactReadError):
    """The immutable content cannot be served in the requested representation."""


class ArtifactReadService:
    def __init__(
        self,
        query: ArtifactReadQuery,
        store: ArtifactStore,
        *,
        max_text_bytes: int = MAX_TEXT_BYTES,
    ) -> None:
        self._query = query
        self._store = store
        self._max_text_bytes = max_text_bytes

    async def metadata(self, digest: str) -> Sequence[ArtifactDescriptor]:
        validate_artifact_digest(digest)
        records = tuple(await self._query.get_by_digest(digest))
        if not records:
            raise ArtifactReadNotFound("artifact was not found")
        return records

    async def content(
        self, digest: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> tuple[ArtifactDescriptor, bytes]:
        records = await self.metadata(digest)
        descriptor = records[0]
        if descriptor.digest != digest:
            raise ArtifactReadError("artifact metadata digest does not match request")
        if any(
            record.digest != descriptor.digest
            or record.byte_count != descriptor.byte_count
            or record.media_type != descriptor.media_type
            for record in records
        ):
            raise ArtifactReadError("artifact metadata is inconsistent")
        if descriptor.byte_count > max_bytes:
            raise ArtifactNotRepresentable("artifact exceeds the configured read bound")
        try:
            data = await self._store.open_bytes(
                digest, max_bytes=min(max_bytes, descriptor.byte_count)
            )
        except Exception as error:  # storage adapters expose bounded integrity errors
            raise ArtifactReadError("artifact content is unavailable") from error
        if len(data) != descriptor.byte_count or hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactReadError("artifact content failed integrity verification")
        return descriptor, data

    async def text(self, digest: str) -> tuple[ArtifactDescriptor, str]:
        descriptor, data = await self.content(digest, max_bytes=self._max_text_bytes)
        if descriptor.media_type.casefold() not in TEXT_MEDIA_TYPES:
            raise ArtifactNotRepresentable("artifact media type is not text")
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactNotRepresentable("artifact text is not valid UTF-8") from error
        if descriptor.media_type.casefold() in {"application/json", "application/ld+json"}:
            try:
                json.loads(value)
            except (TypeError, ValueError) as error:
                raise ArtifactNotRepresentable("artifact JSON is malformed") from error
        return descriptor, value


__all__ = [
    "MAX_TEXT_BYTES",
    "TEXT_MEDIA_TYPES",
    "ArtifactReadError",
    "ArtifactReadNotFound",
    "ArtifactReadService",
]
