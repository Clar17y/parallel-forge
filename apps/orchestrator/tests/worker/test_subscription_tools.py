"""Policy snapshots bind adapters without extending their per-call authority."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.search_ranking import SearchRankingMode
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, SpecialistPurpose
from forge.domain.tool import ToolName, repository_resource_identity
from forge.ranking.configuration import SearchRankingConfiguration
from forge.worker.subscription_tools import SubscriptionToolServiceFactory
from test_subscription_attempt_runner import invocation


def case(
    tmp_path,
    *,
    purpose=SpecialistPurpose.PRIMARY,
    planning=False,
    search_ranking=None,
    task_text=None,
):
    admission, original = invocation()
    project_id = uuid4()
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
    )
    route = replace(admission.task.route, is_primary=purpose is SpecialistPurpose.PRIMARY)
    task = replace(admission.task, purpose=purpose, route=route, owned_paths=())
    envelope = replace(admission.envelope, routes=((purpose, route),))
    admission = replace(admission, task=task, envelope=envelope)
    identity = WorktreeIdentity.for_run(project_id, task.run_id, "forge/test", False)
    run = SimpleNamespace(
        id=task.run_id,
        project_id=project_id,
        policy_version=1,
        state=RunState.PLANNING if planning else RunState.IMPLEMENTING,
        pending_gate=None,
        worktree_path=None if planning else str(tmp_path / "managed"),
        branch_name="forge/test",
        base_sha="a" * 40,
    )
    tools = (
        frozenset({ToolName.REPOSITORY_READ_FILE})
        if planning
        else SPECIALIST_ALLOWED_TOOLS[purpose]
    )
    request = replace(
        original,
        task=task,
        envelope=envelope,
        run_state=run.state,
        attempt_budget=replace(task.budget, max_provider_attempts=1, max_repairs=0),
        authorization=replace(
            original.authorization,
            role=purpose,
            permitted_tools=tools,
            worktree_id=repository_resource_identity(project_id)
            if planning
            else identity.worktree_name,
        ),
        untrusted_context={
            "repository_path": "Z:/attacker",
            "policy": {"version": 999},
            **({"task": {"text": task_text}} if task_text is not None else {}),
        },
    )
    record = SimpleNamespace(
        project_id=project_id,
        version=1,
        document_schema_version=1,
        document=policy.model_dump(mode="json"),
    )
    project = SimpleNamespace(current_policy_version=1)
    active, calls = [], []

    async def get_run(run_id):
        assert run_id == run.id
        return run

    async def get_project(value):
        assert value == project_id
        return project

    async def get_policy(*args):
        return record

    async def rollback():
        pass

    @asynccontextmanager
    async def work_factory():
        active.append(True)
        try:
            yield SimpleNamespace(
                runs=SimpleNamespace(get_for_update=get_run),
                projects=SimpleNamespace(get=get_project, get_policy=get_policy),
                rollback=rollback,
            )
        finally:
            active.pop()

    def adapter(name):
        def make(supplied, *args, **kwargs):
            assert not active, "adapter IO must not hold a DB transaction"
            assert supplied == policy
            calls.append(name)
            return name

        return make

    async def environment(supplied_run, supplied_policy, tree):
        assert not active and supplied_run is run and supplied_policy == policy
        calls.append("environment")
        return {"FORGE_TEST": "test-value"}

    delivery = SimpleNamespace(
        git=adapter("git"),
        reader=adapter("reader"),
        writer=adapter("writer"),
        environment=environment,
        operation_executor=object(),
    )
    factory = SubscriptionToolServiceFactory(
        work_factory,
        artifacts=object(),
        delivery=delivery,
        search_ranking=search_ranking,
    )
    return factory, admission, request, run, project, record, calls, delivery


@pytest.mark.parametrize(
    "purpose,writer,checks",
    [
        (SpecialistPurpose.INDEPENDENT_REVIEW, False, False),
        (SpecialistPurpose.VERIFICATION, False, True),
        (SpecialistPurpose.ROUTINE_IMPLEMENTATION, True, True),
        (SpecialistPurpose.INTEGRATION, True, True),
    ],
)
@pytest.mark.parametrize("phase", [RunState.IMPLEMENTING, RunState.REMEDIATING])
async def test_adapters_follow_permitted_tools_outside_transaction(
    tmp_path, purpose, writer, checks, phase
):
    factory, admission, request, run, _, _, calls, delivery = case(tmp_path, purpose=purpose)
    run.state = phase
    request = replace(request, run_state=phase)
    service = await factory(admission, request)
    assert service._worktree.path.as_posix() == run.worktree_path.replace("\\", "/")
    assert (service._repository_writer is not None) is writer
    assert (service._runner_factory is delivery) is checks
    assert ("writer" in calls) is writer
    assert ("environment" in calls) is checks
    assert bool(service._command_environment) is checks


async def test_planning_uses_repository_reader_without_delivery_io(tmp_path):
    factory, admission, request, _, _, _, calls, _ = case(tmp_path, planning=True)
    service = await factory(admission, request)
    assert service._repository_reader is not None
    assert service._repository_writer is service._runner_factory is service._git is None
    assert not calls and not service._command_environment


@pytest.mark.parametrize(
    "change", ["phase", "policy", "schema", "document", "resource", "tree", "admission"]
)
async def test_stale_or_foreign_binding_fails_before_adapter_io(tmp_path, change):
    factory, admission, request, run, project, record, calls, _ = case(tmp_path)
    if change == "phase":
        run.state = RunState.PAUSED
    elif change == "policy":
        project.current_policy_version = 2
    elif change == "schema":
        record.document_schema_version = 2
    elif change == "document":
        record.document["id"] = str(uuid4())
    elif change == "resource":
        request = replace(
            request, authorization=replace(request.authorization, worktree_id="foreign")
        )
    elif change == "tree":
        run.worktree_path = None
    else:
        admission = replace(admission, task=replace(admission.task, owned_paths=("foreign",)))
    with pytest.raises(ValueError):
        await factory(admission, request)
    assert not calls


async def test_planning_rejects_primary_mutation_capabilities(tmp_path):
    factory, admission, request, _, _, _, calls, _ = case(tmp_path, planning=True)
    request = replace(
        request,
        authorization=replace(
            request.authorization, permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE})
        ),
    )
    with pytest.raises(ValueError, match="planning tools"):
        await factory(admission, request)
    assert not calls


@pytest.mark.parametrize("missing", ["phase", "budget"])
async def test_factory_requires_persisted_invocation_context(tmp_path, missing):
    factory, admission, request, _, _, _, calls, _ = case(tmp_path)
    request = replace(request, **{"run_state" if missing == "phase" else "attempt_budget": None})
    with pytest.raises(ValueError, match="differs from admission"):
        await factory(admission, request)
    assert not calls


@pytest.mark.parametrize("planning", [False, True])
async def test_factory_supplies_search_ranking_and_objective(tmp_path, planning):
    ranker = object()
    ranking = SearchRankingConfiguration(mode=SearchRankingMode.ON, top_k=7, ranker=ranker)
    factory, admission, request, _, _, _, _, _ = case(
        tmp_path,
        planning=planning,
        search_ranking=ranking,
        task_text="Implement retry backoff",
    )
    assert factory._search_ranking is ranking
    service = await factory(admission, request)
    assert service._search_ranker is ranker
    assert service._search_ranking_mode is SearchRankingMode.ON
    assert service._search_ranking_top_k == 7
    assert service._search_objective == "Implement retry backoff"
