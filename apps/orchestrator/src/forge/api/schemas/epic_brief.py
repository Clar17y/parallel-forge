"""Epic brief HTTP request and response schemas."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict

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

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    accepted_brief_revision_id: UUID | None = None
    accepted_brief_digest: str | None = None
    accepted_graph_revision_id: UUID | None = None
    accepted_graph_digest: str | None = None

    @classmethod
    def from_record(cls, record: EpicRecord) -> EpicResponse:
        return cls(
            schema_version=record.schema_version,
            epic_id=record.epic_id,
            project_id=record.project_id,
            version=record.version,
            title=record.title,
            draft=record.draft,
            accepted_brief_revision_id=record.accepted_brief_revision_id,
            accepted_brief_digest=record.accepted_brief_digest,
            accepted_graph_revision_id=record.accepted_graph_revision_id,
            accepted_graph_digest=record.accepted_graph_digest,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class BriefRevisionResponse(BriefRevisionRecord):
    """Immutable brief revision snapshot with explicit nullable keys."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_job_id: UUID | None = None

    @classmethod
    def from_record(cls, record: BriefRevisionRecord) -> BriefRevisionResponse:
        return cls(
            schema_version=record.schema_version,
            brief_revision_id=record.brief_revision_id,
            epic_id=record.epic_id,
            revision_number=record.revision_number,
            epic_version=record.epic_version,
            content_digest=record.content_digest,
            source_job_id=record.source_job_id,
            content=record.content,
            created_at=record.created_at,
        )


class AcceptedBriefResponse(AcceptedBrief):
    """Immutable accepted brief projection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1

    @classmethod
    def from_record(cls, record: AcceptedBrief) -> AcceptedBriefResponse:
        return cls(
            schema_version=record.schema_version,
            problem=record.problem,
            outcomes=record.outcomes,
            scope=record.scope,
            exclusions=record.exclusions,
            requirements=record.requirements,
            decisions=record.decisions,
            assumptions=record.assumptions,
            open_questions=record.open_questions,
            epic_id=record.epic_id,
            project_id=record.project_id,
            epic_version=record.epic_version,
            brief_revision_id=record.brief_revision_id,
            brief_digest=record.brief_digest,
        )


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
