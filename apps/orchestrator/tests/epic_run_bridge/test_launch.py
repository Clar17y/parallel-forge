"""Disposable PostgreSQL admission, replay, and rollback coverage."""

import asyncio
import json
from uuid import uuid4

import pytest
from alembic import command
from fastapi import FastAPI
from forge.api.routes.epic_run_bridge import router_for as bridge_router_for
from forge.api.schemas.epic_run_bridge import EpicAttemptResponse
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.auth import AuthenticatedActor, AuthenticationError, CsrfError
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_items import EpicItemsService
from forge.application.services.epic_run_bridge import EpicRunBridgeService
from forge.application.services.runs import RunService
from forge.domain.epic_brief import (
    BriefAdoptionRequest,
    BriefContent,
    BriefRequirement,
    BriefRevisionCreateRequest,
    EpicCreateRequest,
)
from forge.domain.epic_items import (
    GraphAdoptionRequest,
    GraphBindingConflict,
    GraphRevisionCreateRequest,
    GraphRevisionNotFound,
    ItemInput,
)
from forge.domain.epic_run_bridge import (
    DependencyEvidence,
    EpicExecutionBindingConflict,
    EpicLaunchConflict,
    LaunchRequest,
)
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
)
from forge.persistence.models import (
    ApiMutation,
    Project,
    ProjectPolicyVersion,
    Run,
    RunCommand,
    RunEvent,
    Task,
)
from forge.persistence.models.epic_run_bridge import EpicExecution, EpicItemAttempt
from forge.persistence.repositories.epic_run_bridge import PostgresEpicRunBridgeRepository
from forge.persistence.repositories.mutations import MutationConflict, PostgresMutationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select


class Inspector:
    sha = "c" * 40

    def inspect(self, **kwargs):
        return RepositoryInspection(
            canonical_path=kwargs["repository_path"],
            github_repository=kwargs["github_repository"],
            default_branch=kwargs["default_branch"],
            base_ref=f"refs/heads/{kwargs['default_branch']}",
            base_sha=self.sha,
        )


class BridgeWork(PostgresUnitOfWork):
    async def __aenter__(self):
        await super().__aenter__()
        self.epic_run_bridge = PostgresEpicRunBridgeRepository(self.session)
        return self


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    # The disposable database is dropped by test_database_url. The retained
    # graph migration intentionally refuses downgrading accepted graph data.
    command.upgrade(alembic_config_factory(test_database_url), "head")
    yield test_database_url


@pytest.fixture
def bridge_factory(session_factory):
    return lambda: BridgeWork(session_factory)


@pytest.fixture
async def bridge_tables(session_factory):
    engine = session_factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(EpicExecution.__table__.create)
        await connection.run_sync(EpicItemAttempt.__table__.create)
    yield


async def setup(session_factory, bridge_factory, *, dependencies=False, deferred=False):
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project_id = uuid4()
    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=f"/tmp/forge-{project_id}",
            github_repository=f"owner/repo-{project_id}",
            default_branch="main",
        )
        session.add(project)
        session.add(
            ProjectPolicyVersion(
                project_id=project_id,
                version=1,
                policy_digest="a" * 64,
                document_schema_version=1,
                document={},
            )
        )
        await session.flush()
        project.current_policy_version = 1
    brief_service = EpicBriefService(bridge_factory)
    graph_service = EpicItemsService(bridge_factory)
    epic = await brief_service.create(
        actor=actor,
        idempotency_key="epic",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )
    requirement_id = uuid4()
    brief = await brief_service.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="brief",
        request=BriefRevisionCreateRequest(
            expected_epic_version=1,
            content=BriefContent(
                problem="Problem",
                outcomes=["Outcome"],
                requirements=[
                    BriefRequirement(
                        requirement_id=requirement_id,
                        text="Required behavior",
                        acceptance_criteria=["Works"],
                    )
                ],
            ),
        ),
    )
    await brief_service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt-brief",
        request=BriefAdoptionRequest(
            expected_epic_version=2,
            brief_revision_id=brief.brief_revision_id,
            brief_digest=brief.content_digest,
        ),
    )
    first_id, second_id = uuid4(), uuid4()
    items = [
        ItemInput(
            item_id=first_id,
            disposition="required",
            ordinal=0,
            title="First",
            outcome="First done",
            acceptance_criteria=["Checked"],
            source_requirement_ids=[requirement_id],
        )
    ]
    if dependencies or deferred:
        items.append(
            ItemInput(
                item_id=second_id,
                disposition="deferred" if deferred else "required",
                ordinal=1,
                title="Second",
                outcome="Second done",
                acceptance_criteria=["Checked"],
                source_requirement_ids=[requirement_id],
                dependency_item_ids=[first_id] if dependencies else [],
            )
        )
    graph = await graph_service.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="graph",
        request=GraphRevisionCreateRequest(
            expected_epic_version=3,
            brief_revision_id=brief.brief_revision_id,
            brief_digest=brief.content_digest,
            items=items,
        ),
    )
    await graph_service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt-graph",
        request=GraphAdoptionRequest(
            expected_epic_version=4,
            graph_revision_id=graph.graph_revision_id,
            graph_digest=graph.graph_digest,
        ),
    )
    inspector = Inspector()
    run_service = RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp")
    service = EpicRunBridgeService(bridge_factory, run_service=run_service)

    def request(item_id=first_id, **kwargs):
        return LaunchRequest(
            expected_epic_version=5,
            brief_revision_id=brief.brief_revision_id,
            brief_digest=brief.content_digest,
            graph_revision_id=graph.graph_revision_id,
            graph_digest=graph.graph_digest,
            item_id=item_id,
            **kwargs,
        )

    return service, inspector, actor, epic.epic_id, first_id, second_id, request


