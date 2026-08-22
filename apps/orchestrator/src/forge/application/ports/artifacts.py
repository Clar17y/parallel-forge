"""Framework-free artifact storage and lineage contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.artifact import ArtifactDescriptor


class ArtifactStore(Protocol):
    """Asynchronous verified content-addressed byte storage."""

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = "none",
    ) -> ArtifactDescriptor: ...

    async def open_bytes(self, digest: str) -> bytes: ...

    async def verify(self, digest: str) -> bool: ...


class ArtifactRepository(Protocol):
    """Run-scoped immutable content metadata and lineage."""

    async def record(
        self,
        descriptor: ArtifactDescriptor,
        *,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID | None = None,
        parent_digests: Sequence[str] = (),
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactDescriptor: ...

    async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor: ...
