"""Real authoring, settlement, and atomic adoption through PostgreSQL."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import (
    EpicBrainstormService,
    EpicBriefBrainstormAdapter,
)
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.domain.epic_brainstorm import BrainstormConflict, BrainstormNotFound
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.domain.epic_decomposition import (
    DecompositionConflict,
    DecompositionValidationError,
)
from forge.domain.epic_items import ItemInput, make_snapshot
from forge.domain.operation import canonical_digest
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.models.epic_brief import Epic, EpicBriefRevision
from forge.persistence.models.epic_items import EpicGraphRevision
from forge.persistence.models.project import Project
from forge.persistence.repositories.epic_brief import PostgresEpicBriefRepository
from forge.persistence.repositories.epic_decomposition import PostgresEpicDecompositionUnitOfWork
from forge.persistence.repositories.epic_items import PostgresEpicItemsRepository
from forge.worker.epic_brainstorm import EpicBrainstormWorker
from forge.worker.epic_decomposition import ValidatedDecompositionGateway
from sqlalchemy import select

from .support import SupervisedGateway


async def prepared(factory):
    project_id, epic_id, brief_id, requirement_id = (uuid4() for _ in range(4))
    content = BriefContent(
        problem="Test problem", outcomes=["Outcome"], scope=["Scope"], exclusions=["Excluded"],
        requirements=[BriefRequirement(requirement_id=requirement_id, text="Deliver work", acceptance_criteria=["Done"])],
        assumptions=["Keep assumption"], open_questions=["Open choice?"],
    )
    content.require_adoptable()
    digest = canonical_digest(content.model_dump(mode="json"))
    async with factory() as session, session.begin():
        session.add(Project(
            id=project_id, canonical_path=f"/tmp/forge-{project_id}",
            github_repository=f"example/repo-{project_id}", default_branch="main",
        ))
        await session.flush()
        session.add(Epic(
            id=epic_id, project_id=project_id, title="Epic", version=1,
            draft=content.model_dump(mode="json"), accepted_brief_revision_id=brief_id,
            accepted_brief_digest=digest,
        ))
        session.add(EpicBriefRevision(
            id=brief_id, epic_id=epic_id, revision_number=1, epic_version=1,
            content=content.model_dump(mode="json"), content_digest=digest,
        ))
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    authoring = EpicBrainstormService(
        factory, EpicBriefBrainstormAdapter,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    service = EpicDecompositionService(
        lambda: PostgresEpicDecompositionUnitOfWork(factory), authoring_service=authoring,
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    return service, actor, epic_id, project_id


async def submit(service, actor, epic_id, project_id, *, key="submit"):
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key=f"create-{key}",
        text="Decompose the accepted brief",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    receipt = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[-1].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key=key,
    )
    return conversation_id, version, turns[-1].turn_id, receipt


async def settle(factory, job_id: UUID, gateway=None):
    worker = EpicBrainstormWorker(
        factory, owner=f"decomposition-test-{uuid4().hex}",
        gateway_factory=lambda _job: ValidatedDecompositionGateway(gateway or SupervisedGateway()),
        reader_factory=lambda _job: AsyncMock(), kinds=frozenset(("decomposition",)),
    )
    assert await worker.run_once() == job_id
    await worker.drain()


@pytest.mark.asyncio
async def test_submit_settle_edit_adopt_and_replay(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, version, prompt_id, receipt = await submit(
        service, actor, epic_id, project_id
    )
    async with factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row.snapshot["kind"] == "decomposition"
        assert row.snapshot["expected_epic_version"] == 1
        assert row.snapshot["input_brief_revision_id"] is not None
        assert row.snapshot["accepted_content"]["requirements"]
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "proposed" and outcome.process_settled
    assert outcome.proposal is not None
    item = outcome.proposal.items[0]
    changed = item.model_copy(update={"title": "Operator combined item", "disposition": "deferred"})
    result = await service.adopt(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
        expected_epic_version=1, actor=actor, key="adopt", items=[changed],
    )
    replay = await service.adopt(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
        expected_epic_version=1, actor=actor, key="adopt", items=[changed],
    )
    assert replay == result
    async with factory() as session:
        epic = await session.get(Epic, epic_id)
        row = await session.get(BrainstormJobRow, receipt.job_id)
        graph = await session.get(EpicGraphRevision, result.graph_revision_id)
        assert epic.version == 3 and epic.accepted_graph_revision_id == result.graph_revision_id
        assert row.state == "proposed" and row.adopted_revision_id == result.graph_revision_id
        assert graph.content["items"][0]["title"] == "Operator combined item"
        assert graph.content["items"][0]["disposition"] == "deferred"
    # Shared receipt replay happens before current source checks.
    replay_submit = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=prompt_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="submit",
    )
    assert replay_submit == receipt
    with pytest.raises(BrainstormConflict):
        await service.submit(
            epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
            prompt_turn_id=prompt_id, expected_epic_version=3,
            expected_conversation_version=version, actor=actor, key="submit",
        )


@pytest.mark.asyncio
async def test_submit_requires_accepted_brief_and_project(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor,
        key="create-missing", text="Decompose",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    with pytest.raises(BrainstormNotFound):
        await service.submit(
            epic_id=epic_id, project_id=uuid4(), conversation_id=conversation_id,
            prompt_turn_id=turns[-1].turn_id, expected_epic_version=1,
            expected_conversation_version=version, actor=actor, key="wrong-project",
        )
    async with factory() as session, session.begin():
        epic = await session.get(Epic, epic_id)
        epic.accepted_brief_revision_id = None
        epic.accepted_brief_digest = None
        epic.version += 1
    with pytest.raises(BrainstormConflict, match="requires an accepted brief"):
        await service.submit(
            epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
            prompt_turn_id=turns[-1].turn_id, expected_epic_version=2,
            expected_conversation_version=version, actor=actor, key="missing-accepted",
        )


@pytest.mark.asyncio
async def test_refreshed_version_cannot_adopt_old_proposal(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    async with factory() as session, session.begin():
        epic = await session.get(Epic, epic_id)
        epic.version += 1
    with pytest.raises(DecompositionConflict, match="changed after proposal input"):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=2, actor=actor, key="stale-adopt",
        )
    async with factory() as session:
        epic = await session.get(Epic, epic_id)
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert epic.version == 2 and epic.accepted_graph_revision_id is None
        assert row.state == "proposed" and row.adopted_revision_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["draft", "brief_id", "brief_digest", "graph"])
async def test_source_edit_keeps_proposal_inspectable(decomposition_session_factory, change) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    async with factory() as session, session.begin():
        epic = await session.get(Epic, epic_id)
        if change == "draft":
            epic.version += 1
            epic.draft = {**epic.draft, "problem": "Edited by operator"}
        elif change in ("brief_id", "brief_digest"):
            brief = BriefContent.model_validate(epic.draft)
            if change == "brief_digest":
                brief = brief.model_copy(update={"problem": "Edited brief"})
            repository = PostgresEpicBriefRepository(session)
            revision = await repository.save_revision(
                epic_id, version=1, content=brief,
                content_digest=canonical_digest(brief.model_dump(mode="json")),
            )
            await repository.adopt_revision(epic_id, version=2, revision=revision)
        else:
            item = ItemInput(
                item_id=uuid4(), disposition="required", ordinal=0, title="Manual item",
                outcome="Manual outcome", acceptance_criteria=["Done"],
                source_requirement_ids=[uuid4()], dependency_item_ids=[],
            )
            revision_id = uuid4()
            snapshots, graph_digest = make_snapshot(
                revision_id, [item], epic.accepted_brief_revision_id, epic.accepted_brief_digest,
            )
            repository = PostgresEpicItemsRepository(session)
            revision = await repository.save_revision(
                epic_id, version=1, brief_revision_id=epic.accepted_brief_revision_id,
                brief_digest=epic.accepted_brief_digest, graph_revision_id=revision_id,
                graph_digest=graph_digest, items=snapshots,
            )
            await repository.adopt_revision(epic_id, version=2, revision=revision)
    current_version = 2 if change == "draft" else 3
    with pytest.raises(DecompositionConflict, match="changed after proposal input"):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=current_version, actor=actor, key=f"stale-{change}",
        )
    observed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert observed.state == "proposed" and observed.proposal == outcome.proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["kind", "job_epic", "proposal_project", "payload_digest", "unsettled", "cancelled"])
async def test_wrong_job_or_unsettled_attempt_cannot_adopt(decomposition_session_factory, change) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    async with factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, receipt.job_id)
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        if change == "kind":
            row.snapshot = {**row.snapshot, "kind": "brainstorm"}
        elif change == "job_epic":
            row.snapshot = {**row.snapshot, "epic_id": str(uuid4())}
        elif change == "proposal_project":
            row.proposal = {**row.proposal, "project_id": str(uuid4())}
        elif change == "payload_digest":
            row.proposal = {**row.proposal, "summary": "Tampered after settlement"}
        elif change == "unsettled":
            attempt.process_settled = False
        else:
            row.state = "cancelled"
    with pytest.raises(DecompositionConflict):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key=f"bad-{change}",
        )
    async with factory() as session:
        assert (await session.get(Epic, epic_id)).accepted_graph_revision_id is None
        assert (await session.get(BrainstormJobRow, receipt.job_id)).adopted_revision_id is None


@pytest.mark.asyncio
async def test_pending_operator_turn_after_publication_blocks_adoption(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    turns = await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    await service.append(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        expected_version=len(turns) + 1, actor=actor, key="new-question",
        text="Should this be split further?", pending=True,
    )
    with pytest.raises(DecompositionConflict, match="conversation changed"):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key="after-question",
        )


@pytest.mark.asyncio
async def test_two_settled_jobs_race_to_adopt(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, version, prompt_id, first = await submit(service, actor, epic_id, project_id)
    second = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=prompt_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="submit-second",
    )
    await settle(factory, first.job_id)
    await settle(factory, second.job_id)
    first_outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    second_outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert first_outcome.state == second_outcome.state == "proposed"
    await service.adopt(
        epic_id=epic_id, project_id=project_id, job_id=first.job_id,
        proposal_digest=first_outcome.proposal_digest,
        expected_job_version=first_outcome.job_version, expected_epic_version=1,
        actor=actor, key="first-adopt",
    )
    with pytest.raises(DecompositionConflict):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=second.job_id,
            proposal_digest=second_outcome.proposal_digest,
            expected_job_version=second_outcome.job_version, expected_epic_version=3,
            actor=actor, key="second-adopt",
        )
    still_proposed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert still_proposed.state == "proposed" and still_proposed.adopted_revision_id is None


@pytest.mark.asyncio
async def test_simultaneous_jobs_have_one_adoption_winner(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, version, prompt_id, first = await submit(service, actor, epic_id, project_id)
    second = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=prompt_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="second-submit",
    )
    await settle(factory, first.job_id)
    await settle(factory, second.job_id)
    jobs = [first.job_id, second.job_id]
    outcomes = [
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=job)
        for job in jobs
    ]
    start = asyncio.Event()

    async def adopt(index):
        await start.wait()
        return await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=jobs[index],
            proposal_digest=outcomes[index].proposal_digest,
            expected_job_version=outcomes[index].job_version,
            expected_epic_version=1, actor=actor, key=f"competing-{index}",
        )

    tasks = [asyncio.create_task(adopt(index)) for index in range(2)]
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, BaseException)]
    assert len(winners) == len(losers) == 1
    assert isinstance(losers[0], DecompositionConflict)
    async with factory() as session:
        epic = await session.get(Epic, epic_id)
        rows = [await session.get(BrainstormJobRow, job) for job in jobs]
        graphs = (await session.scalars(select(EpicGraphRevision).where(EpicGraphRevision.epic_id == epic_id))).all()
        mutations = (await session.scalars(select(ApiMutation).where(ApiMutation.action == "epic.decomposition.adopt"))).all()
        audits = (await session.scalars(select(OperatorAuditEvent).where(OperatorAuditEvent.event_type == "epic.decomposition.adopt"))).all()
        assert epic.version == 3 and epic.accepted_graph_revision_id == winners[0].graph_revision_id
        assert len(graphs) == len(mutations) == len(audits) == 1
        assert sum(row.adopted_revision_id is not None for row in rows) == 1


@pytest.mark.asyncio
async def test_simultaneous_same_key_replays_one_adoption(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    start = asyncio.Event()

    async def adopt():
        await start.wait()
        return await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest,
            expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key="same-key",
        )

    tasks = [asyncio.create_task(adopt()) for _ in range(2)]
    start.set()
    first, second = await asyncio.gather(*tasks)
    assert first == second
    async with factory() as session:
        epic = await session.get(Epic, epic_id)
        mutations = (await session.scalars(select(ApiMutation).where(ApiMutation.action == "epic.decomposition.adopt"))).all()
        audits = (await session.scalars(select(OperatorAuditEvent).where(OperatorAuditEvent.event_type == "epic.decomposition.adopt"))).all()
        graphs = (await session.scalars(select(EpicGraphRevision).where(EpicGraphRevision.epic_id == epic_id))).all()
        assert epic.accepted_graph_revision_id == first.graph_revision_id
        assert len(mutations) == len(audits) == len(graphs) == 1


@pytest.mark.asyncio
async def test_graph_save_failure_rolls_back_receipt_audit_and_selection(decomposition_session_factory, monkeypatch) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)

    async def fail_after_save(*args, **kwargs):
        raise RuntimeError("injected after graph save")

    monkeypatch.setattr(PostgresEpicItemsRepository, "adopt_revision", fail_after_save)
    with pytest.raises(RuntimeError, match="injected after graph save"):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key="failed-transaction",
        )
    async with factory() as session:
        assert (await session.get(Epic, epic_id)).version == 1
        assert (await session.get(BrainstormJobRow, receipt.job_id)).adopted_revision_id is None
        assert not (await session.scalars(select(EpicGraphRevision).where(EpicGraphRevision.epic_id == epic_id))).all()
        assert not (await session.scalars(select(ApiMutation).where(ApiMutation.action == "epic.decomposition.adopt"))).all()
        assert not (await session.scalars(select(OperatorAuditEvent).where(OperatorAuditEvent.event_type == "epic.decomposition.adopt"))).all()


@pytest.mark.asyncio
async def test_cancelled_queued_job_has_no_late_publication(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    cancelled = await service.cancel(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        expected_job_version=receipt.job_version, actor=actor, key="cancel-before-launch",
    )
    assert cancelled.state == "cancelled"
    worker = EpicBrainstormWorker(
        factory, owner=f"cancel-test-{uuid4().hex}",
        gateway_factory=lambda _job: ValidatedDecompositionGateway(SupervisedGateway()),
        reader_factory=lambda _job: AsyncMock(), kinds=frozenset(("decomposition",)),
    )
    assert await worker.run_once() is None
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "cancelled" and outcome.proposal is None


@pytest.mark.asyncio
async def test_invalid_result_fails_then_settled_retry_can_publish(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(
        factory, receipt.job_id,
        SupervisedGateway(lambda proposal: proposal.model_copy(update={"open_questions": ()})),
    )
    failed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert failed.state == "failed" and failed.failure == "invalid_output"
    assert failed.process_settled and failed.proposal is None
    retried = await service.retry(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        expected_job_version=failed.job_version, actor=actor, key="retry-after-invalid",
    )
    assert retried.state == "queued"
    await settle(factory, receipt.job_id)
    proposed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert proposed.state == "proposed" and proposed.process_settled
    async with factory() as session:
        attempts = (await session.scalars(select(BrainstormAttemptRow).where(
            BrainstormAttemptRow.job_id == receipt.job_id,
        ).order_by(BrainstormAttemptRow.number))).all()
        assert len(attempts) == 2 and all(attempt.process_settled for attempt in attempts)


@pytest.mark.asyncio
async def test_subject_routes_reject_other_job_kind(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor,
        key="cross-kind-create", text="Explore",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    )
    brainstorm = await service._authoring.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[-1].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="brainstorm-submit",
    )
    for operation in (
        service.observe(epic_id=epic_id, project_id=project_id, job_id=brainstorm.job_id),
        service.cancel(
            epic_id=epic_id, project_id=project_id, job_id=brainstorm.job_id,
            expected_job_version=brainstorm.job_version, actor=actor, key="cross-kind-cancel",
        ),
        service.retry(
            epic_id=epic_id, project_id=project_id, job_id=brainstorm.job_id,
            expected_job_version=brainstorm.job_version, actor=actor, key="cross-kind-retry",
        ),
    ):
        with pytest.raises(BrainstormConflict, match="kind conflicts with route"):
            await operation
    async with factory() as session:
        row = await session.get(BrainstormJobRow, brainstorm.job_id)
        assert row.state == "queued" and row.version == brainstorm.job_version

    decomposition = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=turns[-1].turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="decomposition-submit",
    )
    await settle(factory, decomposition.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=decomposition.job_id)
    with pytest.raises(BrainstormConflict, match="kind conflicts with route"):
        await service._authoring.adopt(
            epic_id=epic_id, project_id=project_id, job_id=decomposition.job_id,
            proposal_digest=outcome.proposal_digest,
            expected_job_version=outcome.job_version, expected_epic_version=1,
            actor=actor, key="brainstorm-adopt-decomposition",
        )


@pytest.mark.asyncio
async def test_adopt_rejects_edited_invalid_source_and_empty_graph(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.proposal is not None
    item = outcome.proposal.items[0]

    with pytest.raises(DecompositionValidationError, match="^graph item count is invalid$"):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key="empty-adopt", items=[],
        )

    bad_req_id = uuid4()
    bad_item = item.model_copy(update={"source_requirement_ids": [bad_req_id]})
    with pytest.raises(
        DecompositionValidationError,
        match=f"^source requirement is missing from accepted brief: {bad_req_id}$",
    ):
        await service.adopt(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
            expected_epic_version=1, actor=actor, key="bad-source-adopt", items=[bad_item],
        )
