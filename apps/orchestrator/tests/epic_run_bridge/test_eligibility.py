"""PostgreSQL and real Git eligibility checks without remote providers."""

import asyncio
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.services.epic_eligibility import EpicEligibilityService
from forge.application.services.epic_lifecycle import EpicLifecycleService
from forge.application.services.epic_run_bridge import EpicRunBridgeService
from forge.application.services.runs import RunCommandService
from forge.domain.epic_run_bridge import EpicLaunchConflict
from forge.domain.operation import canonical_digest
from forge.persistence.models import (
    OperationIntent,
    Project,
    PullRequest,
    Run,
    RunCommand,
    RunEvent,
)
from forge.persistence.models.epic_eligibility import EpicCompletionHandoff
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, null, select, text
from sqlalchemy.exc import DBAPIError

from apps.orchestrator.tests.epic_run_bridge.test_launch import BridgeWork, setup


@pytest.fixture
def bridge_factory(session_factory):
    return lambda: BridgeWork(session_factory)


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


async def set_origin(session_factory, epic_id, path):
    async with session_factory() as session:
        from forge.persistence.models.epic_brief import Epic
        epic = await session.get(Epic, epic_id)
        project = await session.get(Project, epic.project_id)
    git(path, "remote", "add", "origin", f"https://github.com/{project.github_repository}.git")


@pytest.mark.asyncio
async def test_unknown_predecessor_remains_blocked_after_restart(
    session_factory, bridge_factory, tmp_path
):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file").write_text("base")
    git(tmp_path, "add", "file")
    git(tmp_path, "commit", "-m", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD")
    service, inspector, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True, repository_path=tmp_path
    )
    await set_origin(session_factory, epic_id, tmp_path)
    first = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first", request=request(first_id)
    )
    producer = EpicEligibilityService(lambda: PostgresUnitOfWork(session_factory))
    for _ in range(2):
        evidence = await producer.evidence(
            epic_id=epic_id,
            execution_id=first.execution_id,
            item_ids=[first_id],
            base_ref="refs/heads/main",
            base_sha=base_sha,
        )
        assert evidence[0].status == "unverified"
        assert evidence[0].blocker_code == "predecessor_integration_unverified"
    async with session_factory() as session, session.begin():
        run = await session.get(Run, first.run_id)
        run.state = "CANCELLED"
    cancelled = await producer.evidence(
        epic_id=epic_id,
        execution_id=first.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=base_sha,
    )
    assert cancelled[0].blocker_code == "predecessor_failed"
    inspector.sha = base_sha
    bridge = EpicRunBridgeService(bridge_factory, run_service=service._runs, eligibility=producer)
    acknowledged = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="acknowledge-cancelled",
        request=request(second_id, execution_id=first.execution_id, owner_override=True),
    )
    assert acknowledged.dependency_evidence[0].blocker_code == "predecessor_failed"
    assert "predecessor_unverified" in acknowledged.blocker_codes
    assert acknowledged.dependency_evidence[0].handoff_id is None


