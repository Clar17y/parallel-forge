"""Application service for epic decomposition proposals and atomic adoption."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from forge.application.ports.epic_decomposition import (
    DecompositionAdoptionResult,
    DecompositionAuthoringPort,
    EpicDecompositionUnitOfWork,
)
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_brainstorm import (
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormThread,
    BrainstormTurn,
)
from forge.domain.epic_decomposition import (
    DecompositionConflict,
    DecompositionValidationError,
    require_brief_sources,
    validate_decomposition_proposal,
)
from forge.domain.epic_items import (
    GraphValidationError,
    ItemInput,
    make_snapshot,
    validate_graph,
)
from forge.domain.operation import canonical_digest


class EpicDecompositionService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], EpicDecompositionUnitOfWork],
        *,
        authoring_service: DecompositionAuthoringPort,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authoring = authoring_service

    async def create(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        actor: AuthenticatedActor,
        key: str,
        text: str,
    ) -> tuple[UUID, int]:
        return await self._authoring.create(
            epic_id=epic_id, project_id=project_id, actor=actor, key=key, text=text
        )

    async def turns(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
    ) -> tuple[BrainstormTurn, ...]:
        return await self._authoring.turns(
            epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
        )

    async def threads(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
    ) -> tuple[BrainstormThread, ...]:
        return await self._authoring.threads(epic_id=epic_id, project_id=project_id)

    async def append(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
        expected_version: int,
        actor: AuthenticatedActor,
        key: str,
        text: str,
        pending: bool = False,
    ) -> int:
        return await self._authoring.append(
                epic_id=epic_id,
                project_id=project_id,
                conversation_id=conversation_id,
                expected_version=expected_version,
                actor=actor,
                key=key,
                text=text,
                pending=pending,
        )

    async def submit(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        conversation_id: UUID,
        prompt_turn_id: UUID,
        expected_epic_version: int,
        expected_conversation_version: int,
        actor: AuthenticatedActor,
        key: str,
    ) -> AuthoringReceipt:
        return await self._authoring.submit(
                epic_id=epic_id,
                project_id=project_id,
                conversation_id=conversation_id,
                prompt_turn_id=prompt_turn_id,
                expected_epic_version=expected_epic_version,
                expected_conversation_version=expected_conversation_version,
                actor=actor,
                key=key,
                kind="decomposition",
        )

    async def observe(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
    ) -> AuthoringOutcome:
        return await self._authoring.observe(
            epic_id=epic_id,
            project_id=project_id,
            job_id=job_id,
            kind="decomposition",
        )

    async def cancel(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        expected_job_version: int,
        actor: AuthenticatedActor,
        key: str,
    ) -> AuthoringReceipt:
        await self._authoring.require_kind(
            epic_id=epic_id, project_id=project_id, job_id=job_id,
            kind="decomposition",
        )
        return await self._authoring.cancel(
                epic_id=epic_id,
                project_id=project_id,
                job_id=job_id,
                expected_job_version=expected_job_version,
                actor=actor,
                key=key,
        )

    async def retry(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        expected_job_version: int,
        actor: AuthenticatedActor,
        key: str,
    ) -> AuthoringReceipt:
        await self._authoring.require_kind(
            epic_id=epic_id, project_id=project_id, job_id=job_id,
            kind="decomposition",
        )
        return await self._authoring.retry(
                epic_id=epic_id,
                project_id=project_id,
                job_id=job_id,
                expected_job_version=expected_job_version,
                actor=actor,
                key=key,
        )

    async def adopt(
        self,
        *,
        epic_id: UUID,
        project_id: UUID,
        job_id: UUID,
        proposal_digest: str,
        expected_job_version: int,
        expected_epic_version: int,
        actor: AuthenticatedActor,
        key: str,
        items: list[ItemInput] | None = None,
    ) -> DecompositionAdoptionResult:
        """Atomically adopt a settled decomposition proposal or revised graph revision."""
        request_payload = {
            "schema_version": 1,
            "epic_id": str(epic_id),
            "project_id": str(project_id),
            "job_id": str(job_id),
            "proposal_digest": proposal_digest,
            "expected_job_version": expected_job_version,
            "expected_epic_version": expected_epic_version,
            "items": [item.model_dump(mode="json") for item in items] if items is not None else None,
        }
        request_digest = canonical_digest(request_payload)

        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.decomposition.adopt",
                scope=str(epic_id),
                idempotency_key=key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                if receipt.response_payload is None:
                    raise RuntimeError("replay response payload unavailable")
                return DecompositionAdoptionResult(
                    graph_revision_id=UUID(str(receipt.response_payload["graph_revision_id"])),
                    graph_digest=str(receipt.response_payload["graph_digest"]),
                    epic_version=int(str(receipt.response_payload["epic_version"])),
                    job_version=int(str(receipt.response_payload["job_version"])),
                )

            # Step 1: Lock epic first
            epic = await work.epics.get(epic_id, for_update=True)
            if epic.project_id != project_id:
                raise DecompositionConflict("epic does not belong to specified project")
            if epic.version != expected_epic_version:
                raise DecompositionConflict("epic version is stale")
            if epic.accepted_brief_revision_id is None or epic.accepted_brief_digest is None:
                raise DecompositionConflict("epic has no accepted brief")

            # Step 2: Lock job second
            locked = await work.jobs.lock_job_for_adoption(
                job_id,
                epic_id=epic_id,
                project_id=project_id,
                expected_job_version=expected_job_version,
                proposal_digest=proposal_digest,
            )

            # Step 3: Validate proposal binding
            proposal = locked.proposal
            snapshot = locked.snapshot
            if (
                snapshot.expected_epic_version != epic.version
                or snapshot.input_brief_revision_id != epic.accepted_brief_revision_id
                or snapshot.input_brief_digest != epic.accepted_brief_digest
                or snapshot.input_draft_digest != canonical_digest(epic.draft.model_dump(mode="json"))
                or snapshot.input_graph_revision_id != epic.accepted_graph_revision_id
                or snapshot.input_graph_digest != epic.accepted_graph_digest
            ):
                raise DecompositionConflict("epic changed after proposal input")

            # Step 4: Validate against accepted brief requirements
            brief_revision = await work.epics.get_revision(epic_id, epic.accepted_brief_revision_id)
            if brief_revision.content_digest != epic.accepted_brief_digest:
                raise DecompositionConflict("brief revision digest mismatch")
            validate_decomposition_proposal(proposal, brief_revision.content)

            # Step 5: Determine items to adopt
            items_to_adopt: list[ItemInput]
            if items is not None:
                items_to_adopt = list(items)
                require_brief_sources(items_to_adopt, brief_revision.content)
            else:
                items_to_adopt = list(proposal.items)

            # Step 6: Validate graph for adoption (acyclic, required cannot depend on deferred)
            try:
                validate_graph(items_to_adopt, adoption=True)
            except GraphValidationError as err:
                raise DecompositionValidationError(str(err)) from err

            # Step 7: Create immutable graph revision snapshot
            graph_revision_id = uuid4()
            snapshots, graph_digest = make_snapshot(
                graph_revision_id,
                items_to_adopt,
                epic.accepted_brief_revision_id,
                epic.accepted_brief_digest,
            )

            # Step 8: Save graph revision (increments epic.version)
            saved_revision = await work.epic_items.save_revision(
                epic_id,
                version=epic.version,
                brief_revision_id=epic.accepted_brief_revision_id,
                brief_digest=epic.accepted_brief_digest,
                graph_revision_id=graph_revision_id,
                graph_digest=graph_digest,
                items=snapshots,
            )

            # Step 9: Adopt graph revision (selects graph and increments epic.version)
            updated_epic = await work.epic_items.adopt_revision(
                epic_id,
                version=saved_revision.epic_version,
                revision=saved_revision,
            )

            # Step 10: Mark job adopted
            new_job_version = expected_job_version + 1
            await work.jobs.mark_job_adopted(
                job_id,
                graph_revision_id=graph_revision_id,
                job_version=new_job_version,
            )

            # Step 11: Record operator audit event
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.decomposition.adopt",
                subject_type="epic",
                subject_id=epic_id,
                payload={
                    "job_id": str(job_id),
                    "proposal_digest": proposal_digest,
                    "graph_revision_id": str(graph_revision_id),
                    "graph_digest": graph_digest,
                    "epic_version": updated_epic.version,
                    "job_version": new_job_version,
                },
            )

            # Step 12: Complete mutation receipt
            response_payload = {
                "graph_revision_id": str(graph_revision_id),
                "graph_digest": graph_digest,
                "epic_version": updated_epic.version,
                "job_version": new_job_version,
            }
            await work.mutations.complete(
                receipt.id,
                response_status=200,
                response_payload=response_payload,
                resource_kind="epic",
                resource_id=epic_id,
            )

            await work.commit()

            return DecompositionAdoptionResult(
                graph_revision_id=graph_revision_id,
                graph_digest=graph_digest,
                epic_version=updated_epic.version,
                job_version=new_job_version,
            )


__all__ = ["EpicDecompositionService"]
