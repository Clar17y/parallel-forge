"""Authenticated epic discovery commands; each command commits before worker IO."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.epic_brainstorm import BrainstormBriefPort, BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormConflict,
    BrainstormProposal,
    BrainstormThread,
    BrainstormTurn,
    FrozenBriefContent,
    validate_invocation_context,
)
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    RouteBinding,
    SpecialistPurpose,
    TaskBudget,
)
from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormConversation,
    BrainstormJobRow,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository


@dataclass(frozen=True, slots=True)
class BrainstormConfiguredRoute:
    binding: RouteBinding
    budget: TaskBudget
    profile_id: UUID | None
    profile_version: int | None


async def project_brainstorm_route(
    session: AsyncSession, project_id: UUID, budget: TaskBudget
) -> BrainstormConfiguredRoute:
    """Freeze the project's configured exploration route, without fallbacks."""
    from forge.persistence.repositories.subscription import PostgresSubscriptionRepository

    profile = await PostgresSubscriptionRepository(session).project_profile(project_id)
    if profile is None:
        raise BrainstormConflict("project has no configured authoring profile")
    try:
        preferred = profile.preference_for(SpecialistPurpose.EXPLORATION).preferred_route
    except KeyError:
        raise BrainstormConflict("project has no configured exploration route") from None
    if preferred.auth_mode is AuthMode.API_KEY or preferred.billing_mode is BillingMode.PAID_OPT_IN:
        raise BrainstormConflict("brainstorm route requires configured local allowance")
    return BrainstormConfiguredRoute(
        RouteBinding(requested=preferred, effective=preferred),
        budget,
        profile.profile_id,
        profile.version,
    )


