"""Transactional epic draft, revision, and accepted-selection commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel

from forge.application.ports.audit import AuditRepository
from forge.application.ports.epic_brief import EpicBriefRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.projects import ProjectRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_brief import (
    AcceptedBrief,
    BriefAdoptionRequest,
    BriefBindingConflict,
    BriefRevisionCreateRequest,
    BriefRevisionRecord,
    EpicCreateRequest,
    EpicDraftUpdateRequest,
    EpicRecord,
    EpicVersionConflict,
)
from forge.domain.operation import canonical_digest


class EpicBriefUnitOfWork(Protocol):
    epics: EpicBriefRepository
    projects: ProjectRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


def _request[T: BaseModel](value: object, model: type[T]) -> T:
    if isinstance(value, model):
        # Pydantic models contain mutable lists; revalidate at the transaction edge.
        return model.model_validate(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("epic request is invalid")


def _replay[T: BaseModel](receipt: ApiMutationRecord, model: type[T]) -> T:
    if receipt.response_payload is None:
        raise RuntimeError("mutation response is unavailable")
    return model.model_validate(receipt.response_payload)


class EpicBriefService:
    def __init__(self, unit_of_work_factory: Callable[[], EpicBriefUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def create(
        self, *, actor: AuthenticatedActor, idempotency_key: str, request: EpicCreateRequest
    ) -> EpicRecord:
        request = _request(request, EpicCreateRequest)
        digest = canonical_digest(request.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.create",
                scope=f"project:{request.project_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                result = _replay(receipt, EpicRecord)
            else:
                await work.projects.get(request.project_id)
                result = await work.epics.create(
                    epic_id=uuid4(),
                    project_id=request.project_id,
                    title=request.title,
                    draft=request.draft,
                )
                await self._finish(
                    work, receipt, actor, "epic.created", result.epic_id, 201, result
                )
            await work.commit()
            return result

    async def update_draft(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: EpicDraftUpdateRequest,
    ) -> EpicRecord:
        request = _request(request, EpicDraftUpdateRequest)
        async with self._unit_of_work_factory() as work:
            receipt = await self._reserve(
                work, actor, "epic.draft.update", epic_id, idempotency_key, request
            )
            if receipt.is_replay:
                result = _replay(receipt, EpicRecord)
            else:
                current = await work.epics.get(epic_id, for_update=True)
                self._version(current, request.expected_epic_version)
                result = await work.epics.update_draft(
                    epic_id, version=current.version, title=request.title, draft=request.draft
                )
                await self._finish(work, receipt, actor, "epic.draft_updated", epic_id, 200, result)
            await work.commit()
            return result

    async def save_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: BriefRevisionCreateRequest,
    ) -> BriefRevisionRecord:
        request = _request(request, BriefRevisionCreateRequest)
        async with self._unit_of_work_factory() as work:
            receipt = await self._reserve(
                work, actor, "epic.brief_revision.save", epic_id, idempotency_key, request
            )
            if receipt.is_replay:
                result = _replay(receipt, BriefRevisionRecord)
            else:
                current = await work.epics.get(epic_id, for_update=True)
                self._version(current, request.expected_epic_version)
                content_digest = canonical_digest(request.content.model_dump(mode="json"))
                result = await work.epics.save_revision(
                    epic_id,
                    version=current.version,
                    content=request.content,
                    content_digest=content_digest,
                )
                await self._finish(
                    work, receipt, actor, "epic.brief_revision_saved", epic_id, 201, result
                )
            await work.commit()
            return result

    async def adopt_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: BriefAdoptionRequest,
    ) -> EpicRecord:
        request = _request(request, BriefAdoptionRequest)
        async with self._unit_of_work_factory() as work:
            receipt = await self._reserve(
                work, actor, "epic.brief.adopt", epic_id, idempotency_key, request
            )
            if receipt.is_replay:
                result = _replay(receipt, EpicRecord)
            else:
                current = await work.epics.get(epic_id, for_update=True)
                self._version(current, request.expected_epic_version)
                revision = await work.epics.get_revision(epic_id, request.brief_revision_id)
                if revision.content_digest != request.brief_digest:
                    raise BriefBindingConflict("brief digest does not match revision")
                revision.content.require_adoptable()
                result = await work.epics.adopt_revision(
                    epic_id, version=current.version, revision=revision
                )
                await self._finish(work, receipt, actor, "epic.brief_adopted", epic_id, 200, result)
            await work.commit()
            return result

    async def list(self, project_id: UUID) -> Sequence[EpicRecord]:
        async with self._unit_of_work_factory() as work:
            await work.projects.get(project_id)
            records = await work.epics.list(project_id)
            await work.commit()
            return records

    async def get(self, epic_id: UUID) -> EpicRecord:
        async with self._unit_of_work_factory() as work:
            record = await work.epics.get(epic_id)
            await work.commit()
            return record

    async def list_revisions(self, epic_id: UUID) -> Sequence[BriefRevisionRecord]:
        async with self._unit_of_work_factory() as work:
            records = await work.epics.list_revisions(epic_id)
            await work.commit()
            return records

    async def get_revision(self, epic_id: UUID, brief_revision_id: UUID) -> BriefRevisionRecord:
        async with self._unit_of_work_factory() as work:
            record = await work.epics.get_revision(epic_id, brief_revision_id)
            await work.commit()
            return record

    async def accepted(self, epic_id: UUID) -> AcceptedBrief:
        async with self._unit_of_work_factory() as work:
            record = await work.epics.accepted(epic_id)
            await work.commit()
            return record

    @staticmethod
    def _version(current: EpicRecord, expected: int) -> None:
        if current.version != expected:
            raise EpicVersionConflict("epic version is stale")

    @staticmethod
    async def _reserve(
        work: EpicBriefUnitOfWork,
        actor: AuthenticatedActor,
        action: str,
        epic_id: UUID,
        key: str,
        request: BaseModel,
    ) -> ApiMutationRecord:
        digest = canonical_digest(
            {"epic_id": str(epic_id), "request": request.model_dump(mode="json")}
        )
        return await work.mutations.reserve(
            actor_id=actor.actor_id,
            action=action,
            scope=f"epic:{epic_id}",
            idempotency_key=key,
            request_digest=digest,
        )

    @staticmethod
    async def _finish(
        work: EpicBriefUnitOfWork,
        receipt: ApiMutationRecord,
        actor: AuthenticatedActor,
        event: str,
        epic_id: UUID,
        status: int,
        result: EpicRecord | BriefRevisionRecord,
    ) -> None:
        evidence: dict[str, object] = {"epic_id": str(epic_id)}
        if isinstance(result, BriefRevisionRecord):
            evidence.update(
                epic_version=result.epic_version,
                brief_revision_id=str(result.brief_revision_id),
                brief_digest=result.content_digest,
            )
            resource_kind = "epic_brief_revision"
            resource_id = result.brief_revision_id
        else:
            evidence["epic_version"] = result.version
            resource_kind = "epic"
            resource_id = epic_id
            if result.accepted_brief_revision_id is not None:
                evidence.update(
                    brief_revision_id=str(result.accepted_brief_revision_id),
                    brief_digest=result.accepted_brief_digest,
                )
        await work.audit.append(
            actor_id=actor.actor_id,
            event_type=event,
            subject_type="epic",
            subject_id=epic_id,
            correlation_id=receipt.id,
            payload=evidence,
        )
        await work.mutations.complete(
            receipt.id,
            response_status=status,
            response_payload=result.model_dump(mode="json"),
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