@pytest.mark.asyncio
async def test_concurrent_replay_and_fenced_default_launch(
    session_factory, bridge_factory, bridge_tables
):
    service, inspector, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first, replay = await asyncio.gather(
        *[
            service.launch(actor=actor, epic_id=epic_id, idempotency_key="one", request=request())
            for _ in range(2)
        ]
    )
    assert first.attempt_id == replay.attempt_id
    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert first.attempt_number == 1
    assert EpicAttemptResponse(**dict(first)).run_id == first.run_id
    restarted = EpicRunBridgeService(
        bridge_factory,
        run_service=RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp"),
    )
    assert (
        await restarted.launch(
            actor=actor, epic_id=epic_id, idempotency_key="one", request=request()
        )
    ).model_dump(mode="json") == first.model_dump(mode="json")
    assert (await service.get(epic_id, first.attempt_id)).model_dump(
        mode="json"
    ) == first.model_dump(mode="json")
    assert len(await service.list(epic_id)) == 1
    assert first.base_sha == inspector.sha
    with pytest.raises(MutationConflict):
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="one",
            request=request(owner_override=True),
        )
    with pytest.raises(EpicLaunchConflict, match="active_child"):
        await service.launch(actor=actor, epic_id=epic_id, idempotency_key="two", request=request())
    override = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="override",
        request=request(
            owner_override=True, override_note="Parallel work", execution_id=first.execution_id
        ),
    )
    assert override.run_id != first.run_id
    assert override.attempt_number == 2
    assert override.blocker_codes == ["active_child"]
    assert override.owner_override and override.override_note == "Parallel work"
    inspector.sha = "d" * 40
    assert (await service.get(epic_id, first.attempt_id)).base_sha == "c" * 40
    assert (await service.get(epic_id, override.attempt_id)).base_sha == "c" * 40
    drifted = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="drifted",
        request=request(owner_override=True, execution_id=first.execution_id),
    )
    assert drifted.base_sha == "d" * 40
    assert drifted.attempt_number == 3
    assert drifted.context_digest != first.context_digest
    assert drifted.task_digest != first.task_digest
    async with session_factory() as session:
        drifted_task = await session.get(Task, drifted.task_id)
        assert drifted_task is not None
        assert '"base_sha":"' + "d" * 40 + '"' in drifted_task.body
        for model, expected in (
            (EpicExecution, 1),
            (EpicItemAttempt, 3),
            (Task, 3),
            (Run, 3),
            (RunCommand, 3),
            (RunEvent, 3),
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == expected


@pytest.mark.asyncio
async def test_dependency_and_deferred_need_explicit_override(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )
    with pytest.raises(EpicLaunchConflict, match="predecessor_unverified"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="blocked", request=request(second_id)
        )
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="ack",
        request=request(second_id, owner_override=True),
    )
    assert attempt.dependency_evidence[0].status == "unknown"
    assert attempt.blocker_codes == ["predecessor_unverified"]
    assert not attempt.dependency_evidence[0].integrated_sha