class EpicBrainstormService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        briefs: Callable[[AsyncSession], BrainstormBriefPort],
        *,
        route: RouteBinding | None = None,
        budget: TaskBudget,
        route_selector: Callable[
            [AsyncSession, UUID, TaskBudget], Awaitable[BrainstormConfiguredRoute]
        ]
        | None = None,
    ) -> None:
        if budget.max_provider_attempts < 1 or (
            route is not None and route.requested != route.effective
        ):
            raise ValueError("brainstorm requires one configured route and positive attempt budget")
        if route is None and route_selector is None:
            route_selector = project_brainstorm_route
        self.sessions, self.briefs, self.route, self.budget, self.route_selector = (
            sessions,
            briefs,
            route,
            budget,
            route_selector,
        )

    async def create(
        self, *, epic_id: UUID, project_id: UUID, actor: AuthenticatedActor, key: str, text: str
    ) -> tuple[UUID, int]:
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "create",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "text": text,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return UUID(str(replay["conversation_id"])), int(str(replay["version"]))
            source = await self.briefs(session).input(epic_id)
            if source.project_id != project_id:
                raise BrainstormConflict("epic project binding conflicts")
            conversation_id = uuid4()
            row = BrainstormConversation(
                id=conversation_id, epic_id=epic_id, project_id=project_id, version=1
            )
            session.add(row)
            await session.flush()
            await repository.append(
                row, BrainstormTurn(conversation_id=conversation_id, role="operator", text=text)
            )
            response = {"conversation_id": str(conversation_id), "version": row.version}
            await repository.save_receipt(epic_id, key, digest, response)
            await repository.audit(
                epic_id,
                actor.actor_id,
                "conversation_created",
                conversation_id,
                {"version": row.version},
            )
            return conversation_id, row.version

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
        turn = BrainstormTurn(
            conversation_id=conversation_id, role="operator", text=text, pending=pending
        )
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "append",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "conversation_id": str(conversation_id),
                "expected_version": expected_version,
                "text": text,
                "pending": pending,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return int(str(replay["version"]))
            row = await repository.conversation(epic_id, project_id, conversation_id, lock=True)
            if row.version != expected_version:
                raise BrainstormConflict("conversation version is stale")
            version = await repository.append(row, turn)
            await repository.save_receipt(epic_id, key, digest, {"version": version})
            await repository.audit(
                epic_id,
                actor.actor_id,
                "turn_appended",
                turn.turn_id,
                {"conversation_id": str(conversation_id), "version": version},
            )
            return version

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
        expected_snapshot: AuthoringJobSnapshot | None = None,
    ) -> AuthoringReceipt:
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "submit",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "conversation_id": str(conversation_id),
                "prompt_turn_id": str(prompt_turn_id),
                "expected_epic_version": expected_epic_version,
                "expected_conversation_version": expected_conversation_version,
                "expected_snapshot": expected_snapshot.model_dump(mode="json")
                if expected_snapshot
                else None,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return AuthoringReceipt.model_validate(replay)
            conversation = await repository.conversation(
                epic_id, project_id, conversation_id, lock=True
            )
            brief = await self.briefs(session).input(epic_id)
            if (
                brief.project_id != project_id
                or brief.epic_version != expected_epic_version
                or conversation.version != expected_conversation_version
            ):
                raise BrainstormConflict("brainstorm input version is stale")
            turn = await repository.turn(conversation_id, prompt_turn_id)
            if turn.role != "operator":
                raise BrainstormConflict("prompt must be an operator turn")
            history = await repository.turns(conversation_id)
            if not history or history[-1].turn_id != prompt_turn_id:
                raise BrainstormConflict("prompt must be the latest conversation turn")
            selection = (
                await self.route_selector(session, project_id, self.budget)
                if self.route_selector
                else BrainstormConfiguredRoute(
                    cast(RouteBinding, self.route), self.budget, None, None
                )
            )
            job_id = expected_snapshot.job_id if expected_snapshot else uuid4()
            snapshot = AuthoringJobSnapshot(
                job_id=job_id,
                epic_id=epic_id,
                project_id=project_id,
                conversation_id=conversation_id,
                input_brief_revision_id=brief.accepted_revision_id,
                input_brief_digest=brief.accepted_digest,
                input_draft_digest=brief.draft_digest,
                draft_content=brief.draft_content,
                accepted_content=brief.accepted_content,
                input_graph_revision_id=brief.accepted_graph_revision_id,
                input_graph_digest=brief.accepted_graph_digest,
                expected_epic_version=expected_epic_version,
                conversation_version=conversation.version,
                prompt_turn_id=prompt_turn_id,
                profile_id=selection.profile_id,
                profile_version=selection.profile_version,
                route=selection.binding,
                budget=selection.budget,
                reservation_id=expected_snapshot.reservation_id if expected_snapshot else uuid4(),
            )
            if expected_snapshot is not None and snapshot != expected_snapshot:
                raise BrainstormConflict("authoring job snapshot conflicts with configured input")
            validate_invocation_context(snapshot, history)
            row = BrainstormJobRow(
                id=job_id,
                epic_id=epic_id,
                project_id=project_id,
                conversation_id=conversation_id,
                version=1,
                state="queued",
                snapshot=repository.snapshot_payload(snapshot),
            )
            session.add(row)
            await session.flush()
            receipt = repository.receipt(row, key)
            await repository.save_receipt(epic_id, key, digest, receipt.model_dump(mode="json"))
            await repository.audit(
                epic_id,
                actor.actor_id,
                "job_submitted",
                job_id,
                {
                    "input_epic_version": expected_epic_version,
                    "conversation_version": conversation.version,
                },
            )
            return receipt

    async def observe(self, *, epic_id: UUID, project_id: UUID, job_id: UUID) -> AuthoringOutcome:
        async with self.sessions() as session:
            repository = PostgresBrainstormRepository(session)
            row = await repository.job(epic_id, project_id, job_id)
            return await repository.outcome(row)

    async def turns(
        self, *, epic_id: UUID, project_id: UUID, conversation_id: UUID
    ) -> tuple[BrainstormTurn, ...]:
        async with self.sessions() as session:
            repository = PostgresBrainstormRepository(session)
            await repository.conversation(epic_id, project_id, conversation_id)
            return await repository.turns(conversation_id)

    async def threads(self, *, epic_id: UUID, project_id: UUID) -> tuple[BrainstormThread, ...]:
        async with self.sessions() as session:
            return await PostgresBrainstormRepository(session).threads(epic_id, project_id)

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
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "cancel",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "job_id": str(job_id),
                "expected_job_version": expected_job_version,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return AuthoringReceipt.model_validate(replay)
            row = await repository.job(epic_id, project_id, job_id, lock=True)
            if row.version != expected_job_version:
                raise BrainstormConflict("job version is stale")
            if row.state in ("queued", "quota_wait", "capacity_wait"):
                row.state = "cancelled"
            elif row.state in ("running", "reconciling"):
                row.state = "cancel_requested"
                row.next_eligible_at = None
            else:
                raise BrainstormConflict("job is already terminal")
            row.version += 1
            await session.flush()
            receipt = repository.receipt(row, key)
            await repository.save_receipt(epic_id, key, digest, receipt.model_dump(mode="json"))
            await repository.audit(
                epic_id, actor.actor_id, "job_cancelled", job_id, {"state": row.state}
            )
            return receipt

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
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "retry",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "job_id": str(job_id),
                "expected_job_version": expected_job_version,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return AuthoringReceipt.model_validate(replay)
            row = await repository.job(epic_id, project_id, job_id, lock=True)
            if (
                row.version != expected_job_version
                or row.state != "failed"
                or row.current_attempt_id is None
            ):
                raise BrainstormConflict("job retry is not eligible")
            previous = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            snapshot = repository.decode_snapshot(row)
            if (
                previous is None
                or not previous.process_settled
                or previous.number >= snapshot.budget.max_provider_attempts
            ):
                raise BrainstormConflict("attempt settlement or retry budget is unavailable")
            row.state, row.failure, row.next_eligible_at = "queued", None, None
            row.version += 1
            await session.flush()
            receipt = repository.receipt(row, key)
            await repository.save_receipt(epic_id, key, digest, receipt.model_dump(mode="json"))
            await repository.audit(
                epic_id, actor.actor_id, "job_retried", job_id, {"from_attempt": str(previous.id)}
            )
            return receipt

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
    ) -> UUID:
        digest = canonical_digest(
            {
                "schema_version": 1,
                "action": "adopt",
                "epic_id": str(epic_id),
                "project_id": str(project_id),
                "actor_id": str(actor.actor_id),
                "job_id": str(job_id),
                "proposal_digest": proposal_digest,
                "expected_job_version": expected_job_version,
                "expected_epic_version": expected_epic_version,
            }
        )
        async with self.sessions() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            await repository.lock_command(epic_id, key)
            replay = await repository.replay(epic_id, key, digest)
            if replay:
                return UUID(str(replay["brief_revision_id"]))
            # Brief adapter must acquire the producer epic lock before the job row.
            brief = await self.briefs(session).input(epic_id, for_update=True)
            row = await repository.job(epic_id, project_id, job_id, lock=True)
            snapshot = repository.decode_snapshot(row)
            if (
                brief.project_id != project_id
                or brief.epic_version != expected_epic_version
                or brief.epic_version != snapshot.expected_epic_version
                or brief.accepted_revision_id != snapshot.input_brief_revision_id
                or brief.accepted_digest != snapshot.input_brief_digest
                or brief.draft_digest != snapshot.input_draft_digest
                or brief.accepted_graph_revision_id != snapshot.input_graph_revision_id
                or brief.accepted_graph_digest != snapshot.input_graph_digest
            ):
                raise BrainstormConflict("brief changed after proposal input")
            conversation = await repository.conversation(
                epic_id, project_id, row.conversation_id, lock=True
            )
            if (
                row.version != expected_job_version
                or row.state != "proposed"
                or row.proposal_digest != proposal_digest
                or row.proposal is None
            ):
                raise BrainstormConflict("proposal identity or version is stale")
            proposal = BrainstormProposal.model_validate(row.proposal)
            if proposal.digest != proposal_digest:
                raise BrainstormConflict("proposal digest conflicts")
            history = await repository.turns(row.conversation_id)
            if (
                conversation.version != len(history) + 1
                or snapshot.conversation_version < 2
                or len(history) < snapshot.conversation_version
                or history[snapshot.conversation_version - 2].turn_id != snapshot.prompt_turn_id
                or history[snapshot.conversation_version - 2].role != "operator"
                or any(
                    turn.role != "assistant" or turn.pending
                    for turn in history[snapshot.conversation_version - 1 :]
                )
                or sum(
                    turn.turn_id == proposal.turn_id
                    and turn.text == proposal.problem
                    and turn.proposal == proposal
                    for turn in history[snapshot.conversation_version - 1 :]
                )
                != 1
            ):
                raise BrainstormConflict("conversation changed after proposal input")
            revision_id = await self.briefs(session).save_proposal_revision(
                epic_id,
                expected_version=expected_epic_version,
                source_job_id=job_id,
                proposal=proposal,
            )
            row.adopted_revision_id = revision_id
            row.version += 1
            await session.flush()
            await repository.save_receipt(
                epic_id, key, digest, {"brief_revision_id": str(revision_id)}
            )
            await repository.audit(
                epic_id,
                actor.actor_id,
                "proposal_adopted",
                job_id,
                {"proposal_digest": proposal_digest, "brief_revision_id": str(revision_id)},
            )
            return revision_id


