"""Transactional graph revision saves and explicit selection."""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel

from forge.application.ports.audit import AuditRepository
from forge.application.ports.epic_brief import EpicBriefRepository
from forge.application.ports.epic_items import EpicItemsRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_brief import (
    BriefBindingConflict,
    BriefNotAccepted,
    EpicRecord,
    EpicVersionConflict,
)
from forge.domain.epic_items import (
    AcceptedGraph,
    GraphAdoptionRequest,
    GraphBindingConflict,
    GraphNotAccepted,
    GraphRevisionCreateRequest,
    GraphRevisionRecord,
    GraphValidationError,
    ItemInput,
    make_snapshot,
    validate_graph,
)
from forge.domain.operation import canonical_digest


class EpicItemsUnitOfWork(Protocol):
    epics: EpicBriefRepository
    epic_items: EpicItemsRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


def _request[T: BaseModel](value: object, model: type[T]) -> T:
    if isinstance(value, model):
        return model.model_validate(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("graph request is invalid")


def _replay[T: BaseModel](receipt: ApiMutationRecord, model: type[T]) -> T:
    if receipt.response_payload is None:
        raise RuntimeError("mutation response is unavailable")
    return model.model_validate(receipt.response_payload)


class EpicItemsService:
    def __init__(self, unit_of_work_factory: Callable[[], EpicItemsUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def save_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: GraphRevisionCreateRequest,
    ) -> GraphRevisionRecord:
        request = _request(request, GraphRevisionCreateRequest)
        async with self._unit_of_work_factory() as work:
            receipt = await self._reserve(
                work, actor, "epic.graph_revision.save", epic_id, idempotency_key, request
            )
            if receipt.is_replay:
                result = _replay(receipt, GraphRevisionRecord)
            else:
                epic = await work.epics.get(epic_id, for_update=True)
                self._version(epic, request.expected_epic_version)
                self._brief(epic, request.brief_revision_id, request.brief_digest)
                brief = await work.epics.get_revision(epic_id, request.brief_revision_id)
                if brief.content_digest != request.brief_digest:
                    raise BriefBindingConflict("brief digest does not match revision")
                requirements = {value.requirement_id for value in brief.content.requirements}
                if any(
                    ref not in requirements
                    for item in request.items
                    for ref in item.source_requirement_ids
                ):
                    raise GraphValidationError("source requirement is missing from accepted brief")
                graph_revision_id = uuid4()
                snapshots, digest = make_snapshot(
                    graph_revision_id,
                    request.items,
                    request.brief_revision_id,
                    request.brief_digest,
                )
                result = await work.epic_items.save_revision(
                    epic_id,
                    version=epic.version,
                    brief_revision_id=request.brief_revision_id,
                    brief_digest=request.brief_digest,
                    graph_revision_id=graph_revision_id,
                    graph_digest=digest,
                    items=snapshots,
                )
                await self._finish(
                    work, receipt, actor, "epic.graph_revision_saved", epic_id, 201, result
                )
            await work.commit()
            return result

    async def adopt_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: GraphAdoptionRequest,
    ) -> EpicRecord:
        request = _request(request, GraphAdoptionRequest)
        async with self._unit_of_work_factory() as work:
            receipt = await self._reserve(
                work, actor, "epic.graph.adopt", epic_id, idempotency_key, request
            )
            if receipt.is_replay:
                result = _replay(receipt, EpicRecord)
            else:
                epic = await work.epics.get(epic_id, for_update=True)
                self._version(epic, request.expected_epic_version)
                revision = await work.epic_items.get_revision(epic_id, request.graph_revision_id)
                if revision.graph_digest != request.graph_digest:
                    raise GraphBindingConflict("graph digest does not match revision")
                self._brief(epic, revision.brief_revision_id, revision.brief_digest)
                self._validate_revision(revision)
                result = await work.epic_items.adopt_revision(
                    epic_id, version=epic.version, revision=revision
                )
                await self._finish(work, receipt, actor, "epic.graph_adopted", epic_id, 200, result)
            await work.commit()
            return result

    async def list_revisions(self, epic_id: UUID) -> Sequence[GraphRevisionRecord]:
        async with self._unit_of_work_factory() as work:
            records = await work.epic_items.list_revisions(epic_id)
            await work.commit()
            return records

    async def get_revision(self, epic_id: UUID, graph_revision_id: UUID) -> GraphRevisionRecord:
        async with self._unit_of_work_factory() as work:
            record = await work.epic_items.get_revision(epic_id, graph_revision_id)
            await work.commit()
            return record

    async def accepted(self, epic_id: UUID) -> AcceptedGraph:
        async with self._unit_of_work_factory() as work:
            epic = await work.epics.get(epic_id)
            if epic.accepted_graph_revision_id is None or epic.accepted_graph_digest is None:
                raise GraphNotAccepted("graph is not accepted")
            revision = await work.epic_items.get_revision(epic_id, epic.accepted_graph_revision_id)
            if revision.graph_digest != epic.accepted_graph_digest:
                raise GraphBindingConflict("accepted graph digest does not match revision")
            self._brief(epic, revision.brief_revision_id, revision.brief_digest)
            self._validate_revision(revision)
            result = AcceptedGraph(
                epic_id=epic_id,
                brief_revision_id=revision.brief_revision_id,
                brief_digest=revision.brief_digest,
                graph_revision_id=revision.graph_revision_id,
                graph_digest=revision.graph_digest,
                items=revision.items,
            )
            await work.commit()
            return result

    @staticmethod
    def _version(epic: EpicRecord, expected: int) -> None:
        if epic.version != expected:
            raise EpicVersionConflict("epic version is stale")

    @staticmethod
    def _brief(epic: EpicRecord, brief_revision_id: UUID, brief_digest: str) -> None:
        if epic.accepted_brief_revision_id is None or epic.accepted_brief_digest is None:
            raise BriefNotAccepted("brief is not accepted")
        if (epic.accepted_brief_revision_id, epic.accepted_brief_digest) != (
            brief_revision_id,
            brief_digest,
        ):
            raise BriefBindingConflict("graph brief binding does not match accepted brief")

    @staticmethod
    def _validate_revision(revision: GraphRevisionRecord) -> None:
        items = [
            ItemInput.model_validate(
                item.model_dump(mode="python", exclude={"graph_revision_id", "item_digest"})
            )
            for item in revision.items
        ]
        snapshots, digest = make_snapshot(
            revision.graph_revision_id, items, revision.brief_revision_id, revision.brief_digest
        )
        if digest != revision.graph_digest or snapshots != revision.items:
            raise GraphBindingConflict("graph snapshot digest does not match")
        validate_graph(items, adoption=True)

    @staticmethod
    async def _reserve(
        work: EpicItemsUnitOfWork,
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
        work: EpicItemsUnitOfWork,
        receipt: ApiMutationRecord,
        actor: AuthenticatedActor,
        event: str,
        epic_id: UUID,
        status: int,
        result: EpicRecord | GraphRevisionRecord,
    ) -> None:
        evidence: dict[str, object] = {"epic_id": str(epic_id)}
        if isinstance(result, GraphRevisionRecord):
            evidence.update(
                epic_version=result.epic_version,
                graph_revision_id=str(result.graph_revision_id),
                graph_digest=result.graph_digest,
                brief_revision_id=str(result.brief_revision_id),
                brief_digest=result.brief_digest,
            )
            resource_id = result.graph_revision_id
            resource_kind = "epic_graph_revision"
        else:
            evidence.update(
                epic_version=result.version,
                graph_revision_id=str(result.accepted_graph_revision_id),
                graph_digest=result.accepted_graph_digest,
            )
            resource_id = epic_id
            resource_kind = "epic"
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
