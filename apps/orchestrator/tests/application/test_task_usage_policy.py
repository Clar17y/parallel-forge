"""Owner monitoring edits preserve history and distinguish omission from clearing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.task_usage_policy import (
    MonitoringPolicyRevisionRequest,
    TaskUsagePolicyService,
    resolve_effective_monitoring_policy,
)
from forge.domain.task_usage_contract import MonitoringMode, MonitoringPolicy
from forge.persistence.models import Project
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.repositories.task_usage_policy import (
    MonitoringPolicyConflict,
    PostgresTaskUsagePolicyRepository,
)
from pydantic import ValidationError

from apps.orchestrator.tests.application.test_project_task_services import (
    FakeAudit,
    FakeInspector,
    FakeMutations,
    FakeProjects,
    FakeTasks,
    FakeUow,
)

# Reuse the repository's disposable PostgreSQL fixture without editing shared conftest.
from apps.orchestrator.tests.persistence.conftest import (
    migrated_database_url as migrated_database_url,  # noqa: PLC0414
)
from apps.orchestrator.tests.persistence.conftest import (
    session_factory as session_factory,  # noqa: PLC0414
)
from apps.orchestrator.tests.persistence.conftest import (
    test_database_url as test_database_url,  # noqa: PLC0414
)


@pytest.mark.asyncio
async def test_owner_revision_clear_replay_stale_and_history(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    uow = FakeUow(FakeProjects(), FakeTasks(), FakeMutations(), FakeAudit())
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project = await ProjectService(
        lambda: uow, repository_inspector=FakeInspector(path), data_root=tmp_path
    ).register(
        actor=actor,
        idempotency_key="register",
        request=ProjectRegistrationRequest(
            name="forge",
            repository_path=str(path),
            github_repository="owner/repo",
            default_branch="main",
        ),
    )
    service = TaskUsagePolicyService(lambda: uow)
    initial = await service.resolve_for_admission(project.id, frozen_hard_token_cap=120)
    assert initial.policy is not None and initial.policy.revision == 1
    assert initial.hard_token_cap is None
    assert initial.cap_source == "monitoring_revision"
    first_result = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="first",
        request=MonitoringPolicyRevisionRequest(
            expected_revision=1,
            mode="checkpoint",
            minimum_comparables=3,
            history_limit=10,
            window_days=30,
            manual_estimate_tokens=200,
            hard_token_cap=500,
            allow_cross_project_fallback=False,
        ),
    )
    first = first_result.policy
    effective = await service.resolve_for_admission(project.id, frozen_hard_token_cap=120)
    assert effective.policy == first
    assert effective.hard_token_cap == 500
    assert first_result.actor_id == actor.actor_id
    assert first_result.action == "set_policy"
    assert first_result.warnings == ()
    assert len(first_result.affected_snapshot) == 64
    assert first.revision == 2
    assert first.hard_token_cap == 500
    assert first.manual_estimate_tokens == 200
    assert first.window_days == 30
    assert first.allow_cross_project_fallback is False
    cleared = MonitoringPolicyRevisionRequest(
        expected_revision=2,
        hard_token_cap=None,
        manual_estimate_tokens=None,
        window_days=None,
    )
    second_result = await service.revise(
        actor=actor, project_id=project.id, idempotency_key="clear", request=cleared
    )
    second = second_result.policy
    assert second.revision == 3
    assert second.hard_token_cap is None
    assert second.manual_estimate_tokens is None
    assert second.window_days is None
    assert second.mode.value == "checkpoint"
    assert (await service.get(project.id, 2)) == first
    assert (await service.get(project.id)) == second
    effective_after_clear = await service.resolve_for_admission(
        project.id, frozen_hard_token_cap=120
    )
    assert effective_after_clear.policy == second
    assert effective_after_clear.hard_token_cap is None
    replay = await service.revise(
        actor=actor, project_id=project.id, idempotency_key="clear", request=cleared
    )
    assert replay.policy == second
    assert replay.replayed is True
    assert replay.affected_snapshot == second_result.affected_snapshot
    assert len(uow.audit.records) == 3  # registration plus two distinct revisions
    with pytest.raises(MutationConflict):
        await service.revise(
            actor=actor,
            project_id=project.id,
            idempotency_key="clear",
            request=MonitoringPolicyRevisionRequest(expected_revision=3, hard_token_cap=900),
        )
    with pytest.raises(MonitoringPolicyConflict):
        await service.revise(
            actor=actor,
            project_id=project.id,
            idempotency_key="stale",
            request=MonitoringPolicyRevisionRequest(expected_revision=1, hard_token_cap=900),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ("hard_token_cap", "hard_context_cap", "manual_estimate_tokens", "window_days")
)
async def test_idempotency_distinguishes_omitted_preserve_from_explicit_clear(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    uow = FakeUow(FakeProjects(), FakeTasks(), FakeMutations(), FakeAudit())
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project = await ProjectService(
        lambda: uow, repository_inspector=FakeInspector(path), data_root=tmp_path
    ).register(
        actor=actor,
        idempotency_key="register",
        request=ProjectRegistrationRequest(
            name="forge",
            repository_path=str(path),
            github_repository="owner/repo",
            default_branch="main",
        ),
    )
    service = TaskUsagePolicyService(lambda: uow)
    assigned = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="assign",
        request=MonitoringPolicyRevisionRequest.model_validate(
            {"expected_revision": 1, field: 500}
        ),
    )
    assert getattr(assigned.policy, field) == 500

    preserve_request = MonitoringPolicyRevisionRequest(expected_revision=2)
    preserved = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="preserve",
        request=preserve_request,
    )
    assert getattr(preserved.policy, field) == 500
    preserved_replay = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="preserve",
        request=preserve_request,
    )
    assert preserved_replay.replayed and preserved_replay.policy == preserved.policy
    clear_request = MonitoringPolicyRevisionRequest.model_validate(
        {"expected_revision": 2, field: None}
    )
    with pytest.raises(MutationConflict):
        await service.revise(
            actor=actor,
            project_id=project.id,
            idempotency_key="preserve",
            request=clear_request,
        )

    cleared_request = MonitoringPolicyRevisionRequest.model_validate(
        {"expected_revision": 3, field: None}
    )
    cleared = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="clear",
        request=cleared_request,
    )
    assert getattr(cleared.policy, field) is None
    cleared_replay = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="clear",
        request=cleared_request,
    )
    assert cleared_replay.replayed and cleared_replay.policy == cleared.policy
    with pytest.raises(MutationConflict):
        await service.revise(
            actor=actor,
            project_id=project.id,
            idempotency_key="clear",
            request=MonitoringPolicyRevisionRequest(expected_revision=3),
        )

    later_preserve = await service.revise(
        actor=actor,
        project_id=project.id,
        idempotency_key="later-preserve",
        request=MonitoringPolicyRevisionRequest(expected_revision=4),
    )
    assert getattr(later_preserve.policy, field) is None
    assert (await service.get(project.id, 2)) == assigned.policy
    assert (await service.get(project.id, 3)) == preserved.policy


@pytest.mark.parametrize(
    "field", ["hard_token_cap", "hard_context_cap", "manual_estimate_tokens", "window_days"]
)
@pytest.mark.parametrize("value", [0, -1, 2**63, True])
def test_monitoring_integers_reject_invalid_storage_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        MonitoringPolicyRevisionRequest.model_validate({"expected_revision": 0, field: value})


def test_unconfigured_legacy_project_uses_only_its_frozen_aggregate_cap() -> None:
    resolved = resolve_effective_monitoring_policy(None, frozen_hard_token_cap=120)
    assert resolved.policy is None
    assert resolved.hard_token_cap == 120
    assert resolved.cap_source == "frozen_legacy"
    assert resolve_effective_monitoring_policy(None).hard_token_cap is None
    with pytest.raises(ValueError, match="frozen hard token cap"):
        resolve_effective_monitoring_policy(None, frozen_hard_token_cap=0)


@pytest.mark.asyncio
async def test_postgres_policy_revision_cas_history_and_signed64(session_factory) -> None:
    project_id, actor_id = uuid4(), uuid4()
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="monitoring",
                canonical_path=f"C:/monitoring/{project_id}",
                canonical_path_key=f"c:/monitoring/{project_id}",
                github_repository=f"owner/{project_id}",
                default_branch="main",
            )
        )
        await session.commit()

    first = MonitoringPolicy(project_id=project_id)
    async with session_factory() as session:
        repo = PostgresTaskUsagePolicyRepository(session)
        await repo.append_policy_revision(first, expected_revision=0, actor_id=actor_id)
        await session.commit()
    second = MonitoringPolicy(
        project_id=project_id,
        revision=2,
        mode=MonitoringMode.CHECKPOINT,
        hard_token_cap=2**63 - 1,
        manual_estimate_tokens=2**63 - 1,
        window_days=30,
        allow_cross_project_fallback=False,
    )
    async with session_factory() as session:
        repo = PostgresTaskUsagePolicyRepository(session)
        await repo.append_policy_revision(second, expected_revision=1, actor_id=actor_id)
        await session.commit()
        assert await repo.get(project_id, 1) == first
        assert await repo.get(project_id) == second

    async def competing(cap: int) -> bool:
        async with session_factory() as session:
            repo = PostgresTaskUsagePolicyRepository(session)
            try:
                await repo.append_policy_revision(
                    MonitoringPolicy(project_id=project_id, revision=3, hard_token_cap=cap),
                    expected_revision=2,
                    actor_id=actor_id,
                )
                await session.commit()
                return True
            except MonitoringPolicyConflict:
                await session.rollback()
                return False

    results = await asyncio.gather(competing(100), competing(200))
    assert sorted(results) == [False, True]
    async with session_factory() as session:
        repo = PostgresTaskUsagePolicyRepository(session)
        latest = await repo.get(project_id)
        assert latest is not None and latest.revision == 3
        assert latest.hard_token_cap in {100, 200}
        assert await repo.get(project_id, 2) == second