class BoundEpicAuthoringAdapter:
    """Frozen authoring port bound to one authenticated epic and project."""

    def __init__(
        self,
        service: EpicBrainstormService,
        *,
        epic_id: UUID,
        project_id: UUID,
        actor: AuthenticatedActor,
        idempotency_key: str,
    ) -> None:
        self.service = service
        self.epic_id = epic_id
        self.project_id = project_id
        self.actor = actor
        self.idempotency_key = idempotency_key

    async def submit(self, job: AuthoringJobSnapshot) -> AuthoringReceipt:
        if (
            job.kind != "brainstorm"
            or job.epic_id != self.epic_id
            or job.project_id != self.project_id
        ):
            raise BrainstormConflict("authoring subject binding conflicts")
        return await self.service.submit(
            epic_id=self.epic_id,
            project_id=self.project_id,
            conversation_id=job.conversation_id,
            prompt_turn_id=job.prompt_turn_id,
            expected_epic_version=job.expected_epic_version,
            expected_conversation_version=job.conversation_version,
            actor=self.actor,
            key=canonical_digest(
                {"schema_version": 1, "namespace": self.idempotency_key, "job_id": str(job.job_id)}
            ),
            expected_snapshot=job,
        )

    async def observe(self, job_id: UUID) -> AuthoringOutcome:
        return await self.service.observe(
            epic_id=self.epic_id, project_id=self.project_id, job_id=job_id
        )


