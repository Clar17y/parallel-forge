"""Read-only contracts for persisted artifact metadata."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from forge.domain.artifact import ArtifactDescriptor


class ArtifactReadQuery(Protocol):
    """The API's read boundary for immutable artifact metadata."""

    async def get_by_digest(self, digest: str) -> Sequence[ArtifactDescriptor]: ...


__all__ = ["ArtifactReadQuery"]