@pytest.mark.asyncio
async def test_distinct_keys_race_and_invalid_binding_cannot_create_source(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    results = await asyncio.gather(
        service.launch(actor=actor, epic_id=epic_id, idempotency_key="left", request=request()),
        service.launch(actor=actor, epic_id=epic_id, idempotency_key="right", request=request()),
        return_exceptions=True,
    )
    assert sum(isinstance(value, EpicLaunchConflict) for value in results) == 1
    with pytest.raises(GraphBindingConflict):
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="bad-graph",
            request=request().model_copy(update={"graph_digest": "0" * 64}),
        )
    with pytest.raises(GraphBindingConflict, match="item is not in saved graph"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="bad-item", request=request(uuid4())
        )
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1


@pytest.mark.asyncio
async def test_deferred_item_is_readable_blocker(session_factory, bridge_factory, bridge_tables):
    service, _, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, deferred=True
    )
    with pytest.raises(EpicLaunchConflict, match="item_deferred"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="default", request=request(second_id)
        )
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="override",
        request=request(second_id, owner_override=True),
    )
    assert attempt.blocker_codes == ["item_deferred"]


@pytest.mark.asyncio
async def test_active_child_blocks_new_accepted_graph_too(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first", request=request()
    )
    async with session_factory() as session:
        original_body = (await session.get(Task, first.task_id)).body
    graph_service = EpicItemsService(bridge_factory)
    old = await graph_service.get_revision(epic_id, request().graph_revision_id)
    item = ItemInput.model_validate(
        old.items[0].model_dump(exclude={"graph_revision_id", "item_digest"})
    )
    revision = await graph_service.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="next-graph",
        request=GraphRevisionCreateRequest(
            expected_epic_version=5,
            brief_revision_id=old.brief_revision_id,
            brief_digest=old.brief_digest,
            items=[item],
        ),
    )
    await graph_service.adopt_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="next-adopt",
        request=GraphAdoptionRequest(
            expected_epic_version=6,
            graph_revision_id=revision.graph_revision_id,
            graph_digest=revision.graph_digest,
        ),
    )
    next_request = request().model_copy(
        update={
            "expected_epic_version": 7,
            "graph_revision_id": revision.graph_revision_id,
            "graph_digest": revision.graph_digest,
        }
    )
    with pytest.raises(EpicLaunchConflict, match="active_child"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="new-default", request=next_request
        )
    explicit = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="new-override",
        request=next_request.model_copy(update={"owner_override": True}),
    )
    assert explicit.blocker_codes == ["active_child"]
    async with session_factory() as session:
        assert (await session.get(Task, first.task_id)).body == original_body
        assert (await session.get(Task, explicit.task_id)).body != original_body


@pytest.mark.asyncio
async def test_real_unaccepted_graph_requires_explicit_owner_action(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    graph_service = EpicItemsService(bridge_factory)
    old = await graph_service.get_revision(epic_id, request().graph_revision_id)
    item = ItemInput.model_validate(
        old.items[0].model_dump(exclude={"graph_revision_id", "item_digest"})
    )
    saved = await graph_service.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="unaccepted-graph",
        request=GraphRevisionCreateRequest(
            expected_epic_version=5,
            brief_revision_id=old.brief_revision_id,
            brief_digest=old.brief_digest,
            items=[item],
        ),
    )
    selected = request().model_copy(
        update={
            "expected_epic_version": 6,
            "graph_revision_id": saved.graph_revision_id,
            "graph_digest": saved.graph_digest,
        }
    )
    with pytest.raises(EpicLaunchConflict, match="graph_not_accepted"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="default-unaccepted", request=selected
        )
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="owner-unaccepted",
        request=selected.model_copy(update={"owner_override": True}),
    )
    assert attempt.graph_revision_id == saved.graph_revision_id
    assert attempt.blocker_codes == ["graph_not_accepted"]