class EpicBriefBrainstormAdapter:
    """#69 producer adapter; activated only after its modules and source hook land."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def input(self, epic_id: UUID, *, for_update: bool = False) -> BriefInput:
        PostgresEpicBriefRepository = import_module(
            "forge.persistence.repositories.epic_brief"
        ).PostgresEpicBriefRepository
        repository = PostgresEpicBriefRepository(self.session)
        epic = await repository.get(epic_id, for_update=for_update)
        accepted = await repository.accepted(epic_id) if epic.accepted_brief_revision_id else None
        draft_payload = epic.draft.model_dump(mode="json")
        return BriefInput(
            epic_id=epic.epic_id,
            project_id=epic.project_id,
            epic_version=epic.version,
            draft_digest=canonical_digest(draft_payload),
            accepted_revision_id=epic.accepted_brief_revision_id,
            accepted_digest=epic.accepted_brief_digest,
            draft_content=FrozenBriefContent.model_validate(draft_payload),
            accepted_content=FrozenBriefContent.model_validate(
                accepted.model_dump(
                    mode="json",
                    exclude={
                        "epic_id",
                        "project_id",
                        "epic_version",
                        "brief_revision_id",
                        "brief_digest",
                    },
                )
            )
            if accepted
            else None,
            accepted_graph_revision_id=epic.accepted_graph_revision_id,
            accepted_graph_digest=epic.accepted_graph_digest,
        )

    async def save_proposal_revision(
        self,
        epic_id: UUID,
        *,
        expected_version: int,
        source_job_id: UUID,
        proposal: BrainstormProposal,
    ) -> UUID:
        producer = import_module("forge.domain.epic_brief")
        BriefContent, BriefRequirement = producer.BriefContent, producer.BriefRequirement
        PostgresEpicBriefRepository = import_module(
            "forge.persistence.repositories.epic_brief"
        ).PostgresEpicBriefRepository

        repository = PostgresEpicBriefRepository(self.session)
        epic = await repository.get(epic_id, for_update=True)
        if epic.version != expected_version:
            raise BrainstormConflict("brief draft changed before adoption")
        draft = epic.draft
        existing_texts = {item.text for item in draft.requirements}
        requirements = [
            *draft.requirements,
            *(
                BriefRequirement(
                    requirement_id=uuid4(),
                    text=text,
                    acceptance_criteria=list(proposal.requirement_criteria.get(text, ())),
                )
                for text in proposal.requirements
                if text not in existing_texts
            ),
        ]

        def unique(*groups: tuple[str, ...] | list[str]) -> list[str]:
            return list(dict.fromkeys(item for group in groups for item in group))

        try:
            content = BriefContent(
                problem=proposal.problem,
                outcomes=unique(draft.outcomes, proposal.outcomes),
                scope=unique(draft.scope, proposal.scope),
                exclusions=unique(draft.exclusions, proposal.exclusions),
                requirements=requirements,
                decisions=unique(draft.decisions, proposal.decisions),
                assumptions=unique(draft.assumptions, proposal.assumptions),
                open_questions=unique(draft.open_questions, proposal.open_questions),
            )
        except ValidationError, ValueError:
            raise BrainstormConflict("proposal exceeds accepted brief limits") from None
        revision = await repository.save_revision(
            epic_id,
            version=expected_version,
            content=content,
            content_digest=canonical_digest(content.model_dump(mode="json")),
            source_job_id=source_job_id,
        )
        return cast(UUID, revision.brief_revision_id)