@pytest.mark.asyncio
async def test_settled_merge_requires_integration_and_is_immutable(
    session_factory, bridge_factory, tmp_path
):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file").write_text("base")
    git(tmp_path, "add", "file")
    git(tmp_path, "commit", "-m", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD")
    service, inspector, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True, repository_path=tmp_path
    )
    await set_origin(session_factory, epic_id, tmp_path)
    first = await service.launch(
        actor=actor, epic_id=epic_id, idempotency_key="first", request=request(first_id)
    )
    git(tmp_path, "checkout", "-b", "merged")
    (tmp_path / "file").write_text("integrated")
    git(tmp_path, "commit", "-am", "integrated")
    merged_sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "main")
    producer = EpicEligibilityService(lambda: PostgresUnitOfWork(session_factory))
    bridge = EpicRunBridgeService(bridge_factory, run_service=service._runs, eligibility=producer)
    async with session_factory() as session, session.begin():
        run = await session.get(Run, first.run_id)
        run.state = "COMPLETED"
        run.version += 1
        command = (
            await session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))
        ).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        project = await session.get(Project, run.project_id)
        payload = {"approval_id": str(uuid4())}
        digest = canonical_digest(payload)
        intent = OperationIntent(
            id=uuid4(),
            run_id=run.id,
            operation_kind="merge_pr",
            idempotency_key=f"{run.id}:merge_pr:{digest}",
            request_digest=digest,
            request_payload=payload,
            status="SUCCEEDED",
            outcome_schema_version=1,
            outcome_payload={"merge_sha": merged_sha},
            completed_at=datetime.now(UTC),
        )
        session.add(intent)
        session.add(
            PullRequest(
                run_id=run.id,
                repository=project.github_repository,
                branch="merged",
                base_ref="main",
                pull_request_number=1,
                head_sha=merged_sha,
                base_sha=base_sha,
                checks={},
                review_state={},
                state="MERGED",
                merge_sha=merged_sha,
                merge_intent_id=intent.id,
            )
        )
        next_sequence = (
            await session.scalar(
                select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id)
            )
        ) + 1
        session.add(
            RunEvent(
                run_id=run.id,
                sequence=next_sequence,
                run_version=run.version,
                event_type="run.merge_completed",
                actor_class="worker",
                payload={"merge_intent_id": str(intent.id), "merge_sha": merged_sha},
                occurred_at=datetime.now(UTC),
            )
        )
    before = await producer.evidence(
        epic_id=epic_id,
        execution_id=first.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=base_sha,
    )
    assert before[0].status == "unverified"
    inspector.sha = base_sha
    with pytest.raises(EpicLaunchConflict, match="predecessor_unverified"):
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="before-integration",
            request=request(second_id, execution_id=first.execution_id),
        )
    fake_base = git(tmp_path, "commit-tree", f"{base_sha}^{{tree}}", "-p", merged_sha, "-m", "forged ancestry")
    git(tmp_path, "replace", base_sha, fake_base)
    assert subprocess.run(  # noqa: ASYNC221 - fixture creates a tiny local Git history
        ["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", merged_sha, base_sha],
        check=False,
    ).returncode == 0
    replaced = await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=base_sha,
    )
    assert replaced[0].status == "unverified"
    assert replaced[0].handoff_id is None
    git(tmp_path, "replace", "-d", base_sha)
    grafts = tmp_path / ".git" / "info" / "grafts"
    grafts.write_text(f"{base_sha} {merged_sha}\n")
    grafted = await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=base_sha,
    )
    assert grafted[0].status == "unverified"
    grafts.unlink()
    git(tmp_path, "merge", "--ff-only", "merged")
    integrated_sha = git(tmp_path, "rev-parse", "main")
    async with session_factory() as session, session.begin():
        pull = (await session.scalars(select(PullRequest).where(PullRequest.run_id == first.run_id))).one()
        pull.base_ref = "other"
    wrong_branch = await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=integrated_sha,
    )
    assert wrong_branch[0].status == "unverified"
    async with session_factory() as session, session.begin():
        pull = (await session.scalars(select(PullRequest).where(PullRequest.run_id == first.run_id))).one()
        pull.base_ref = "main"
    concurrent = await asyncio.gather(
        *(
            producer.evidence(
                epic_id=epic_id,
                execution_id=first.execution_id,
                item_ids=[first_id],
                base_ref="refs/heads/main",
                base_sha=integrated_sha,
            )
            for _ in range(6)
        )
    )
    proof = concurrent[0]
    assert proof[0].status == "verified"
    assert all(value[0].handoff_id == proof[0].handoff_id for value in concurrent)
    assert proof[0].integrated_sha == merged_sha
    async with session_factory() as session:
        original = (await session.scalars(select(EpicCompletionHandoff))).one()
        original_base, original_digest = original.verified_base_sha, original.evidence_digest
        assert original_base == integrated_sha
    shallow = tmp_path / ".git" / "shallow"
    shallow.write_text(f"{base_sha}\n")
    assert (await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=integrated_sha,
    ))[0].status == "unverified"
    shallow.unlink()
    again = await EpicEligibilityService(lambda: PostgresUnitOfWork(session_factory)).evidence(
        epic_id=epic_id,
        execution_id=first.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=integrated_sha,
    )
    assert again[0].handoff_id == proof[0].handoff_id
    inspector.sha = integrated_sha
    with pytest.raises(EpicLaunchConflict) as budget_conflict:
        await bridge.launch(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="budget-default",
            request=request(second_id, execution_id=first.execution_id),
        )
    assert "predecessor_unverified" not in budget_conflict.value.blocker_codes
    dependent = await bridge.launch(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="after-integration",
        request=request(second_id, execution_id=first.execution_id, owner_override=True),
    )
    assert dependent.dependency_evidence[0].handoff_id == proof[0].handoff_id
    assert dependent.dependency_evidence[0].status == "verified"
    assert "epic_usage_unknown" in dependent.blocker_codes
    items = await producer.readiness(epic_id=epic_id, execution_id=first.execution_id)
    assert [(item.item_id, item.status) for item in items] == [
        (first_id, "verified"),
        (second_id, "active"),
    ]
    execution_view = await EpicLifecycleService(
        lambda: PostgresUnitOfWork(session_factory),
        commands=RunCommandService(lambda: PostgresUnitOfWork(session_factory)),
        eligibility=producer,
    ).get(epic_id, first.execution_id)
    assert execution_view.items == items
    (tmp_path / "later").write_text("later integration")
    git(tmp_path, "add", "later")
    git(tmp_path, "commit", "-m", "advance integration")
    advanced_sha = git(tmp_path, "rev-parse", "main")
    advanced = await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=advanced_sha,
    )
    assert advanced[0].status == "verified" and advanced[0].handoff_id == proof[0].handoff_id
    async with session_factory() as session:
        retained = (await session.scalars(select(EpicCompletionHandoff))).one()
        assert (retained.verified_base_sha, retained.evidence_digest) == (
            original_base, original_digest
        )
    later_epoch = await service.start(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="later-epoch",
        expected_epic_version=5,
        owner_override=True,
    )
    later = await producer.evidence(
        epic_id=epic_id,
        execution_id=later_epoch.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=integrated_sha,
    )
    assert later[0].status == "unknown"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EpicCompletionHandoff)) == 1
        with pytest.raises(DBAPIError, match="epic completion handoffs are immutable"):
            await session.execute(
                text("UPDATE epic_completion_handoffs SET merge_sha = :sha"),
                {"sha": base_sha},
            )
        await session.rollback()
    async with session_factory() as session, session.begin():
        pull = (await session.scalars(select(PullRequest).where(PullRequest.run_id == first.run_id))).one()
        pull.merge_sha = advanced_sha
        recorded_intent = await session.get(OperationIntent, intent.id)
        recorded_intent.outcome_payload = {"merge_sha": advanced_sha}
        event = (await session.scalars(select(RunEvent).where(
            RunEvent.run_id == first.run_id,
            RunEvent.event_type == "run.merge_completed",
        ))).one()
        event.payload = {**event.payload, "merge_sha": advanced_sha}
    conflicting = await producer.evidence(
        epic_id=epic_id, execution_id=first.execution_id,
        item_ids=[first_id], base_ref="refs/heads/main", base_sha=advanced_sha,
    )
    assert conflicting[0].status == "unverified" and conflicting[0].handoff_id is None
    async with session_factory() as session:
        retained = (await session.scalars(select(EpicCompletionHandoff))).one()
        assert (retained.merge_sha, retained.verified_base_sha, retained.evidence_digest) == (
            merged_sha, original_base, original_digest
        )
    async with session_factory() as session, session.begin():
        recorded_intent = await session.get(OperationIntent, intent.id)
        recorded_intent.status = "FAILED"
        recorded_intent.outcome_payload = null()
        recorded_intent.outcome_schema_version = None
        recorded_intent.last_error = "merge observation rejected"
    rejected = await producer.evidence(
        epic_id=epic_id,
        execution_id=first.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=integrated_sha,
    )
    assert rejected[0].status == "unverified"
    git(tmp_path, "reset", "--hard", base_sha)
    stale = await producer.evidence(
        epic_id=epic_id,
        execution_id=first.execution_id,
        item_ids=[first_id],
        base_ref="refs/heads/main",
        base_sha=base_sha,
    )
    assert stale[0].status == "unverified"
    rolled_back = await producer.readiness(epic_id=epic_id, execution_id=first.execution_id)
    assert rolled_back[0].status == "blocked"
    assert rolled_back[1].status == "active"