@pytest.mark.asyncio
async def test_combined_controls_preserve_requested_actual_versions_and_unknown_proof(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True, deferred=True
    )
    first = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first", request=request()
    )
    stale = request(second_id, owner_override=True).model_copy(
        update={"expected_epic_version": 4, "execution_id": first.execution_id}
    )
    with pytest.raises(EpicLaunchConflict) as error:
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="default",
            request=stale.model_copy(update={"owner_override": False}),
        )
    assert error.value.blocker_codes == (
        "epic_version_stale",
        "item_deferred",
        "active_child",
        "predecessor_unverified",
    )
    attempt = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="override", request=stale
    )
    assert attempt.expected_epic_version == 4
    assert attempt.actual_epic_version == 5
    assert attempt.item_disposition == "deferred"
    assert attempt.blocker_codes == list(error.value.blocker_codes)
    assert attempt.dependency_evidence[0].status == "unknown"
    async with session_factory() as session:
        task = await session.get(Task, attempt.task_id)
        assert task is not None
        context = json.loads(task.body)
        assert context["context_digest"] == attempt.context_digest
        assert context["blocker_codes"] == list(error.value.blocker_codes)
        assert context["execution_id"] == str(first.execution_id)
        assert context["predecessors"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_explicit_execution_reuse_and_same_source_new_epoch(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first", request=request()
    )
    next_epoch = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="second", request=request(owner_override=True)
    )
    same_epoch = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="third",
        request=request(owner_override=True, execution_id=first.execution_id),
    )
    assert next_epoch.execution_id != first.execution_id
    assert next_epoch.attempt_number == 1
    assert same_epoch.execution_id == first.execution_id
    assert same_epoch.attempt_number == 2
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EpicExecution)) == 2


@pytest.mark.asyncio
async def test_real_old_brief_graph_pair_can_be_explicitly_selected(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    brief_service = EpicBriefService(bridge_factory)
    newer = await brief_service.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="new-brief",
        request=BriefRevisionCreateRequest(
            expected_epic_version=5,
            content=BriefContent(
                problem="New",
                outcomes=["New outcome"],
                requirements=[
                    BriefRequirement(
                        requirement_id=uuid4(), text="New need", acceptance_criteria=["New check"]
                    )
                ],
            ),
        ),
    )
    await brief_service.adopt_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="new-brief-adopt",
        request=BriefAdoptionRequest(
            expected_epic_version=6,
            brief_revision_id=newer.brief_revision_id,
            brief_digest=newer.content_digest,
        ),
    )
    old_pair = request().model_copy(update={"expected_epic_version": 7})
    with pytest.raises(EpicLaunchConflict) as error:
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="old-default", request=old_pair
        )
    assert error.value.blocker_codes == ("brief_not_accepted", "graph_not_accepted")
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="old-override",
        request=old_pair.model_copy(update={"owner_override": True}),
    )
    assert attempt.brief_revision_id == old_pair.brief_revision_id
    assert attempt.graph_revision_id == old_pair.graph_revision_id
    assert attempt.blocker_codes == list(error.value.blocker_codes)


@pytest.mark.asyncio
async def test_cross_epic_or_mismatched_execution_cannot_fabricate_source(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    _, _, other_actor, other_epic_id, _, _, other_request = await setup(
        session_factory, bridge_factory
    )
    other_attempt = await service.launch(
        actor=other_actor,
        epic_id=other_epic_id,
        idempotency_key="other-launch",
        request=other_request(),
    )
    with pytest.raises(EpicExecutionBindingConflict):
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="wrong-execution",
            request=request(execution_id=other_attempt.execution_id, owner_override=True),
        )
    cross_graph = request().model_copy(
        update={
            "graph_revision_id": other_request().graph_revision_id,
            "graph_digest": other_request().graph_digest,
            "owner_override": True,
        }
    )
    with pytest.raises(GraphRevisionNotFound):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="wrong-graph", request=cross_graph
        )
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1


