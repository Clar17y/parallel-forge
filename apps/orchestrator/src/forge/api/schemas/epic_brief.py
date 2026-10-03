"""Epic brief HTTP request and response schemas."""

from __future__ import annotations

from uuid import UUID

from forge.domain.epic_brief import (
    AcceptedBrief,
    BriefContent,
    BriefRequirement,
    BriefRevisionRecord,
    EpicRecord,
)
from forge.domain.epic_brief import (
    BriefAdoptionRequest as DomainBriefAdoptionRequest,
)
from forge.domain.epic_brief import (
    BriefRevisionCreateRequest as DomainBriefRevisionCreateRequest,
)
from forge.domain.epic_brief import (
    EpicCreateRequest as DomainEpicCreateRequest,
)
from forge.domain.epic_brief import (
    EpicDraftUpdateRequest as DomainEpicDraftUpdateRequest,
)


class EpicCreateRequest(DomainEpicCreateRequest):
    """Closed epic creation request body."""


class EpicDraftUpdateRequest(DomainEpicDraftUpdateRequest):
    """Closed epic draft update request body."""


class BriefRevisionCreateRequest(DomainBriefRevisionCreateRequest):
    """Closed brief revision creation request body."""


class BriefAdoptionRequest(DomainBriefAdoptionRequest):
    """Closed brief adoption request body."""


EpicCreate = EpicCreateRequest
EpicDraftUpdate = EpicDraftUpdateRequest
BriefRevisionCreate = BriefRevisionCreateRequest
BriefAdoption = BriefAdoptionRequest


class EpicResponse(EpicRecord):
    """Authoritative epic snapshot with explicit nullable keys."""

    accepted_brief_revision_id: UUID | None = None
    accepted_brief_digest: str | None = None
    accepted_graph_revision_id: UUID | None = None
    accepted_graph_digest: str | None = None

    @classmethod
    def from_record(cls, record: EpicRecord) -> EpicResponse:
        return cls(**dict(record))


class BriefRevisionResponse(BriefRevisionRecord):
    """Immutable brief revision snapshot with explicit nullable keys."""

    source_job_id: UUID | None = None

    @classmethod
    def from_record(cls, record: BriefRevisionRecord) -> BriefRevisionResponse:
        return cls(**dict(record))


class AcceptedBriefResponse(AcceptedBrief):
    """Immutable accepted brief projection."""

    @classmethod
    def from_record(cls, record: AcceptedBrief) -> AcceptedBriefResponse:
        return cls(**dict(record))


__all__ = [
    "AcceptedBriefResponse",
    "BriefAdoption",
    "BriefAdoptionRequest",
    "BriefContent",
    "BriefRequirement",
    "BriefRevisionCreate",
    "BriefRevisionCreateRequest",
    "BriefRevisionResponse",
    "EpicCreate",
    "EpicCreateRequest",
    "EpicDraftUpdate",
    "EpicDraftUpdateRequest",
    "EpicResponse",
]
