"""Disposable PostgreSQL admission, replay, and rollback coverage."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from forge.api.routes.epic_lifecycle import router_for as lifecycle_router_for
from forge.api.routes.epic_run_bridge import router_for as bridge_router_for
from forge.api.schemas.epic_run_bridge import EpicAttemptResponse
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.auth import AuthenticatedActor, AuthenticationError, CsrfError
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_budget import (
    EpicBudgetEdit,
    EpicBudgetPermitRequest,
    EpicBudgetService,
)
from forge.application.services.epic_items import EpicItemsService
from forge.application.services.epic_lifecycle import EpicControlRequest, EpicLifecycleService
from forge.application.services.epic_run_bridge import EpicRunBridgeService
from forge.application.services.runs import (
    RunCommandService,
    RunService,
    hash_run_command_idempotency_key,
)
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
    TaskBudget,
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
from httpx import ASGITransport, AsyncClient, ConnectError, ConnectTimeout
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
def bridge_factory(session_factory):
    return lambda: BridgeWork(session_factory)


async def setup(session_factory, bridge_factory, *, dependencies=False, deferred=False, independent=False, repository_path=None):
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project_id = uuid4()
    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=str(repository_path) if repository_path is not None else f"/tmp/forge-{project_id}",
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
    if dependencies or deferred or independent:
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


async def submit_authoring_for_child(
    session_factory, *, child, actor, epic_id, kind="brainstorm", provider="fake"
):
    from forge.application.services.epic_brainstorm import (
        EpicBrainstormService,
        EpicBriefBrainstormAdapter,
    )
    from forge.application.services.epic_decomposition import EpicDecompositionService
    from forge.domain.subscription import RouteBinding
    from forge.persistence.repositories.epic_decomposition import (
        PostgresEpicDecompositionUnitOfWork,
    )

    async with session_factory() as session:
        child_task = await session.get(Task, child.task_id)
        assert child_task is not None
        project_id = child_task.project_id
    route = RouteSpec(provider=provider, client="fake", model="fixture")
    base = EpicBrainstormService(
        session_factory,
        EpicBriefBrainstormAdapter,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    authoring = (
        base
        if kind == "brainstorm"
        else EpicDecompositionService(
            lambda: PostgresEpicDecompositionUnitOfWork(session_factory),
            authoring_service=base,
        )
    )
    conversation, version = await authoring.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key=f"{kind}-conversation",
        text="Next",
    )
    turn = (
        await authoring.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation)
    )[-1]
    submitted = await authoring.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=5,
        expected_conversation_version=version,
        actor=actor,
        key=f"{kind}-submit",
    )
    return authoring, submitted, project_id


@pytest.mark.asyncio
async def test_concurrent_replay_and_fenced_default_launch(session_factory, bridge_factory):
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
    assert override.blocker_codes == ["child_usage_unproved", "epic_usage_unknown", "active_child"]
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
async def test_dependency_and_deferred_need_explicit_override(session_factory, bridge_factory):
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
    session_factory, bridge_factory
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
async def test_deferred_item_is_readable_blocker(session_factory, bridge_factory):
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
async def test_active_child_blocks_new_accepted_graph_too(session_factory, bridge_factory):
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
    assert explicit.blocker_codes == ["child_usage_unproved", "epic_usage_unknown", "active_child"]
    async with session_factory() as session:
        assert (await session.get(Task, first.task_id)).body == original_body
        assert (await session.get(Task, explicit.task_id)).body != original_body


@pytest.mark.asyncio
async def test_real_unaccepted_graph_requires_explicit_owner_action(
    session_factory, bridge_factory
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
    session_factory, bridge_factory
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
        "child_usage_unproved",
        "epic_usage_unknown",
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
async def test_explicit_execution_reuse_and_same_source_new_epoch(session_factory, bridge_factory):
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
    session_factory, bridge_factory
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
    session_factory, bridge_factory
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
    session_factory, bridge_factory
):
    _, inspector, actor, epic_id, _, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )

    class MovingEligibility:
        def __init__(self):
            self.calls: list[str] = []

        async def evidence(self, *, epic_id, execution_id, item_ids, base_ref, base_sha):
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
    execution = await service.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    with pytest.raises(EpicLaunchConflict, match="predecessor_unverified"):
        await service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="moving-default",
            request=request(second_id, execution_id=execution.execution_id),
        )
    assert eligibility.calls == ["c" * 40, "d" * 40]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
    attempt = await service.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="moving-owner",
        request=request(second_id, execution_id=execution.execution_id, owner_override=True),
    )
    assert attempt.base_sha == "d" * 40
    assert attempt.dependency_evidence[0].status == "unknown"
    assert "predecessor_unverified" in attempt.blocker_codes
    assert eligibility.calls[-1] == "d" * 40


@pytest.mark.integration
async def test_required_child_failure_during_eligibility_blocks_final_admission(
    session_factory, bridge_factory, monkeypatch
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, inspector, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="race-first", request=request(first_id)
    )
    async def no_budget_blockers(self, epic_id, project_id, *, ceiling, hold):
        return []

    monkeypatch.setattr(
        PostgresEpicRunBridgeRepository, "child_budget_blockers", no_budget_blockers
    )

    class SettlingEligibility:
        calls = 0

        async def evidence(self, *, epic_id, execution_id, item_ids, base_ref, base_sha):
            self.calls += 1
            if self.calls == 1:
                async with bridge_factory() as work:
                    run = await work.runs.get(first.run_id)
                    await work.runs.transition(
                        first.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
                    )
                    hold = await work.session.get(EpicChildBudgetHold, first.attempt_id)
                    assert hold is not None
                    hold.effects_settled = True
                    for command in (
                        await work.session.scalars(
                            select(RunCommand).where(RunCommand.run_id == first.run_id)
                        )
                    ).all():
                        command.status = "COMPLETED"
                        command.completed_at = datetime.now(UTC)
                    await work.commit()
            return [
                DependencyEvidence(
                    item_id=item_ids[0], status="verified",
                    predecessor_run_id=first.run_id, integrated_sha=base_sha,
                )
            ]

    eligibility = SettlingEligibility()
    service = EpicRunBridgeService(
        bridge_factory,
        run_service=RunService(bridge_factory, repository_inspector=inspector, data_root="/tmp"),
        eligibility=eligibility,
    )
    async with session_factory() as session:
        before = []
        for model in (Task, Run, EpicItemAttempt):
            before.append(await session.scalar(select(func.count()).select_from(model)))
    with pytest.raises(EpicLaunchConflict) as blocked:
        await service.launch(
            actor=actor, epic_id=epic_id, idempotency_key="race-second",
            request=request(second_id, execution_id=first.execution_id),
        )
    assert blocked.value.blocker_codes == ("predecessor_failed",)
    assert eligibility.calls == 1
    async with session_factory() as session:
        after = []
        for model in (Task, Run, EpicItemAttempt):
            after.append(await session.scalar(select(func.count()).select_from(model)))
    assert after == before
    owner_request = request(second_id, execution_id=first.execution_id, owner_override=True)
    admitted = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="race-owner", request=owner_request
    )
    assert admitted.owner_override and admitted.override_note is None
    assert "predecessor_failed" in admitted.blocker_codes
    assert await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="race-owner", request=owner_request
    ) == admitted
    assert eligibility.calls == 2


@pytest.mark.asyncio
async def test_continuously_moving_base_requires_explicit_owner_action_and_forgets_stale_proof(
    session_factory, bridge_factory
):
    _, inspector, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )

    class AlwaysMovingEligibility:
        def __init__(self):
            self.calls: list[str] = []

        async def evidence(self, *, epic_id, execution_id, item_ids, base_ref, base_sha):
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
    execution = await service.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    launch_request = request(second_id, execution_id=execution.execution_id)
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
            assert await session.scalar(select(func.count()).select_from(model)) == (
                1 if model is EpicExecution else 0
            )
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
async def test_unsafe_note_or_malformed_evidence_cannot_persist(session_factory, bridge_factory):
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
        async def evidence(self, *, epic_id, execution_id, item_ids, base_ref, base_sha):
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
    execution = await service.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    with pytest.raises(ValueError, match="durable payload contains a raw credential"):
        await malformed_service.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="unsafe-evidence",
            request=request(second_id, execution_id=execution.execution_id, owner_override=True),
        )
    async with session_factory() as session:
        for model in (ApiMutation, EpicExecution, EpicItemAttempt, Task, Run, RunCommand):
            count = await session.scalar(select(func.count()).select_from(model))
            assert count == (6 if model is ApiMutation else 1 if model is EpicExecution else 0)


@pytest.mark.asyncio
async def test_http_requires_operator_csrf_idempotency_and_exposes_owner_action(
    session_factory, bridge_factory
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
            "blocker_codes": ["child_usage_unproved", "epic_usage_unknown", "active_child"],
            "actual_epic_version": 5,
            "owner_action": "retry_with_owner_override",
        }
        owner = await client.post(
            url,
            headers={**headers, "Idempotency-Key": "owner"},
            json={**payload, "owner_override": True},
        )
        assert owner.status_code == 201
        assert owner.json()["blocker_codes"] == [
            "child_usage_unproved",
            "epic_usage_unknown",
            "active_child",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_override", (False, True))
async def test_rollback_and_retry_after_receipt_failure(
    session_factory, bridge_factory, monkeypatch, owner_override
):
    from forge.persistence.models.epic_run_bridge import EpicBudgetAdmissionPermit

    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    launch_request = request(owner_override=owner_override)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected completion failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMutationRepository, "complete", fail)
        with pytest.raises(RuntimeError, match="completion failure"):
            await service.launch(
                actor=actor, epic_id=epic_id, idempotency_key="retry", request=launch_request
            )
    async with session_factory() as session:
        for model in (
            EpicExecution,
            EpicItemAttempt,
            EpicBudgetAdmissionPermit,
            Task,
            Run,
            RunCommand,
            RunEvent,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    result = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="retry", request=launch_request
    )
    assert result.run_id == (await service.get(epic_id, result.attempt_id)).run_id
    async with session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(EpicBudgetAdmissionPermit)
        ) == int(owner_override)


@pytest.mark.asyncio
async def test_profile_is_frozen_for_epic_run_and_replay(session_factory, bridge_factory):
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


@pytest.mark.asyncio
async def test_explicit_start_freezes_accepted_pair_without_creating_child(
    session_factory, bridge_factory
):
    service, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    source = request()
    first = await service.start(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="start",
        expected_epic_version=5,
    )
    replay = await service.start(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="start",
        expected_epic_version=5,
    )
    assert replay == first
    assert first.brief_revision_id == source.brief_revision_id
    assert first.graph_revision_id == source.graph_revision_id
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EpicExecution)) == 1
        assert await session.scalar(select(func.count()).select_from(EpicItemAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(Run)) == 0


@pytest.mark.asyncio
async def test_no_child_control_settles_and_replay_keeps_receipt(session_factory, bridge_factory):
    bridge, _, actor, epic_id, _, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="start", expected_epic_version=5
    )
    service = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    pause = await service.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert pause.state == "PAUSED" and pause.intent_ids == ()
    resume = await service.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="resume",
        request=EpicControlRequest(expected_execution_version=2, action="resume"),
    )
    assert resume.state == "ACTIVE" and resume.execution_version == 3
    assert (
        await service.request(
            actor=actor,
            epic_id=epic_id,
            execution_id=execution.execution_id,
            idempotency_key="pause",
            request=EpicControlRequest(expected_execution_version=1, action="pause"),
        )
        == pause
    )


@pytest.mark.asyncio
async def test_pause_intent_fences_launch_and_owner_admission_keeps_pending_state(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="first",
        request=request(execution_id=execution.execution_id),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    pause = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert pause.state == "PAUSE_REQUESTED" and len(pause.intent_ids) == 1
    with pytest.raises(EpicLaunchConflict) as error:
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="second",
            request=request(execution_id=execution.execution_id),
        )
    assert "execution_not_active" in error.value.blocker_codes
    owner = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="owner",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    assert first.run_id != owner.run_id
    assert "execution_not_active" in owner.blocker_codes
    assert await controls.reconcile_one() == pause.intent_ids[0]
    async with bridge_factory() as work:
        assert (
            await work.epic_run_bridge.control_state(execution.execution_id)
        ) == "PAUSE_REQUESTED"
        command = await work.commands.get_by_idempotency_key(
            hash_run_command_idempotency_key(
                f"epic-control:{pause.intent_ids[0]}", actor_id=actor.actor_id, run_id=first.run_id
            )
        )
        assert command is not None and command.run_id == first.run_id
        await work.commit()


@pytest.mark.integration
async def test_owner_child_after_no_child_cancel_revokes_terminal_projection(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="cancel-start", expected_epic_version=5
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    cancelled = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="no-child-cancel",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    assert cancelled.state == "CANCELLED"
    with pytest.raises(EpicLaunchConflict, match="execution_not_active"):
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="after-cancel-default",
            request=request(execution_id=execution.execution_id),
        )
    child = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="after-cancel-owner",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "BLOCKED"
    assert projection.blocker_code == "uncontrolled_child"
    assert projection.control_version == 3
    assert projection.children[0].attempt.run_id == child.run_id
    assert "execution_not_active" in child.blocker_codes
    budget = EpicBudgetService(bridge_factory)
    current = await budget.get(epic_id)
    permit = await budget.permit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="inactive-child-permit",
        request=EpicBudgetPermitRequest(
            expected_version=current.version,
            run_id=child.run_id,
        ),
    )
    assert "execution_not_active" in permit.warnings
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="cancelled-owner-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    assert (
        await executor.admit_next(
            "cancelled-owner", TaskBudget(max_provider_attempts=1, max_repairs=0)
        )
        is None
    )
    assert len((await budget.get(epic_id)).permits) == 2
    assert all(value.consumed_attempt_id is None for value in (await budget.get(epic_id)).permits)


@pytest.mark.integration
async def test_lifecycle_http_discovery_owner_budget_and_control_auth(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, _, _, _ = await setup(session_factory, bridge_factory)

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
    app.state.epic_run_bridge_service = bridge
    app.state.epic_lifecycle_service = EpicLifecycleService(
        bridge_factory, commands=RunCommandService(bridge_factory)
    )
    app.state.epic_budget_service = EpicBudgetService(bridge_factory)
    app.include_router(lifecycle_router_for(), prefix="/api")
    base = f"/api/epics/{epic_id}"
    headers = {"Origin": "http://127.0.0.1:3000", "X-CSRF-Token": "csrf"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        assert (await client.get(f"{base}/executions")).status_code == 401
        client.cookies.set("forge_session", "session")
        assert (
            await client.post(
                f"{base}/executions",
                headers={"Idempotency-Key": "start"},
                json={"expected_epic_version": 5},
            )
        ).status_code == 403
        started = await client.post(
            f"{base}/executions",
            headers={**headers, "Idempotency-Key": "start"},
            json={"expected_epic_version": 5},
        )
        assert started.status_code == 201
        execution_id = started.json()["execution_id"]
        listed = await client.get(f"{base}/executions")
        assert listed.status_code == 200
        assert listed.json()[0]["execution"]["execution_id"] == execution_id
        assert listed.json()[0]["control_state"] == "ACTIVE"
        budget = await client.get(f"{base}/budget")
        assert budget.status_code == 200 and budget.json()["version"] == 0
        edited = await client.put(
            f"{base}/budget",
            headers={**headers, "Idempotency-Key": "budget"},
            json={
                "expected_version": 0,
                "ceiling": budget.json()["ceiling"],
                "disabled_dimensions": ["provider_attempts"],
            },
        )
        assert edited.status_code == 200
        assert edited.json()["disabled_dimensions"] == ["provider_attempts"]
        assert (await client.get(f"{base}/budget")).json()["initialized"]
        cancelled = await client.post(
            f"{base}/executions/{execution_id}/commands",
            headers={**headers, "Idempotency-Key": "cancel"},
            json={"expected_execution_version": 1, "action": "cancel"},
        )
        assert cancelled.status_code == 202 and cancelled.json()["state"] == "CANCELLED"
        read = await client.get(f"{base}/executions/{execution_id}")
        assert read.status_code == 200 and read.json()["control_state"] == "CANCELLED"
        replay = await client.post(
            f"{base}/executions/{execution_id}/commands",
            headers={**headers, "Idempotency-Key": "cancel"},
            json={"expected_execution_version": 1, "action": "cancel"},
        )
        assert replay.json() == cancelled.json()


@pytest.mark.integration
async def test_merging_child_cancel_refusal_stays_blocked_with_actual_run(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="merging-child", request=request()
    )
    async with bridge_factory() as work:
        row = await work.session.get(Run, child.run_id)
        row.state = "MERGING"
        await work.commit()
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    requested = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="cancel-merging",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    assert requested.state == "CANCEL_REQUESTED"
    assert await controls.reconcile_one() == requested.intent_ids[0]
    projection = await controls.get(epic_id, child.execution_id)
    assert projection.control_state == "BLOCKED"
    assert projection.blocker_code == "child_control_refused"
    assert projection.intents[0].status == "refused"
    assert "merge" in projection.intents[0].refusal
    assert projection.children[0].run_state.value == "MERGING"
    assert not projection.children[0].effects_settled


@pytest.mark.integration
async def test_normal_api_and_separate_worker_reconcile_refused_control(
    session_factory, bridge_factory, test_database_url, tmp_path
):
    from forge.api.app import create_app
    from forge.worker.main import run_worker

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="process-child", request=request()
    )
    async with bridge_factory() as work:
        row = await work.session.get(Run, child.run_id)
        row.state = "MERGING"
        await work.commit()
    settings = Settings(database_url=test_database_url, data_root=tmp_path / "forge-data")
    app = create_app(settings, session_factory=session_factory)
    receipt = await app.state.epic_lifecycle_service.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="process-cancel",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    assert receipt.state == "CANCEL_REQUESTED"
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            settings.model_copy(update={"process_role": "worker"}),
            handlers={},
            stop_event=stop,
            poll_interval=0.05,
        )
    )
    try:
        for _ in range(100):
            projection = await app.state.epic_lifecycle_service.get(epic_id, child.execution_id)
            if projection.control_state == "BLOCKED":
                break
            await asyncio.sleep(0.05)
        assert projection.control_state == "BLOCKED"
        assert projection.intents[0].status == "refused"
        assert projection.children[0].run_state.value == "MERGING"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


@pytest.mark.integration
async def test_http_api_and_separate_worker_process_replay_gate_pause_resume(
    session_factory, bridge_factory, migrated_database_url, tmp_path
):
    import os
    import socket
    import subprocess
    import sys
    from datetime import UTC, datetime
    from pathlib import Path

    from forge.application.services.auth import AuthService
    from forge.domain.approval import ApprovalGate
    from forge.domain.run import RunState

    bridge, _, _, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    bootstrap = await AuthService(lambda: PostgresUnitOfWork(session_factory)).issue_bootstrap()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    for key in (
        "FORGE_PROVIDER_SECRET_REFERENCE",
        "FORGE_GOOGLE_API_KEY_REFERENCE",
        "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH",
    ):
        environment.pop(key, None)
    environment.update(
        FORGE_DATABASE_URL=migrated_database_url,
        FORGE_API_PORT=str(port),
        FORGE_WEB_ORIGIN=origin,
        FORGE_DATA_ROOT=str(tmp_path / "data"),
    )
    repo_root = Path(__file__).resolve().parents[4]
    api_log = (tmp_path / "api.log").open("w", encoding="utf-8")
    worker_log = (tmp_path / "worker.log").open("w", encoding="utf-8")
    api_process = None
    worker_process = None

    def launch_process(entry: str, stream):
        return subprocess.Popen(
            [sys.executable, "-c", f"from {entry} import run; run()"],
            cwd=repo_root,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def stop_process(process):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)

    async def wait_for_state(client, execution_id, expected, *, worker):
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            assert worker.poll() is None, "worker process exited before control settled"
            response = await client.get(f"/api/epics/{epic_id}/executions/{execution_id}")
            assert response.status_code == 200
            projection = response.json()
            if projection["control_state"] == expected:
                return projection
            await asyncio.sleep(0.1)
        raise AssertionError(f"execution control did not reach {expected}")

    try:
        api_process = launch_process("forge.api.main", api_log)
        async with AsyncClient(base_url=origin, timeout=3) as client:
            for _ in range(100):
                if api_process.poll() is not None:
                    raise AssertionError("API process exited during startup")
                try:
                    health = await client.get("/api/health")
                    if health.status_code == 200:
                        break
                except ConnectError, ConnectTimeout:
                    await asyncio.sleep(0.1)
                    continue
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("API HTTP process did not become ready")
            login = await client.post(
                "/api/auth/bootstrap",
                headers={"Origin": origin},
                json={"token": bootstrap},
            )
            assert login.status_code == 200
            headers = {
                "Origin": origin,
                "X-CSRF-Token": login.json()["csrf_token"],
            }
            started = await client.post(
                f"/api/epics/{epic_id}/executions",
                headers={**headers, "Idempotency-Key": "process-start"},
                json={"expected_epic_version": 5},
            )
            assert started.status_code == 201
            execution_id = started.json()["execution_id"]
            actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
            child = await bridge.launch(
                actor=actor,
                epic_id=epic_id,
                idempotency_key="process-gate-child",
                request=request(execution_id=execution_id),
            )
            async with bridge_factory() as work:
                start_command = await work.session.scalar(
                    select(RunCommand).where(RunCommand.run_id == child.run_id)
                )
                assert start_command is not None
                start_command.status = "COMPLETED"
                start_command.completed_at = datetime.now(UTC)
                run = await work.runs.get(child.run_id)
                planning = await work.runs.transition(
                    child.run_id, run.version, RunState.PLANNING, "run.planning", {}
                )
                await work.runs.await_approval(
                    child.run_id,
                    planning.version,
                    ApprovalGate.PLAN,
                    "a" * 64,
                    "run.awaiting_plan_approval",
                    {},
                )
                await work.commit()
            pause = await client.post(
                f"/api/epics/{epic_id}/executions/{execution_id}/commands",
                headers={**headers, "Idempotency-Key": "process-pause"},
                json={"expected_execution_version": 1, "action": "pause"},
            )
            assert pause.status_code == 202 and pause.json()["state"] == "PAUSE_REQUESTED"
            worker_process = launch_process("forge.worker.main", worker_log)
            paused = await wait_for_state(client, execution_id, "PAUSED", worker=worker_process)
            assert paused["children"][0]["attempt"]["run_id"] == str(child.run_id)
            assert paused["children"][0]["pending_gate"] is None
            assert paused["children"][0]["retained_gate"] == "plan"
            await stop_process(worker_process)
            worker_process = None
            replay = await client.post(
                f"/api/epics/{epic_id}/executions/{execution_id}/commands",
                headers={**headers, "Idempotency-Key": "process-pause"},
                json={"expected_execution_version": 1, "action": "pause"},
            )
            assert replay.status_code == 202 and replay.json() == pause.json()
            resume = await client.post(
                f"/api/epics/{epic_id}/executions/{execution_id}/commands",
                headers={**headers, "Idempotency-Key": "process-resume"},
                json={"expected_execution_version": 2, "action": "resume"},
            )
            assert resume.status_code == 202 and resume.json()["state"] == "RESUME_REQUESTED"
            worker_process = launch_process("forge.worker.main", worker_log)
            active = await wait_for_state(client, execution_id, "ACTIVE", worker=worker_process)
            assert active["children"][0]["attempt"]["run_id"] == str(child.run_id)
            assert active["children"][0]["pending_gate"] == "plan"
            assert active["children"][0]["retained_gate"] is None
            async with bridge_factory() as work:
                commands = (
                    await work.session.scalars(
                        select(RunCommand).where(RunCommand.run_id == child.run_id)
                    )
                ).all()
                assert sum(command.command_type == "pause" for command in commands) == 1
                assert sum(command.command_type == "resume" for command in commands) == 1
                await work.commit()
    finally:
        await stop_process(worker_process)
        await stop_process(api_process)
        api_log.close()
        worker_log.close()


@pytest.mark.asyncio
async def test_owner_budget_edit_is_versioned_and_replays_original_receipt(
    session_factory, bridge_factory
):
    _, _, actor, epic_id, _, _, _ = await setup(session_factory, bridge_factory)
    service = EpicBudgetService(bridge_factory)
    first = await service.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="ceiling",
        request=EpicBudgetEdit(
            expected_version=0, ceiling=TaskBudget(max_provider_attempts=2, max_cost_minor=100)
        ),
    )
    second = await service.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="lower",
        request=EpicBudgetEdit(
            expected_version=1, ceiling=TaskBudget(max_provider_attempts=1, max_cost_minor=None)
        ),
    )
    assert second.version == 2 and second.ceiling.max_cost_minor is None
    assert (
        await service.edit(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="ceiling",
            request=EpicBudgetEdit(
                expected_version=0, ceiling=TaskBudget(max_provider_attempts=2, max_cost_minor=100)
            ),
        )
        == first
    )


@pytest.mark.asyncio
async def test_owner_can_unset_shared_cap_without_erasing_child_hold(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first-cap", request=request()
    )
    budget = EpicBudgetService(bridge_factory)
    before = await budget.get(epic_id)
    assert before.held["provider_attempts"] == 1
    edited = await budget.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="unset-cap",
        request=EpicBudgetEdit(
            expected_version=before.version,
            ceiling=before.ceiling,
            disabled_dimensions=("provider_attempts", "duration_ms"),
        ),
    )
    assert edited.disabled_dimensions == ("duration_ms", "provider_attempts")
    after = await budget.get(epic_id)
    assert after.disabled_dimensions == edited.disabled_dimensions
    assert after.held == before.held and after.unknown
    async with bridge_factory() as work:
        epic = await work.epics.get(epic_id, for_update=True)
        blockers = await work.epic_run_bridge.child_budget_blockers(
            epic_id,
            epic.project_id,
            ceiling=before.ceiling,
            hold=TaskBudget(max_provider_attempts=1, max_duration_seconds=300, max_tool_calls=25),
        )
        await work.commit()
    assert "epic_budget_provider_attempts_exhausted" not in blockers
    assert "child_usage_unproved" in blockers
    replay = await budget.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="unset-cap",
        request=EpicBudgetEdit(
            expected_version=before.version,
            ceiling=before.ceiling,
            disabled_dimensions=("provider_attempts", "duration_ms"),
        ),
    )
    assert replay == edited and first.run_id != epic_id


@pytest.mark.integration
async def test_owner_launch_permit_reaches_one_actual_child_admission(
    session_factory, bridge_factory
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    budget = EpicBudgetService(bridge_factory)
    await budget.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="launch-zero-cap",
        request=EpicBudgetEdit(expected_version=0, ceiling=TaskBudget(max_provider_attempts=0)),
    )
    with pytest.raises(EpicLaunchConflict, match="epic_budget_provider_attempts_exhausted"):
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="launch-default", request=request()
        )
    assert (await budget.get(epic_id)).permits == ()
    owner_request = request(owner_override=True, override_note="Admit this child once")
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="launch-owner", request=owner_request
    )
    assert "epic_budget_provider_attempts_exhausted" in child.blocker_codes
    before = await budget.get(epic_id)
    assert len(before.permits) == 1
    assert before.permits[0].run_id == child.run_id
    assert before.permits[0].actor_id == actor.actor_id
    assert before.permits[0].note == "Admit this child once"
    assert before.permits[0].consumed_attempt_id is None
    assert "epic_budget_provider_attempts_exhausted" in before.permits[0].warnings
    assert any(
        action.event_type == "epic.budget_admission_permitted"
        and action.actor_id == actor.actor_id
        and action.note == "Admit this child once"
        for action in before.owner_actions
    )

    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="owner-launch-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
    )
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admitted = await executor.admit_next("owner-launch", reservation)
    assert admitted is not None and admitted.attempt.attempt_number == 1
    after = await budget.get(epic_id)
    assert len(after.permits) == 1
    assert after.permits[0].consumed_attempt_id == admitted.attempt.attempt_id
    replay = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="launch-owner", request=owner_request
    )
    assert replay == child
    assert len((await budget.get(epic_id)).permits) == 1

    async with bridge_factory() as work:
        await work.subscription_budget.settle_attempt(
            child.run_id,
            task,
            admitted.attempt.attempt_id,
            AttemptTelemetry(
                duration_ms=1,
                tool_call_count=0,
                named_check_count=0,
                input_tokens=0,
                output_tokens=0,
                estimated_api_cost_minor=0,
                quota_status=QuotaStatus.OK,
            ),
        )
        assert await work.subscription_budget.try_debit_repair(
            child.run_id, task, admitted.attempt.attempt_id
        )
        await work.scheduler.finish(admitted.lease, successful=False)
        attempt = await work.session.get(SubscriptionAttempt, admitted.attempt.attempt_id)
        assert attempt is not None
        attempt.status = "terminal"
        logical = await work.session.get(SubscriptionTask, task)
        assert logical is not None
        logical.state = "queued"
        logical.version += 1
        await work.commit()
    assert await executor.admit_next("owner-launch-retry", reservation) is None
    assert len((await budget.get(epic_id)).permits) == 1


@pytest.mark.integration
async def test_owner_launch_unknown_child_hold_reaches_actual_admission(
    session_factory, bridge_factory
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="unknown-first", request=request()
    )
    with pytest.raises(EpicLaunchConflict, match="child_usage_unproved"):
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="unknown-default",
            request=request(execution_id=first.execution_id),
        )
    second = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="unknown-owner",
        request=request(execution_id=first.execution_id, owner_override=True),
    )
    assert "child_usage_unproved" in second.blocker_codes
    budget = EpicBudgetService(bridge_factory)
    before = await budget.get(epic_id)
    assert before.unknown and len(before.permits) == 1
    assert before.permits[0].run_id == second.run_id
    async with bridge_factory() as work:
        run = await work.runs.get(second.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            second.run_id,
            provider="p",
            worktree="unknown-owner-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admitted = await executor.admit_next(
        "unknown-owner", TaskBudget(max_provider_attempts=1, max_repairs=0)
    )
    assert admitted is not None and admitted.attempt.attempt_number == 1
    after = await budget.get(epic_id)
    assert after.unknown and "child_usage_unproved" in after.warnings
    assert after.permits[0].consumed_attempt_id == admitted.attempt.attempt_id


@pytest.mark.integration
async def test_internal_child_claim_obeys_edited_epic_cap_and_one_owner_permit(
    session_factory, bridge_factory
):
    from datetime import timedelta

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
        max_input_tokens=100,
        max_output_tokens=40,
        max_cost_minor=20,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="claim-child", request=request()
    )
    budget = EpicBudgetService(bridge_factory)
    current = await budget.get(epic_id)
    lowered = await budget.edit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="claim-low-cap",
        request=EpicBudgetEdit(
            expected_version=current.version,
            ceiling=TaskBudget(max_provider_attempts=0),
        ),
    )
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="epic-claim-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    async with bridge_factory() as held:
        await held.epics.get(epic_id, for_update=True)
        pending_claim = asyncio.create_task(
            executor.admit_next("default", reservation, lease_for=timedelta(seconds=30))
        )
        await asyncio.sleep(0.05)
        assert not pending_claim.done()
        await held.commit()
    assert await asyncio.wait_for(pending_claim, 2) is None
    async with bridge_factory() as work:
        assert not (
            await work.session.scalars(
                select(SubscriptionAttempt).where(SubscriptionAttempt.task_row_id == task)
            )
        ).all()
        await work.commit()
    permitted = await budget.permit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="claim-permit",
        request=EpicBudgetPermitRequest(
            expected_version=lowered.version,
            run_id=child.run_id,
        ),
    )
    assert "epic_budget_provider_attempts_exhausted" in permitted.warnings
    admission = await executor.admit_next("owner", reservation, lease_for=timedelta(seconds=30))
    assert admission is not None and admission.attempt.attempt_number == 1
    after = await budget.get(epic_id)
    assert after.permits[0].consumed_attempt_id == admission.attempt.attempt_id
    assert after.held["provider_attempts"] == 1

    # Settle a real first reservation and queue the same task's second attempt.
    async with bridge_factory() as work:
        await work.subscription_budget.settle_attempt(
            child.run_id,
            task,
            admission.attempt.attempt_id,
            AttemptTelemetry(
                duration_ms=1,
                tool_call_count=0,
                named_check_count=0,
                input_tokens=0,
                output_tokens=0,
                estimated_api_cost_minor=0,
                quota_status=QuotaStatus.OK,
            ),
        )
        assert await work.subscription_budget.try_debit_repair(
            child.run_id, task, admission.attempt.attempt_id
        )
        await work.scheduler.finish(admission.lease, successful=False)
        attempted = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        attempted.status = "terminal"
        logical = await work.session.get(SubscriptionTask, task)
        logical.state = "queued"
        logical.version += 1
        await work.commit()
    measured = await budget.get(epic_id)
    assert measured.known["provider_attempts"] == 1
    assert measured.known["estimated_api_cost_minor"] == 0
    assert measured.held["provider_attempts"] == 0
    assert not measured.unknown
    # Ordinary gateway evidence in the same run is an independent execution;
    # subscription consumption remains counted once from its reservation.
    from forge.persistence.models import AgentExecution, ModelUsage

    ordinary_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(
            id=ordinary_id, run_id=child.run_id, role="planner", instruction_version="v1",
            provider="fixture", model="ordinary", status="SUCCEEDED",
        ))
        session.add(ModelUsage(
            run_id=child.run_id, agent_execution_id=ordinary_id,
            provider="fixture", model="ordinary", prompt_version="v1",
            input_tokens=7, output_tokens=2, duration_ms=3,
            pricing_version="v1", estimated_cost_minor=0, currency="USD",
        ))
    mixed = await budget.get(epic_id)
    assert mixed.known["provider_attempts"] == 2
    assert mixed.known["input_tokens"] == 7
    assert not mixed.unknown
    assert await executor.admit_next("retry-default", reservation) is None
    retry_permit = await budget.permit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="retry-permit",
        request=EpicBudgetPermitRequest(
            expected_version=measured.version,
            run_id=child.run_id,
        ),
    )
    assert "epic_budget_provider_attempts_exhausted" in retry_permit.warnings
    retried = await executor.admit_next("retry-owner", reservation)
    assert retried is not None and retried.attempt.attempt_number == 2
    final = await budget.get(epic_id)
    assert final.known["provider_attempts"] == 2
    assert final.held["provider_attempts"] == 1
    assert final.permits[1].consumed_attempt_id == retried.attempt.attempt_id


@pytest.mark.integration
@pytest.mark.parametrize("missing", ("currency", "admission"))
async def test_positive_child_cost_requires_persisted_lineage_for_next_retry(
    session_factory, bridge_factory, missing
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus
    from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
    from forge.persistence.models.subscription_quota import SubscriptionQuotaAdmission

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="unproved-child", request=request()
    )
    budget = EpicBudgetService(bridge_factory)
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
        max_input_tokens=100,
        max_output_tokens=40,
        max_cost_minor=20,
    )
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="unproved-cost-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    first = await executor.admit_next("first-cost", reservation)
    assert first is not None
    async with bridge_factory() as work:
        await work.subscription_budget.settle_attempt(
            child.run_id,
            task,
            first.attempt.attempt_id,
            AttemptTelemetry(
                duration_ms=1,
                input_tokens=0,
                output_tokens=0,
                estimated_api_cost_minor=7,
                currency="USD" if missing == "admission" else None,
                quota_status=QuotaStatus.OK,
            ),
        )
        if missing == "admission":
            admission_row = await work.session.get(
                SubscriptionQuotaAdmission, first.attempt.attempt_id
            )
            assert admission_row is not None
            await work.session.delete(admission_row)
        assert await work.subscription_budget.try_debit_repair(
            child.run_id, task, first.attempt.attempt_id
        )
        await work.scheduler.finish(first.lease, successful=False)
        attempt = await work.session.get(SubscriptionAttempt, first.attempt.attempt_id)
        attempt.status = "terminal"
        logical = await work.session.get(SubscriptionTask, task)
        logical.state = "queued"
        logical.version += 1
        await work.commit()
    projection = await budget.get(epic_id)
    assert projection.known["estimated_api_cost_minor"] == 0
    assert projection.held["estimated_api_cost_minor"] >= 7
    assert projection.currency is None and projection.unknown
    assert "child_cost_lineage_unproved" in projection.warnings
    assert await executor.admit_next("retry-default", reservation) is None
    permit = await budget.permit(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="unproved-retry-permit",
        request=EpicBudgetPermitRequest(
            expected_version=projection.version,
            run_id=child.run_id,
        ),
    )
    assert "epic_usage_unknown" in permit.warnings
    retried = await executor.admit_next("retry-owner", reservation)
    assert retried is not None and retried.attempt.attempt_number == 2
    after = await budget.get(epic_id)
    assert after.permits[0].consumed_attempt_id == retried.attempt.attempt_id
    assert after.known["estimated_api_cost_minor"] == 0
    assert after.currency is None and after.unknown


@pytest.mark.integration
async def test_proven_positive_child_cost_keeps_unit_until_conflicting_currency(
    session_factory, bridge_factory
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus
    from forge.persistence.models.epic_brainstorm import BrainstormJobRow
    from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="money-start", expected_epic_version=5
    )
    budget = EpicBudgetService(bridge_factory)
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
        max_input_tokens=100,
        max_output_tokens=40,
        max_cost_minor=20,
    )
    for index, currency in enumerate(("USD", "EUR")):
        child = await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key=f"money-child-{index}",
            request=request(
                execution_id=execution.execution_id,
                owner_override=index > 0,
            ),
        )
        async with bridge_factory() as work:
            run = await work.runs.get(child.run_id)
            primary = await _admit_run(work, run, (_route("p"), _route("p")))
            task = await _enqueue(
                work,
                child.run_id,
                provider="p",
                worktree=f"money-tree-{index}",
                parent_id=primary,
                paths=("apps",),
            )
            await work.commit()
        admitted = await executor.admit_next(f"money-{index}", reservation)
        assert admitted is not None and admitted.attempt.run_id == child.run_id
        async with bridge_factory() as work:
            await work.subscription_budget.settle_attempt(
                child.run_id,
                task,
                admitted.attempt.attempt_id,
                AttemptTelemetry(
                    duration_ms=1,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_api_cost_minor=25 if index == 0 else 6,
                    currency=currency,
                    quota_status=QuotaStatus.OK,
                ),
            )
            await work.commit()
        projection = await budget.get(epic_id)
        if index == 0:
            assert projection.known["estimated_api_cost_minor"] == 25
            assert projection.currency == "USD"
            assert not projection.unknown
        else:
            assert projection.known["estimated_api_cost_minor"] == 0
            assert projection.currency is None and projection.unknown
            assert "epic_cost_currency_conflict" in projection.warnings

    authoring, submitted, project_id = await submit_authoring_for_child(
        session_factory, child=child, actor=actor, epic_id=epic_id, provider="p"
    )
    async with session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("currency-default") is None
        row = await session.get(BrainstormJobRow, submitted.job_id)
        assert row is not None and row.failure == "budget_exhausted"
    failed = await authoring.observe(
        epic_id=epic_id, project_id=project_id, job_id=submitted.job_id
    )
    await authoring.retry(
        epic_id=epic_id,
        project_id=project_id,
        job_id=submitted.job_id,
        expected_job_version=failed.job_version,
        actor=actor,
        key="currency-owner-retry",
        owner_override=True,
    )
    async with session_factory() as session, session.begin():
        admitted = await PostgresBrainstormRepository(session).claim("currency-owner")
        assert admitted is not None and admitted[0].id == submitted.job_id


@pytest.mark.integration
@pytest.mark.parametrize("permit_source", ("manual", "launch"))
async def test_owner_epic_permit_preserves_real_provider_quota_refusal(
    session_factory, bridge_factory, permit_source
):
    from datetime import UTC, datetime, timedelta

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.provider_quota import QuotaExhaustion
    from forge.domain.subscription_quota import QuotaPoolKey
    from forge.persistence.models.subscription import SubscriptionAttempt
    from forge.persistence.models.subscription_quota import SubscriptionQuotaPool

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    budget = EpicBudgetService(bridge_factory)
    before = await budget.get(epic_id)
    if permit_source == "launch":
        lowered = await budget.edit(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="quota-low-cap",
            request=EpicBudgetEdit(
                expected_version=before.version,
                ceiling=TaskBudget(max_provider_attempts=0),
            ),
        )
        child = await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="quota-owner-child",
            request=request(owner_override=True),
        )
    else:
        child = await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="quota-child", request=request()
        )
        before = await budget.get(epic_id)
        lowered = await budget.edit(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="quota-low-cap",
            request=EpicBudgetEdit(
                expected_version=before.version,
                ceiling=TaskBudget(max_provider_attempts=0),
            ),
        )
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="epic-quota-tree",
            parent_id=primary,
            paths=("apps",),
        )
        now = datetime.now(UTC)
        await work.quota.report_exhaustion(
            QuotaPoolKey("p", "local", "subscription-allowance_only"),
            QuotaExhaustion(now, "operator_report", now + timedelta(hours=2)),
            actor_id=actor.actor_id,
            idempotency_key="epic-quota-report",
        )
        await work.commit()
    if permit_source == "manual":
        permit = await budget.permit(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="quota-permit",
            request=EpicBudgetPermitRequest(expected_version=lowered.version, run_id=child.run_id),
        )
        assert "epic_budget_provider_attempts_exhausted" in permit.warnings
    else:
        assert "epic_budget_provider_attempts_exhausted" in child.blocker_codes
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    assert await executor.admit_next("quota-blocked", TaskBudget()) is None
    after = await budget.get(epic_id)
    assert after.permits[0].consumed_attempt_id is None
    assert after.known["provider_attempts"] == 0
    assert after.held["provider_attempts"] == 1  # The bridge hold survives provider refusal.
    async with bridge_factory() as work:
        assert not (
            await work.session.scalars(
                select(SubscriptionAttempt).where(SubscriptionAttempt.task_row_id == task)
            )
        ).all()
        pool = await work.session.get(
            SubscriptionQuotaPool,
            ("p", "local", "subscription-allowance_only"),
            with_for_update=True,
        )
        assert pool is not None and pool.blocked
        pool.blocked = False
        pool.next_eligible_at = None
        pool.reset_at = None
        pool.revision += 1
        await work.commit()
    admitted = await executor.admit_next(
        "quota-recovered", TaskBudget(max_provider_attempts=1, max_repairs=0)
    )
    assert admitted is not None and admitted.attempt.attempt_number == 1
    final = await budget.get(epic_id)
    assert len(final.permits) == 1
    assert final.permits[0].consumed_attempt_id == admitted.attempt.attempt_id


@pytest.mark.integration
async def test_terminal_child_hold_waits_for_command_quiescence(session_factory, bridge_factory):
    from datetime import UTC, datetime

    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="held-child", request=request()
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        await work.runs.transition(
            child.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
        )
        await work.commit()
    assert await controls.reconcile_one_hold() is None
    async with bridge_factory() as work:
        hold = await work.session.get(EpicChildBudgetHold, child.attempt_id)
        assert not hold.effects_settled
        command = await work.session.scalar(
            select(RunCommand).where(RunCommand.run_id == child.run_id)
        )
        assert command is not None and command.status == "PENDING"
        command.status = "CANCELLED"
        command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one_hold() == child.attempt_id
    assert await controls.reconcile_one_hold() is None
    async with bridge_factory() as work:
        hold = await work.session.get(EpicChildBudgetHold, child.attempt_id)
        assert hold.effects_settled
        await work.commit()


@pytest.mark.integration
async def test_terminal_child_pending_effect_is_sequence_blocker_independent_of_budget(
    session_factory, bridge_factory
):
    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="effect-first", request=request()
    )
    async with bridge_factory() as work:
        run = await work.runs.get(first.run_id)
        await work.runs.transition(
            first.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
        )
        hold = await work.session.get(EpicChildBudgetHold, first.attempt_id)
        assert hold is not None
        hold.effects_settled = True  # Budget evidence cannot imply effect settlement.
        await work.commit()
    with pytest.raises(EpicLaunchConflict) as refused:
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="effect-default", request=request()
        )
    assert "child_effects_unsettled" in refused.value.blocker_codes
    forced = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="effect-owner",
        request=request(owner_override=True, execution_id=first.execution_id),
    )
    assert "child_effects_unsettled" in forced.blocker_codes


@pytest.mark.integration
async def test_known_cancelled_child_requires_owner_after_effects_settle(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.run import RunState
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold
    from forge.persistence.models.subscription import SubscriptionAttempt

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="known-effect-first", request=request()
    )
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
    )
    async with bridge_factory() as work:
        run = await work.runs.get(first.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            first.run_id,
            provider="p",
            worktree="known-effect-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admitted = await executor.admit_next("known-effect", reservation)
    assert admitted is not None
    async with bridge_factory() as work:
        await work.subscription_budget.settle_attempt(
            first.run_id,
            task,
            admitted.attempt.attempt_id,
            AttemptTelemetry(
                duration_ms=1,
                tool_call_count=0,
                named_check_count=0,
                input_tokens=0,
                output_tokens=0,
                estimated_api_cost_minor=0,
                quota_status=QuotaStatus.OK,
            ),
        )
        await work.scheduler.finish(admitted.lease, successful=True)
        attempt = await work.session.get(SubscriptionAttempt, admitted.attempt.attempt_id)
        assert attempt is not None
        attempt.status = "terminal"
        run = await work.runs.get(first.run_id)
        await work.runs.transition(
            first.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
        )
        hold = await work.session.get(EpicChildBudgetHold, first.attempt_id)
        assert hold is not None
        hold.effects_settled = True
        await work.commit()
    with pytest.raises(EpicLaunchConflict) as refused:
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="known-effect-blocked",
            request=request(),
        )
    assert "child_effects_unsettled" in refused.value.blocker_codes
    async with bridge_factory() as work:
        for command in (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))
        ).all():
            command.status = "COMPLETED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    async with bridge_factory() as work:
        await work.runs.get_for_update(first.run_id)
        quiescence = await work.runs.prove_quiescent(first.run_id)
        assert quiescence.is_quiescent, quiescence
    with pytest.raises(EpicLaunchConflict) as permanent:
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="known-effect-next-default",
            request=request(execution_id=first.execution_id),
        )
    assert "predecessor_failed" in permanent.value.blocker_codes
    next_child = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="known-effect-next-owner",
        request=request(execution_id=first.execution_id, owner_override=True),
    )
    assert next_child.attempt_id != first.attempt_id
    assert next_child.owner_override and next_child.override_note is None
    assert "predecessor_failed" in next_child.blocker_codes


@pytest.mark.integration
async def test_superseded_control_never_enqueues_a_child_command(session_factory, bridge_factory):
    from forge.persistence.models.epic_run_bridge import EpicExecutionControl

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="fence-child", request=request()
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="fence-pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    async with bridge_factory() as work:
        await work.epics.get(epic_id, for_update=True)
        control = await work.session.get(
            EpicExecutionControl, child.execution_id, with_for_update=True
        )
        assert control is not None
        control.version += 1
        await work.commit()
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    projection = await controls.get(epic_id, child.execution_id)
    assert projection.intents[0].status == "superseded"
    async with bridge_factory() as work:
        commands = (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == child.run_id))
        ).all()
        assert len(commands) == 1  # The original start command only.


@pytest.mark.integration
async def test_control_enqueue_retains_epic_fence_while_waiting_for_run(
    session_factory, bridge_factory
):
    from forge.persistence.models.epic_run_bridge import EpicExecutionControl

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="race-child", request=request()
    )
    entering = asyncio.Event()

    class ObservedCommands(RunCommandService):
        async def enqueue_in_work(self, **kwargs):
            entering.set()
            return await super().enqueue_in_work(**kwargs)

    controls = EpicLifecycleService(bridge_factory, commands=ObservedCommands(bridge_factory))
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="race-pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )

    async def supersede():
        async with bridge_factory() as work:
            await work.epics.get(epic_id, for_update=True)
            control = await work.session.get(
                EpicExecutionControl, child.execution_id, with_for_update=True
            )
            assert control is not None
            control.version += 1
            await work.commit()

    async with bridge_factory() as holder:
        await holder.runs.get_for_update(child.run_id)
        reconciling = asyncio.create_task(controls.reconcile_one())
        await asyncio.wait_for(entering.wait(), 5)
        superseding = asyncio.create_task(supersede())
        await asyncio.sleep(0.1)
        assert not superseding.done()  # Epic lock remains held through enqueue.
        await holder.commit()
    assert await asyncio.wait_for(reconciling, 5) == receipt.intent_ids[0]
    await asyncio.wait_for(superseding, 5)
    async with bridge_factory() as work:
        commands = (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == child.run_id))
        ).all()
        assert len(commands) == 2


@pytest.mark.integration
async def test_already_paused_sibling_is_observed_and_later_resume_blocks_aggregate(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="sibling-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="sibling-first",
        request=request(execution_id=execution.execution_id),
    )
    second = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="sibling-second",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    async with bridge_factory() as work:
        for child in (first, second):
            start = await work.session.scalar(
                select(RunCommand).where(RunCommand.run_id == child.run_id)
            )
            assert start is not None
            start.status = "COMPLETED"
            start.completed_at = datetime.now(UTC)
            run = await work.runs.get(child.run_id)
            await work.runs.transition(
                child.run_id, run.version, RunState.PLANNING, "run.planning", {}
            )
        first_run = await work.runs.get(first.run_id)
        await work.runs.pause(first.run_id, first_run.version, "run.paused", {})
        await work.commit()
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="sibling-pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert len(receipt.intent_ids) == 1
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    async with bridge_factory() as work:
        first_run = await work.runs.get(first.run_id)
        await work.runs.resume(first.run_id, first_run.version, "run.resumed", {})
        second_run = await work.runs.get(second.run_id)
        await work.runs.pause(second.run_id, second_run.version, "run.paused", {})
        command_id = next(intent.command_id for intent in
                          (await controls.get(epic_id, execution.execution_id)).intents
                          if intent.intent_id == receipt.intent_ids[0])
        command = await work.session.get(RunCommand, command_id)
        assert command is not None
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    projection = await controls.get(epic_id, execution.execution_id)
    assert next(intent for intent in projection.intents if intent.intent_id == receipt.intent_ids[0]).status == "settled"
    assert projection.control_state == "BLOCKED"


@pytest.mark.integration
@pytest.mark.parametrize("authoring_kind", ("brainstorm", "decomposition"))
async def test_child_unknown_blocks_authoring_claim_until_direct_owner_retry(
    session_factory, bridge_factory, authoring_kind
):
    from forge.persistence.models.epic_brainstorm import BrainstormJobRow
    from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="authoring-child", request=request()
    )
    authoring, submitted, project_id = await submit_authoring_for_child(
        session_factory, child=child, actor=actor, epic_id=epic_id, kind=authoring_kind
    )
    async with session_factory() as session, session.begin():
        assert (
            await PostgresBrainstormRepository(session).claim(
                "default-authoring", kinds=frozenset((authoring_kind,))
            )
            is None
        )
        row = await session.get(BrainstormJobRow, submitted.job_id)
        assert row is not None and row.state == "failed" and row.current_attempt_id is None
    failed = await authoring.observe(
        epic_id=epic_id, project_id=project_id, job_id=submitted.job_id
    )
    recovered = await authoring.retry(
        epic_id=epic_id,
        project_id=project_id,
        job_id=submitted.job_id,
        expected_job_version=failed.job_version,
        actor=actor,
        key="owner-authoring-retry",
        owner_override=True,
    )
    assert recovered.state == "queued"
    async with session_factory() as session, session.begin():
        admitted = await PostgresBrainstormRepository(session).claim(
            "owner-authoring", kinds=frozenset((authoring_kind,))
        )
        assert admitted is not None and admitted[0].id == submitted.job_id
        assert admitted[1].number == 1


@pytest.mark.integration
@pytest.mark.parametrize("missing_token", (False, True))
async def test_known_child_attempt_consumes_authoring_ceiling_with_same_provider(
    session_factory, bridge_factory, missing_token
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import AttemptTelemetry, QuotaStatus, encode_subscription_record
    from forge.persistence.models.epic_brainstorm import BrainstormBudgetLedger, BrainstormJobRow
    from forge.persistence.models.subscription import SubscriptionAttempt
    from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="shared-child", request=request()
    )
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
    )
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="shared-attempt-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    first = await executor.admit_next("shared-child-attempt", reservation)
    assert first is not None
    async with bridge_factory() as work:
        await work.subscription_budget.settle_attempt(
            child.run_id,
            task,
            first.attempt.attempt_id,
            AttemptTelemetry(
                duration_ms=1,
                tool_call_count=0,
                named_check_count=0,
                input_tokens=None if missing_token else 0,
                output_tokens=0,
                estimated_api_cost_minor=0,
                quota_status=QuotaStatus.OK,
            ),
        )
        attempt = await work.session.get(SubscriptionAttempt, first.attempt.attempt_id)
        assert attempt is not None
        attempt.status = "terminal"
        ledger = await work.session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
        assert ledger is not None
        ledger.ceiling = encode_subscription_record(TaskBudget(max_provider_attempts=1))
        ledger.version += 1
        await work.commit()
    projection = await EpicBudgetService(bridge_factory).get(epic_id)
    if missing_token:
        assert projection.unknown and "child_usage_unproved" in projection.warnings
    else:
        assert not projection.unknown and projection.known["provider_attempts"] == 1
    authoring, submitted, project_id = await submit_authoring_for_child(
        session_factory, child=child, actor=actor, epic_id=epic_id, provider="p"
    )
    async with session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("shared-default") is None
        row = await session.get(BrainstormJobRow, submitted.job_id)
        assert row is not None and row.failure == "budget_exhausted"
        assert row.current_attempt_id is None
    failed = await authoring.observe(
        epic_id=epic_id, project_id=project_id, job_id=submitted.job_id
    )
    assert failed.process_settled
    await authoring.retry(
        epic_id=epic_id,
        project_id=project_id,
        job_id=submitted.job_id,
        expected_job_version=failed.job_version,
        actor=actor,
        key="shared-owner-retry",
        owner_override=True,
    )
    async with session_factory() as session, session.begin():
        admitted = await PostgresBrainstormRepository(session).claim("shared-owner")
        assert admitted is not None and admitted[0].id == submitted.job_id
        assert admitted[1].number == 1
        if not missing_token:
            admitted[1].process_settled = True
            admitted[1].usage_known = True
            admitted[1].usage = {
                "duration_ms": 0,
                "tool_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_api_cost_minor": 0,
            }
            ledger = await session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
            assert ledger is not None
            ledger.ceiling = encode_subscription_record(TaskBudget(max_provider_attempts=2))
            ledger.version += 1
    if not missing_token:
        projection = await EpicBudgetService(bridge_factory).get(epic_id)
        assert projection.known["provider_attempts"] == 2
        assert projection.held["provider_attempts"] == 0
        conversation, version = await authoring.create(
            epic_id=epic_id,
            project_id=project_id,
            actor=actor,
            key="second-shared-conversation",
            text="Again",
        )
        turn = (
            await authoring.turns(
                epic_id=epic_id, project_id=project_id, conversation_id=conversation
            )
        )[-1]
        second = await authoring.submit(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation,
            prompt_turn_id=turn.turn_id,
            expected_epic_version=5,
            expected_conversation_version=version,
            actor=actor,
            key="second-shared-submit",
        )
        async with session_factory() as session, session.begin():
            assert (
                await PostgresBrainstormRepository(session).claim("second-shared-default") is None
            )
            row = await session.get(BrainstormJobRow, second.job_id)
            assert row is not None and row.failure == "budget_exhausted"
            assert row.current_attempt_id is None


@pytest.mark.integration
async def test_same_epic_provider_authoring_and_delivery_claims_finish_without_deadlock(
    session_factory, bridge_factory
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

    from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
        _admit_run,
        _enqueue,
        _route,
    )

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="concurrent-child", request=request()
    )
    await submit_authoring_for_child(
        session_factory, child=child, actor=actor, epic_id=epic_id, provider="p"
    )
    async with bridge_factory() as work:
        run = await work.runs.get(child.run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            child.run_id,
            provider="p",
            worktree="concurrent-epic-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()

    async def authoring_claim():
        async with session_factory() as session, session.begin():
            return await PostgresBrainstormRepository(session).claim("concurrent-authoring")

    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    reservation = TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
    )
    authoring_result, delivery_result = await asyncio.wait_for(
        asyncio.gather(
            authoring_claim(),
            executor.admit_next("concurrent-delivery", reservation),
        ),
        5,
    )
    assert authoring_result is None  # Child hold is still unknown by default.
    assert delivery_result is not None and delivery_result.attempt.run_id == child.run_id
    assert (await EpicBudgetService(bridge_factory).get(epic_id)).held["provider_attempts"] >= 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("history_state", "retry"),
    [("COMPLETED", False), ("FAILED", True), ("CANCELLED", True)],
)
@pytest.mark.parametrize("action", ("pause", "cancel"))
async def test_control_terminal_history_and_live_successor_preserves_history(
    session_factory, bridge_factory, history_state, retry, action
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="history-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="history-first",
        request=request(first_id, execution_id=execution.execution_id),
    )
    async with bridge_factory() as work:
        run = await work.session.get(Run, first.run_id)
        run.state = history_state
        command = await work.session.scalar(select(RunCommand).where(RunCommand.run_id == first.run_id))
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        hold = await work.session.get(EpicChildBudgetHold, first.attempt_id)
        hold.effects_settled = True
        await work.commit()
    second = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="history-second",
        request=request(first_id if retry else second_id,
                        execution_id=execution.execution_id, owner_override=True),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    receipt = await controls.request(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key=f"history-{action}",
        request=EpicControlRequest(expected_execution_version=1, action=action),
    )
    assert receipt.state == f"{action.upper()}_REQUESTED" and len(receipt.intent_ids) == 1
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    async with bridge_factory() as work:
        first_run = await work.runs.get(first.run_id)
        assert first_run.state.value == history_state
        commands = (await work.session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))).all()
        assert all(command.command_type != action for command in commands)
        second_run = await work.runs.get(second.run_id)
        if action == "pause":
            await work.runs.pause(second.run_id, second_run.version, "run.paused", {})
        else:
            await work.runs.transition(second.run_id, second_run.version, RunState.CANCELLED, "run.cancelled", {})
        for command in (await work.session.scalars(
            select(RunCommand).where(RunCommand.run_id == second.run_id)
        )).all():
            command.status = "COMPLETED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == ("PAUSED" if action == "pause" else "CANCELLED")
    assert next(child for child in projection.children if child.attempt.run_id == first.run_id).run_state.value == history_state


@pytest.mark.integration
async def test_resume_paused_successor_with_completed_history(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState

    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="resume-history-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="resume-history-first",
        request=request(first_id, execution_id=execution.execution_id),
    )
    second = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="resume-history-second",
        request=request(second_id, execution_id=execution.execution_id, owner_override=True),
    )
    async with bridge_factory() as work:
        first_run = await work.session.get(Run, first.run_id)
        first_run.state = "COMPLETED"
        for child in (first, second):
            for command in (await work.session.scalars(
                select(RunCommand).where(RunCommand.run_id == child.run_id)
            )).all():
                command.status = "COMPLETED"
                command.completed_at = datetime.now(UTC)
        second_run = await work.runs.get(second.run_id)
        await work.runs.pause(second.run_id, second_run.version, "run.paused", {})
        await work.commit()
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    paused = await controls.request(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="resume-history-paused",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert paused.state == "PAUSED" and not paused.intent_ids
    receipt = await controls.request(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="resume-history-control",
        request=EpicControlRequest(expected_execution_version=2, action="resume"),
    )
    assert receipt.state == "RESUME_REQUESTED" and len(receipt.intent_ids) == 1
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    async with bridge_factory() as work:
        second_run = await work.runs.get(second.run_id)
        await work.runs.resume(second.run_id, second_run.version, "run.resumed", {})
        command = await work.session.scalar(select(RunCommand).where(
            RunCommand.run_id == second.run_id, RunCommand.command_type == "resume"
        ))
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "ACTIVE"
    assert next(child for child in projection.children if child.attempt.run_id == first.run_id).run_state is RunState.COMPLETED


@pytest.mark.integration
async def test_resume_acknowledgement_then_terminal_child_settles_intent_but_blocks_active(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="terminal-resume-child", request=request()
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    async with bridge_factory() as work:
        start = await work.session.scalar(
            select(RunCommand).where(RunCommand.run_id == child.run_id)
        )
        assert start is not None
        start.status = "COMPLETED"
        start.completed_at = datetime.now(UTC)
        run = await work.runs.get(child.run_id)
        planning = await work.runs.transition(
            child.run_id, run.version, RunState.PLANNING, "run.planning", {}
        )
        await work.runs.pause(child.run_id, planning.version, "run.paused", {})
        await work.commit()
    pause = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="terminal-resume-paused",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert pause.state == "PAUSED" and not pause.intent_ids
    resume = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="terminal-resume-action",
        request=EpicControlRequest(expected_execution_version=2, action="resume"),
    )
    assert await controls.reconcile_one() == resume.intent_ids[0]
    async with bridge_factory() as work:
        paused = await work.runs.get(child.run_id)
        restored = await work.runs.resume(child.run_id, paused.version, "run.resumed", {})
        await work.runs.transition(
            child.run_id, restored.version, RunState.FAILED, "run.failed", {}
        )
        command_id = next(intent.command_id for intent in
                          (await controls.get(epic_id, child.execution_id)).intents
                          if intent.intent_id == resume.intent_ids[0])
        command = await work.session.get(RunCommand, command_id)
        assert command is not None
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        # A separate durable effect remains pending after the resume command
        # acknowledged and the run became terminal.
        start = await work.session.scalar(select(RunCommand).where(
            RunCommand.run_id == child.run_id, RunCommand.id != command_id
        ))
        assert start is not None
        start.status = "PENDING"
        start.completed_at = None
        await work.commit()
    assert await controls.reconcile_one() == resume.intent_ids[0]
    projection = await controls.get(epic_id, child.execution_id)
    assert next(intent for intent in projection.intents if intent.intent_id == resume.intent_ids[0]).status == "settled"
    assert projection.control_state == "BLOCKED"
    async with bridge_factory() as work:
        hold = await work.session.get(EpicChildBudgetHold, child.attempt_id)
        assert hold is not None and not hold.effects_settled
        await work.commit()


@pytest.mark.integration
async def test_cancel_observes_terminal_unsettled_sibling_with_live_child(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState
    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="mixed-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="mixed-first",
        request=request(execution_id=execution.execution_id),
    )
    sibling = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="mixed-sibling",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    async with bridge_factory() as work:
        run = await work.runs.get(sibling.run_id)
        await work.runs.transition(
            sibling.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
        )
        await work.commit()
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="mixed-cancel",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    projection = await controls.get(epic_id, execution.execution_id)
    assert receipt.state == "CANCEL_REQUESTED"
    assert {intent.run_id for intent in projection.intents} == {first.run_id, sibling.run_id}
    assert {intent.status for intent in projection.intents} == {"requested", "observing"}
    assert await controls.reconcile_one() in receipt.intent_ids
    async with bridge_factory() as work:
        run = await work.runs.get(first.run_id)
        await work.runs.transition(
            first.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
        )
        for command in (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))
        ).all():
            command.status = "COMPLETED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() in receipt.intent_ids
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "CANCEL_REQUESTED"
    async with bridge_factory() as work:
        hold = await work.session.get(EpicChildBudgetHold, sibling.attempt_id)
        assert hold is not None and not hold.effects_settled
        for command in (
            await work.session.scalars(
                select(RunCommand).where(RunCommand.run_id == sibling.run_id)
            )
        ).all():
            command.status = "CANCELLED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() in receipt.intent_ids
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "CANCELLED"
    assert all(child.effects_settled for child in projection.children)


@pytest.mark.integration
async def test_cancel_commands_both_owner_admitted_live_children_once(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="two-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="two-first",
        request=request(execution_id=execution.execution_id),
    )
    second = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="two-second",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="two-cancel",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    assert len(receipt.intent_ids) == 2
    assert {
        intent.run_id for intent in (await controls.get(epic_id, execution.execution_id)).intents
    } == {
        first.run_id,
        second.run_id,
    }
    assert await controls.reconcile_one() in receipt.intent_ids
    assert await controls.reconcile_one() in receipt.intent_ids
    async with bridge_factory() as work:
        for child in (first, second):
            run = await work.runs.get(child.run_id)
            await work.runs.transition(
                child.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
            )
            commands = (
                await work.session.scalars(
                    select(RunCommand).where(RunCommand.run_id == child.run_id)
                )
            ).all()
            assert sum(command.command_type == "cancel" for command in commands) == 1
            for command in commands:
                command.status = "COMPLETED"
                command.completed_at = datetime.now(UTC)
        await work.commit()
    for _ in range(2):
        assert await controls.reconcile_one() in receipt.intent_ids
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "CANCELLED"
    assert all(child.effects_settled for child in projection.children)
    assert {intent.status for intent in projection.intents} == {"settled"}


@pytest.mark.integration
async def test_terminal_failed_sibling_is_observed_during_pause(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="complete-start", expected_epic_version=5
    )
    live = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="complete-live",
        request=request(execution_id=execution.execution_id),
    )
    completed = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="complete-owner",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    async with bridge_factory() as work:
        run = await work.runs.get(completed.run_id)
        planning = await work.runs.transition(
            completed.run_id, run.version, RunState.PLANNING, "run.planning", {}
        )
        await work.runs.transition(
            completed.run_id, planning.version, RunState.FAILED, "run.failed", {}
        )
        await work.commit()
    pause = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="complete-pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    projection = await controls.get(epic_id, execution.execution_id)
    assert pause.state == "PAUSE_REQUESTED"
    assert {intent.run_id for intent in projection.intents} == {completed.run_id, live.run_id}
    assert {intent.status for intent in projection.intents} == {"requested", "observing"}
    assert projection.control_state == "PAUSE_REQUESTED"
    assert next(child for child in projection.children if child.attempt.run_id == completed.run_id).run_state is RunState.FAILED
    assert await controls.reconcile_one() in pause.intent_ids
    async with bridge_factory() as work:
        run = await work.runs.get(live.run_id)
        await work.runs.pause(live.run_id, run.version, "run.paused", {})
        for child in (live, completed):
            for command in (await work.session.scalars(
                select(RunCommand).where(RunCommand.run_id == child.run_id)
            )).all():
                command.status = "COMPLETED"
                command.completed_at = datetime.now(UTC)
        await work.commit()
    for _ in range(3):
        await controls.reconcile_one()
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "PAUSED"
    assert next(child for child in projection.children if child.attempt.run_id == completed.run_id).run_state is RunState.FAILED


@pytest.mark.integration
async def test_owner_child_inserted_during_cancel_keeps_terminal_unsettled_effect_blocked(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.run import RunState

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="late-start", expected_epic_version=5
    )
    first = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="late-first",
        request=request(execution_id=execution.execution_id),
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    receipt = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="late-cancel",
        request=EpicControlRequest(expected_execution_version=1, action="cancel"),
    )
    late = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="late-owner",
        request=request(execution_id=execution.execution_id, owner_override=True),
    )
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    async with bridge_factory() as work:
        for child in (first, late):
            run = await work.runs.get(child.run_id)
            await work.runs.transition(
                child.run_id, run.version, RunState.CANCELLED, "run.cancelled", {}
            )
        for command in (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))
        ).all():
            command.status = "COMPLETED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == receipt.intent_ids[0]
    projection = await controls.get(epic_id, execution.execution_id)
    assert projection.control_state == "BLOCKED"
    assert projection.blocker_code == "uncontrolled_child"
    assert not next(
        child for child in projection.children if child.attempt.run_id == late.run_id
    ).effects_settled
    async with bridge_factory() as work:
        for command in (
            await work.session.scalars(select(RunCommand).where(RunCommand.run_id == late.run_id))
        ).all():
            command.status = "CANCELLED"
            command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one_hold() == late.attempt_id
    recovered = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=execution.execution_id,
        idempotency_key="late-owner-recover",
        request=EpicControlRequest(expected_execution_version=2, action="cancel"),
    )
    assert recovered.state == "CANCELLED"


@pytest.mark.integration
async def test_child_gate_pause_resume_settles_only_after_command_observation(
    session_factory, bridge_factory
):
    from datetime import UTC, datetime

    from forge.domain.approval import ApprovalGate
    from forge.domain.run import RunState

    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="gate-child", request=request()
    )
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    async with bridge_factory() as work:
        start = await work.session.scalar(
            select(RunCommand).where(RunCommand.run_id == child.run_id)
        )
        assert start is not None
        start.status = "COMPLETED"
        start.completed_at = datetime.now(UTC)
        run = await work.runs.get(child.run_id)
        planning = await work.runs.transition(
            child.run_id, run.version, RunState.PLANNING, "run.planning", {}
        )
        gate = await work.runs.await_approval(
            child.run_id,
            planning.version,
            ApprovalGate.PLAN,
            "a" * 64,
            "run.awaiting_plan_approval",
            {},
        )
        await work.commit()
    pause = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="gate-pause",
        request=EpicControlRequest(expected_execution_version=1, action="pause"),
    )
    assert pause.state == "PAUSE_REQUESTED"
    assert await controls.reconcile_one() == pause.intent_ids[0]
    assert await controls.reconcile_one() is None
    assert (await controls.get(epic_id, child.execution_id)).control_state == "PAUSE_REQUESTED"
    async with bridge_factory() as work:
        paused = await work.runs.pause(child.run_id, gate.version, "run.paused", {})
        command = await work.session.get(
            RunCommand, (await controls.get(epic_id, child.execution_id)).intents[0].command_id
        )
        assert command is not None
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == pause.intent_ids[0]
    projection = await controls.get(epic_id, child.execution_id)
    assert projection.control_state == "PAUSED"
    assert projection.children[0].run_state is RunState.PAUSED
    assert projection.children[0].pending_gate is None
    assert projection.children[0].retained_gate is ApprovalGate.PLAN
    resume = await controls.request(
        actor=actor,
        epic_id=epic_id,
        execution_id=child.execution_id,
        idempotency_key="gate-resume",
        request=EpicControlRequest(expected_execution_version=2, action="resume"),
    )
    assert resume.state == "RESUME_REQUESTED"
    assert await controls.reconcile_one() == resume.intent_ids[0]
    async with bridge_factory() as work:
        restored = await work.runs.resume(child.run_id, paused.version, "run.resumed", {})
        command_id = (await controls.get(epic_id, child.execution_id)).intents[1].command_id
        command = await work.session.get(RunCommand, command_id)
        assert command is not None
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        await work.commit()
    assert await controls.reconcile_one() == resume.intent_ids[0]
    projection = await controls.get(epic_id, child.execution_id)
    assert projection.control_state == "ACTIVE"
    assert projection.children[0].run_state is RunState.AWAITING_PLAN_APPROVAL
    assert projection.children[0].run_version == restored.version
    assert projection.children[0].pending_gate is ApprovalGate.PLAN
    assert projection.children[0].retained_gate is None


@pytest.mark.asyncio
async def test_execution_scoped_reads_and_epic_wide_default(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )
    # Epoch 1
    execution_1 = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="start-epoch-1", expected_epic_version=5
    )
    child_1 = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="launch-child-1",
        request=request(item_id=first_id, execution_id=execution_1.execution_id),
    )
    # Epoch 2
    execution_2 = await bridge.start(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="start-epoch-2",
        expected_epic_version=5,
        owner_override=True,
        override_note="epoch 2 override",
    )
    child_2 = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="launch-child-2",
        request=request(
            item_id=second_id,
            execution_id=execution_2.execution_id,
            owner_override=True,
            override_note="launch 2",
        ),
    )

    from forge.persistence.models.epic_run_bridge import EpicExecutionControl

    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution_1.execution_id)
        assert control is not None
        await work.session.delete(control)
        for execution_fields in ({}, {"execution_id": None}, {"execution_id": str(uuid4())}):
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.execution_started",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=uuid4(),
                payload={**execution_fields, "warnings": [], "override_note": "unbound audit"},
            )
        await work.audit.append(
            actor_id=actor.actor_id,
            event_type="epic.execution_control_requested",
            subject_type="epic",
            subject_id=epic_id,
            correlation_id=uuid4(),
            payload={
                "execution_id": str(execution_2.execution_id),
                "warnings": ["historical"],
                "override_note": "later owner note",
            },
        )
        await work.commit()

    # Verify repository list_attempts with and without execution_id
    async with bridge_factory() as work:
        scoped_1 = await work.epic_run_bridge.list_attempts(
            epic_id, execution_id=execution_1.execution_id
        )
        assert [a.attempt_id for a in scoped_1] == [child_1.attempt_id]

        scoped_2 = await work.epic_run_bridge.list_attempts(
            epic_id, execution_id=execution_2.execution_id
        )
        assert [a.attempt_id for a in scoped_2] == [child_2.attempt_id]

        complete_epic_wide = await work.epic_run_bridge.list_attempts(epic_id)
        assert [a.attempt_id for a in complete_epic_wide] == [
            child_1.attempt_id,
            child_2.attempt_id,
        ]

    # Verify lifecycle service projections
    controls = EpicLifecycleService(bridge_factory, commands=RunCommandService(bridge_factory))
    proj_1 = await controls.get(epic_id, execution_1.execution_id)
    assert [c.attempt.attempt_id for c in proj_1.children] == [child_1.attempt_id]
    assert len(proj_1.owner_actions) == 1
    assert proj_1.owner_actions[0].actor_id == actor.actor_id
    assert proj_1.owner_actions[0].note is None
    assert proj_1.control_version is None
    assert proj_1.control_state is None
    assert proj_1.blocker_code is None

    proj_2 = await controls.get(epic_id, execution_2.execution_id)
    assert [c.attempt.attempt_id for c in proj_2.children] == [child_2.attempt_id]
    assert [action.note for action in proj_2.owner_actions] == [
        "epoch 2 override",
        "later owner note",
    ]
    assert all(action.actor_id == actor.actor_id for action in proj_2.owner_actions)
    assert proj_2.owner_actions[1].warnings == ("historical",)
    assert await controls.list(epic_id) == (proj_1, proj_2)


@pytest.mark.asyncio
async def test_execution_discovery_does_not_reprove_historical_readiness(
    session_factory, bridge_factory
):
    from forge.application.services.epic_dispatch import EpicDispatchService
    from forge.application.services.epic_eligibility import EpicItemEligibility

    bridge, _, actor, epic_id, item_id, _, _ = await setup(session_factory, bridge_factory)
    first = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="discover-first", expected_epic_version=5
    )
    async with bridge_factory() as work:
        second = await work.epic_run_bridge.create_execution(
            epic_id=epic_id, brief_revision_id=first.brief_revision_id,
            brief_digest=first.brief_digest, graph_revision_id=first.graph_revision_id,
            graph_digest=first.graph_digest,
        )
        await work.commit()

    class Eligibility:
        calls = 0

        async def readiness(self, *, epic_id, execution_id):
            self.calls += 1
            return (EpicItemEligibility(
                item_id=item_id, disposition="required", status="ready",
                blocker_code=None, dependency_evidence=(), completion_evidence=None,
            ),)

    class Dispatch:
        calls = 0

        async def get(self, epic_id, execution_id):
            self.calls += 1
            return await EpicDispatchService(bridge_factory).get(epic_id, execution_id)

    eligibility, dispatch = Eligibility(), Dispatch()
    controls = EpicLifecycleService(
        bridge_factory, commands=RunCommandService(bridge_factory),
        eligibility=eligibility, dispatch=dispatch,
    )
    discovered = await controls.list(epic_id)
    assert [value.execution.execution_id for value in discovered] == [
        first.execution_id, second.execution_id,
    ]
    assert eligibility.calls == dispatch.calls == 0
    assert all(value.items == () and value.dispatch is None for value in discovered)
    selected = await controls.get(epic_id, first.execution_id)
    assert eligibility.calls == dispatch.calls == 1
    assert selected.items[0].item_id == item_id
    assert selected.dispatch is not None and selected.dispatch.execution_id == first.execution_id