@pytest.mark.asyncio
async def test_base_moves_while_eligibility_awaits_and_proof_is_rechecked(
    session_factory, bridge_factory, bridge_tables
):
    _, inspector, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )

    class MovingEligibility:
        def __init__(self):
            self.calls: list[str] = []

        async def evidence(self, *, epic_id, item_ids, base_sha):
            self.calls.append(base_sha)
            if base_sha == "c" * 40:
                inspector.sha = "d" * 40
                return [
                    DependencyEvidence(
                        item_id=item_ids[0],
                        status="verified",
                        predecessor_run_id=uuid4(),
                        integrated_sha=base_sha,
                    )
                ]
            return [DependencyEvidence(item_id=item_ids[0], status="unknown")]

    eligibility = MovingEligibility()
    service = EpicRunBridgeService(
        bridge_factory,
        run_service=RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp"),
        eligibility=eligibility,
    )
    with pytest.raises(EpicLaunchConflict, match="predecessor_unverified"):
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="moving-default",
            request=request(second_id),
        )
    assert eligibility.calls == ["c" * 40, "d" * 40]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="moving-owner",
        request=request(second_id, owner_override=True),
    )
    assert attempt.base_sha == "d" * 40
    assert attempt.dependency_evidence[0].status == "unknown"
    assert "predecessor_unverified" in attempt.blocker_codes
    assert eligibility.calls[-1] == "d" * 40


@pytest.mark.asyncio
async def test_continuously_moving_base_requires_explicit_owner_action_and_forgets_stale_proof(
    session_factory, bridge_factory, bridge_tables
):
    _, inspector, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )

    class AlwaysMovingEligibility:
        def __init__(self):
            self.calls: list[str] = []

        async def evidence(self, *, epic_id, item_ids, base_sha):
            self.calls.append(base_sha)
            digits = "cdef0123456789ab"
            inspector.sha = digits[(digits.index(base_sha[0]) + 1) % len(digits)] * 40
            return [
                DependencyEvidence(
                    item_id=item_ids[0],
                    status="verified",
                    predecessor_run_id=uuid4(),
                    integrated_sha=base_sha,
                )
            ]

    eligibility = AlwaysMovingEligibility()
    service = EpicRunBridgeService(
        bridge_factory,
        run_service=RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp"),
        eligibility=eligibility,
    )
    launch_request = request(second_id)
    with pytest.raises(EpicLaunchConflict) as blocked:
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="moving-default", request=launch_request
        )
    assert blocked.value.blocker_codes == (
        "repository_base_moved",
        "predecessor_unverified",
    )
    assert eligibility.calls == ["c" * 40, "d" * 40, "e" * 40]
    async with session_factory() as session:
        for model in (EpicExecution, EpicItemAttempt, Task, Run):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ApiMutation)
                .where(ApiMutation.action == "epic.item.launch")
            )
            == 0
        )

    owner_request = launch_request.model_copy(update={"owner_override": True})
    attempt = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="moving-owner", request=owner_request
    )
    assert eligibility.calls == ["c" * 40, "d" * 40, "e" * 40, "f" * 40, "0" * 40, "1" * 40]
    assert attempt.base_sha == inspector.sha == "2" * 40
    assert attempt.blocker_codes == ["repository_base_moved", "predecessor_unverified"]
    assert attempt.dependency_evidence == [DependencyEvidence(item_id=first_id, status="unknown")]
    replay = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="moving-owner", request=owner_request
    )
    assert replay.model_dump(mode="json") == attempt.model_dump(mode="json")
    assert len(eligibility.calls) == 6
    assert (await service.get(epic_id, attempt.attempt_id)).model_dump(
        mode="json"
    ) == attempt.model_dump(mode="json")
    async with session_factory() as session:
        run = await session.get(Run, attempt.run_id)
        task = await session.get(Task, attempt.task_id)
        assert run is not None and run.base_sha == attempt.base_sha
        assert task is not None and task.task_digest == attempt.task_digest
        for model in (EpicExecution, EpicItemAttempt, Task, Run):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ApiMutation)
                .where(ApiMutation.action == "epic.item.launch")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_unsafe_note_or_malformed_evidence_cannot_persist(
    session_factory, bridge_factory, bridge_tables
):
    service, inspector, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )
    unsafe = request(second_id, owner_override=True).model_copy(
        update={"override_note": "password=synthetic_placeholder"}
    )
    with pytest.raises(ValueError, match="durable payload contains a raw credential"):
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="unsafe-note", request=unsafe
        )

    class MalformedEligibility:
        async def evidence(self, *, epic_id, item_ids, base_sha):
            return [
                {
                    "item_id": str(item_ids[0]),
                    "status": "unknown",
                    "secret": "synthetic_placeholder",
                }
            ]

    malformed_service = EpicRunBridgeService(
        bridge_factory,
        run_service=RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp"),
        eligibility=MalformedEligibility(),
    )
    with pytest.raises(ValueError, match="durable payload contains a raw credential"):
        await malformed_service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="unsafe-evidence",
            request=request(second_id, owner_override=True),
        )
    async with session_factory() as session:
        for model in (ApiMutation, EpicExecution, EpicItemAttempt, Task, Run, RunCommand):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == (5 if model is ApiMutation else 0)


