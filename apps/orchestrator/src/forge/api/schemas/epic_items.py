"""Epic work-item graph HTTP request and response schemas."""

from __future__ import annotations

from pydantic import Field

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

    readiness: list[ItemReadiness] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: GraphRevisionRecord) -> GraphRevisionResponse:
        return cls(**dict(record), readiness=project_readiness(record.items))


class AcceptedGraphResponse(AcceptedGraph):
    """Immutable accepted graph projection with deterministic readiness projections."""

    readiness: list[ItemReadiness] = Field(default_factory=list)

    @classmethod
    def from_record(cls, record: AcceptedGraph) -> AcceptedGraphResponse:
        return cls(**dict(record), readiness=project_readiness(record.items))


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
