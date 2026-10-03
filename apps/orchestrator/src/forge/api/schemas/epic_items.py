"""Epic work-item graph HTTP request and response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from forge.api.schemas.epic_brief import EpicResponse
from forge.domain.epic_items import (
    AcceptedGraph,
    GraphRevisionRecord,
    ItemInput,
    ItemReadiness,
    ItemSnapshot,
    project_readiness,
)
from forge.domain.epic_items import (
    GraphAdoptionRequest as DomainGraphAdoptionRequest,
)
from forge.domain.epic_items import (
    GraphRevisionCreateRequest as DomainGraphRevisionCreateRequest,
)


class GraphRevisionCreateRequest(DomainGraphRevisionCreateRequest):
    """Closed graph revision creation request body."""


class GraphAdoptionRequest(DomainGraphAdoptionRequest):
    """Closed graph adoption request body."""


GraphRevisionCreate = GraphRevisionCreateRequest
GraphAdoption = GraphAdoptionRequest


class GraphRevisionResponse(GraphRevisionRecord):
    """Immutable graph revision snapshot with deterministic readiness projections."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    readiness: list[ItemReadiness] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: GraphRevisionRecord) -> GraphRevisionResponse:
        return cls(
            schema_version=record.schema_version,
            graph_revision_id=record.graph_revision_id,
            epic_id=record.epic_id,
            brief_revision_id=record.brief_revision_id,
            brief_digest=record.brief_digest,
            revision_number=record.revision_number,
            epic_version=record.epic_version,
            graph_digest=record.graph_digest,
            items=record.items,
            created_at=record.created_at,
            readiness=project_readiness(record.items),
        )


class AcceptedGraphResponse(AcceptedGraph):
    """Immutable accepted graph projection with deterministic readiness projections."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    readiness: list[ItemReadiness] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: AcceptedGraph) -> AcceptedGraphResponse:
        return cls(
            schema_version=record.schema_version,
            epic_id=record.epic_id,
            brief_revision_id=record.brief_revision_id,
            brief_digest=record.brief_digest,
            graph_revision_id=record.graph_revision_id,
            graph_digest=record.graph_digest,
            items=record.items,
            readiness=project_readiness(record.items),
        )


__all__ = [
    "AcceptedGraphResponse",
    "EpicResponse",
    "GraphAdoption",
    "GraphAdoptionRequest",
    "GraphRevisionCreate",
    "GraphRevisionCreateRequest",
    "GraphRevisionResponse",
    "ItemInput",
    "ItemReadiness",
    "ItemSnapshot",
]