@pytest.mark.asyncio
async def test_http_requires_operator_csrf_idempotency_and_exposes_owner_action(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)

    class Auth:
        async def require_session(self, token, *, csrf_token=None, require_csrf=False):
            if token != "session":
                raise AuthenticationError("invalid session")
            if require_csrf and csrf_token != "csrf":
                raise CsrfError("invalid csrf")
            return actor

    app = FastAPI()
    app.state.settings = Settings(web_origin="http://127.0.0.1:3000")
    app.state.auth_service = Auth()
    app.state.epic_run_bridge_service = service
    app.include_router(bridge_router_for(), prefix="/api")
    url = f"/api/epics/{epic_id}/work-item-runs"
    payload = request().model_dump(mode="json")
    headers = {"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": "csrf"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        unauthenticated = await client.get(url)
        assert unauthenticated.status_code == 401
        client.cookies.set("forge_session", "session")
        assert (
            await client.post(url, headers={"Idempotency-Key": "no-csrf"}, json=payload)
        ).status_code == 403
        assert (await client.post(url, headers=headers, json=payload)).status_code == 422
        created = await client.post(
            url, headers={**headers, "Idempotency-Key": "launch"}, json=payload
        )
        assert created.status_code == 201
        replay = await client.post(
            url, headers={**headers, "Idempotency-Key": "launch"}, json=payload
        )
        assert replay.status_code == 201 and replay.json() == created.json()
        read = await client.get(f"{url}/{created.json()['attempt_id']}")
        listed = await client.get(url)
        assert read.status_code == 200 and read.json() == created.json()
        assert listed.status_code == 200 and listed.json() == [created.json()]
        blocked = await client.post(
            url, headers={**headers, "Idempotency-Key": "blocked"}, json=payload
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == {
            "code": "epic_launch_blocked",
            "blocker_codes": ["active_child"],
            "actual_epic_version": 5,
            "owner_action": "retry_with_owner_override",
        }
        owner = await client.post(
            url,
            headers={**headers, "Idempotency-Key": "owner"},
            json={**payload, "owner_override": True},
        )
        assert owner.status_code == 201
        assert owner.json()["blocker_codes"] == ["active_child"]


@pytest.mark.asyncio
async def test_rollback_and_retry_after_receipt_failure(
    session_factory, bridge_factory, bridge_tables, monkeypatch
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected completion failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMutationRepository, "complete", fail)
        with pytest.raises(RuntimeError, match="completion failure"):
            await service.launch(
                actor=actor, epic_id=epic_id, idempotency_key="retry", request=request()
            )
    async with session_factory() as session:
        for model in (EpicExecution, EpicItemAttempt, Task, Run, RunCommand, RunEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    result = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="retry", request=request()
    )
    assert result.run_id == (await service.get(epic_id, result.attempt_id)).run_id


@pytest.mark.asyncio
async def test_profile_is_frozen_for_epic_run_and_replay(
    session_factory, bridge_factory, bridge_tables
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(
                purpose=SpecialistPurpose.PRIMARY,
                preferred_route=RouteSpec(
                    provider="openai",
                    client="openai-client",
                    model="openai-model",
                    effort=ReasoningEffort.LOW,
                    auth_mode=AuthMode.SUBSCRIPTION,
                    billing_mode=BillingMode.ALLOWANCE_ONLY,
                ),
            ),
        ),
    )
    async with bridge_factory() as work:
        epic = await work.epics.get(epic_id)
        await work.subscription.select_project_profile(epic.project_id, profile)
        await work.commit()
    attempt = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="profile", request=request()
    )
    async with bridge_factory() as work:
        envelope = await work.subscription.envelope_for_run(attempt.run_id)
        assert envelope is not None
        assert envelope.profile_id == profile.profile_id
        assert envelope.profile_version == 1
        assert envelope.safety_policy_version == 1
        await work.commit()
    assert (
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="profile", request=request()
        )
    ).attempt_id == attempt.attempt_id
