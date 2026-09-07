"""Integration tests for deterministic controller-step persistence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.application.ports.controller_steps import (
    ControllerStepRecord,
    ControllerStepRepository,
    ControllerStepUnsettledError,
)
from forge.application.ports.executions import ExecutionStatus
from forge.domain.run import RunSnapshot
from forge.observability.redaction import Redactor
from forge.persistence.models import (
    AgentExecution,
    Artifact,
    ArtifactLineage,
    RunEvent,
    Step,
)
from forge.persistence.repositories.controller_steps import (
    ControllerStepConflict,
    ControllerStepDataError,
    ControllerStepNotFound,
    ControllerStepRepositoryError,
    PostgresControllerStepRepository,
)
from forge.persistence.repositories.runs import PersistenceError
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select


async def _create_test_artifact(session_factory, run_id: UUID, digest_char: str = "1") -> UUID:
    artifact_id = uuid4()
    digest = digest_char * 64
    async with session_factory() as session, session.begin():
        artifact = Artifact(
            id=artifact_id,
            digest=digest,
            media_type="text/plain",
            storage_pointer=f"sha256/{digest[:2]}/{digest[2:]}.blob",
            size_bytes=16,
            artifact_metadata={},
        )
        lineage = ArtifactLineage(
            id=uuid4(),
            artifact_id=artifact_id,
            run_id=run_id,
            producer_kind="controller_validation",
        )
        session.add_all([artifact, lineage])
    return artifact_id


@pytest.mark.integration
async def test_happy_admission_and_finalization(session_factory, persisted_run) -> None:
    step_id = uuid4()
    started = datetime.now(UTC) - timedelta(seconds=10)

    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        assert isinstance(repo, ControllerStepRepository)
        admitted = await repo.admit(
            persisted_run.id,
            step_id,
            "validate",
            1,
            started_at=started,
        )
        assert isinstance(admitted, ControllerStepRecord)
        assert admitted.run_id == persisted_run.id
        assert admitted.step_id == step_id
        assert admitted.id == step_id
        assert admitted.kind == "validate"
        assert admitted.attempt == 1
        assert admitted.status == ExecutionStatus.RUNNING
        assert admitted.started_at == started
        assert admitted.completed_at is None
        assert admitted.outcome is None
        assert admitted.output_artifact_id is None
        assert admitted.is_new is True
        assert admitted.is_terminal is False

    # Check Step row and event in DB
    async with session_factory() as session:
        step_row = await session.get(Step, step_id)
        assert step_row is not None
        assert step_row.status == "RUNNING"
        assert step_row.attempt == 1

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == persisted_run.id,
                        RunEvent.event_type == "controller_step.admitted",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["step_id"] == str(step_id)
        assert events[0].payload["status"] == "RUNNING"

    # Create output artifact
    artifact_id = await _create_test_artifact(session_factory, persisted_run.id, "a")

    # Finalize
    completed = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        finalized = await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            output_artifact_id=artifact_id,
            outcome="validation successful",
            completed_at=completed,
        )
        assert isinstance(finalized, ControllerStepRecord)
        assert finalized.run_id == persisted_run.id
        assert finalized.step_id == step_id
        assert finalized.status == ExecutionStatus.SUCCEEDED
        assert finalized.output_artifact_id == artifact_id
        assert finalized.outcome == "validation successful"
        assert finalized.completed_at == completed
        assert finalized.is_new is False
        assert finalized.is_terminal is True

    # Verify get returns detached record
    async with session_factory() as session:
        repo = PostgresControllerStepRepository(session)
        got = await repo.get(persisted_run.id, step_id)
        assert got == finalized

        step_row = await session.get(Step, step_id)
        assert step_row is not None
        assert step_row.status == "SUCCEEDED"
        assert step_row.outcome == "validation successful"
        assert step_row.output_artifact_id == artifact_id

        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == persisted_run.id,
                        RunEvent.event_type == "controller_step.finalized",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["step_id"] == str(step_id)
        assert events[0].payload["status"] == "SUCCEEDED"
        assert events[0].payload["output_artifact_id"] == str(artifact_id)


@pytest.mark.integration
async def test_exact_replay(session_factory, persisted_run) -> None:
    step_id = uuid4()
    started = datetime.now(UTC) - timedelta(seconds=20)

    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        first = await repo.admit(
            persisted_run.id,
            step_id,
            "validate",
            1,
            started_at=started,
        )
        assert first.is_new is True

    # Exact replay of admission
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        replay = await repo.admit(
            persisted_run.id,
            step_id,
            "validate",
            1,
            started_at=started,
        )
        assert replay.is_new is False
        assert replay.step_id == first.step_id
        assert replay.started_at == first.started_at
        assert replay.status == ExecutionStatus.RUNNING

    # Verify no duplicate events or steps
    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == persisted_run.id,
                        RunEvent.event_type == "controller_step.admitted",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    # Finalize
    completed = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        finalized = await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.FAILED,
            outcome="validation failed",
            completed_at=completed,
        )
        assert finalized.is_new is False
        assert finalized.status == ExecutionStatus.FAILED

    # Exact replay of finalization
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        re_finalized = await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.FAILED,
            outcome="validation failed",
            completed_at=completed,
        )
        assert re_finalized == finalized

    # Verify no duplicate finalization events
    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == persisted_run.id,
                        RunEvent.event_type == "controller_step.finalized",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


@pytest.mark.integration
async def test_no_agent_execution_row(session_factory, persisted_run) -> None:
    step_id = uuid4()
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.admit(persisted_run.id, step_id, "validate", 1)
        await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            outcome="passed",
        )

    async with session_factory() as session:
        agent_rows = (
            (
                await session.execute(
                    select(AgentExecution).where(AgentExecution.run_id == persisted_run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(agent_rows) == 0


@pytest.mark.integration
async def test_concurrent_same_key_admission_one_winner(session_factory, persisted_run) -> None:
    step_id_1 = uuid4()
    step_id_2 = uuid4()

    async def _try_admit(step_id: UUID) -> tuple[bool, str | None]:
        try:
            async with session_factory() as session, session.begin():
                repo = PostgresControllerStepRepository(session)
                await repo.admit(persisted_run.id, step_id, "validate", 1)
            return True, None
        except ControllerStepConflict as exc:
            return False, str(exc)

    results = await asyncio.gather(_try_admit(step_id_1), _try_admit(step_id_2))
    successes = [r for r in results if r[0]]
    failures = [r for r in results if not r[0]]

    assert len(successes) == 1
    assert len(failures) == 1

    # Exactly one Step row exists
    async with session_factory() as session:
        steps = (
            (
                await session.execute(
                    select(Step).where(
                        Step.run_id == persisted_run.id,
                        Step.kind == "validate",
                        Step.attempt == 1,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(steps) == 1


@pytest.mark.integration
async def test_caller_rollback_including_event_failure(session_factory, persisted_run) -> None:
    step_id = uuid4()

    # Caller explicitly rolls back
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.admit(persisted_run.id, step_id, "validate", 1)
        await session.rollback()

    async with session_factory() as session:
        step_row = await session.get(Step, step_id)
        assert step_row is None
        events = (
            (await session.execute(select(RunEvent).where(RunEvent.run_id == persisted_run.id)))
            .scalars()
            .all()
        )
        assert len(events) == 0

    # Event append failure rolls back step
    class FailingEventRepo:
        async def append(self, event):
            raise PersistenceError("simulated event failure")

    step_id_2 = uuid4()
    with pytest.raises(ControllerStepRepositoryError):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session, events=FailingEventRepo())
            await repo.admit(persisted_run.id, step_id_2, "validate", 1)

    async with session_factory() as session:
        step_row_2 = await session.get(Step, step_id_2)
        assert step_row_2 is None


@pytest.mark.integration
async def test_conflicting_identities_and_cross_run_artifacts(
    session_factory, persisted_run
) -> None:
    step_id = uuid4()
    started = datetime.now(UTC) - timedelta(seconds=30)

    # Seed a second run
    other_run = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=1,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other_run)
        await work.commit()
    other_run_id = other_run.id

    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.admit(persisted_run.id, step_id, "validate", 1, started_at=started)

    # 1. Re-admit same key (run_id, kind, attempt) with different step_id -> Conflict
    other_step_id = uuid4()
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(persisted_run.id, other_step_id, "validate", 1)

    # 2. Re-admit same step_id for different run -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(other_run_id, step_id, "validate", 1)

    # 3. Re-admit with mismatched started_at -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(
                persisted_run.id,
                step_id,
                "validate",
                1,
                started_at=started + timedelta(seconds=5),
            )

    # 4. Get with step_id belonging to different run -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session:
            repo = PostgresControllerStepRepository(session)
            await repo.get(other_run_id, step_id)

    # 5. Finalize with step_id belonging to different run -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(other_run_id, step_id, ExecutionStatus.SUCCEEDED)

    # 6. Step linked to an AgentExecution -> Conflict on admit, get, and finalize
    agent_step_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Step(
                id=agent_step_id,
                run_id=persisted_run.id,
                kind="validate",
                attempt=2,
                status="RUNNING",
                started_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            AgentExecution(
                id=uuid4(),
                run_id=persisted_run.id,
                step_id=agent_step_id,
                role="planner",
                instruction_version="v1",
                provider="test",
                model="test",
                status="RUNNING",
                started_at=datetime.now(UTC),
            )
        )

    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(persisted_run.id, agent_step_id, "validate", 2)

    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session:
            repo = PostgresControllerStepRepository(session)
            await repo.get(persisted_run.id, agent_step_id)

    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(persisted_run.id, agent_step_id, ExecutionStatus.SUCCEEDED)

    # 7. Cross-run output artifact -> Conflict
    other_run_artifact_id = await _create_test_artifact(session_factory, other_run_id, "b")
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.SUCCEEDED,
                output_artifact_id=other_run_artifact_id,
            )

    # 8. Nonexistent artifact -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.SUCCEEDED,
                output_artifact_id=uuid4(),
            )


@pytest.mark.integration
async def test_terminal_replay_conflict(session_factory, persisted_run) -> None:
    step_id = uuid4()
    started = datetime.now(UTC) - timedelta(seconds=10)
    completed = datetime.now(UTC)

    artifact_id = await _create_test_artifact(session_factory, persisted_run.id, "c")

    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.admit(persisted_run.id, step_id, "validate", 1, started_at=started)
        await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            output_artifact_id=artifact_id,
            outcome="passed",
            completed_at=completed,
        )

    # Finalize with different status -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.FAILED,
                output_artifact_id=artifact_id,
                outcome="passed",
                completed_at=completed,
            )

    # Finalize with different output_artifact_id -> Conflict
    other_artifact_id = await _create_test_artifact(session_factory, persisted_run.id, "d")
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.SUCCEEDED,
                output_artifact_id=other_artifact_id,
                outcome="passed",
                completed_at=completed,
            )

    # Finalize with different outcome -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.SUCCEEDED,
                output_artifact_id=artifact_id,
                outcome="different outcome",
                completed_at=completed,
            )

    # Finalize with different completed_at -> Conflict
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.SUCCEEDED,
                output_artifact_id=artifact_id,
                outcome="passed",
                completed_at=completed + timedelta(seconds=1),
            )

    # Finalize with non-terminal status RUNNING -> ValueError
    with pytest.raises(ValueError):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.finalize(
                persisted_run.id,
                step_id,
                ExecutionStatus.RUNNING,
            )

    # Finalize with completed_at < started_at -> Conflict
    step_id_2 = uuid4()
    with pytest.raises(ControllerStepConflict):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(persisted_run.id, step_id_2, "validate", 2, started_at=started)
            await repo.finalize(
                persisted_run.id,
                step_id_2,
                ExecutionStatus.SUCCEEDED,
                completed_at=started - timedelta(seconds=1),
            )


@pytest.mark.integration
async def test_unresolved_next_attempt(session_factory, persisted_run) -> None:
    # 1. Fresh run next_attempt -> 1
    async with session_factory() as session:
        repo = PostgresControllerStepRepository(session)
        next_att = await repo.next_attempt(persisted_run.id, "validate")
        assert next_att == 1

    # 2. Admit attempt 1 (still RUNNING)
    step_id = uuid4()
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.admit(persisted_run.id, step_id, "validate", 1)

    # 3. next_attempt while attempt 1 is RUNNING -> ControllerStepUnsettledError
    with pytest.raises(ControllerStepUnsettledError):
        async with session_factory() as session:
            repo = PostgresControllerStepRepository(session)
            await repo.next_attempt(persisted_run.id, "validate")

    # 4. Finalize attempt 1 -> next_attempt succeeds and returns 2
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        await repo.finalize(persisted_run.id, step_id, ExecutionStatus.SUCCEEDED)

    async with session_factory() as session:
        repo = PostgresControllerStepRepository(session)
        next_att = await repo.next_attempt(persisted_run.id, "validate")
        assert next_att == 2


@pytest.mark.integration
async def test_redaction_and_unsupported_kind(session_factory, persisted_run) -> None:
    # Outcome redaction
    step_id = uuid4()
    redactor = Redactor(secrets=["topsecret_token_123"])
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session, redactor=redactor)
        await repo.admit(persisted_run.id, step_id, "validate", 1)
        finalized = await repo.finalize(
            persisted_run.id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            outcome="validation secret: topsecret_token_123",
        )
        assert finalized.outcome is not None
        assert "topsecret_token_123" not in finalized.outcome
        assert "[REDACTED]" in finalized.outcome

    # Unsupported kind
    with pytest.raises(ValueError):
        async with session_factory() as session, session.begin():
            repo = PostgresControllerStepRepository(session)
            await repo.admit(persisted_run.id, uuid4(), "plan", 1)

    with pytest.raises(ValueError):
        async with session_factory() as session:
            repo = PostgresControllerStepRepository(session)
            await repo.next_attempt(persisted_run.id, "invalid_kind")


@pytest.mark.integration
async def test_not_found_and_malformed_data_failures(session_factory, persisted_run) -> None:
    # Run not found
    missing_run_id = uuid4()
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        with pytest.raises(ControllerStepNotFound):
            await repo.admit(missing_run_id, uuid4(), "validate", 1)
        with pytest.raises(ControllerStepNotFound):
            await repo.finalize(missing_run_id, uuid4(), ExecutionStatus.SUCCEEDED)

    # Step not found on finalize
    async with session_factory() as session, session.begin():
        repo = PostgresControllerStepRepository(session)
        with pytest.raises(ControllerStepNotFound):
            await repo.finalize(persisted_run.id, uuid4(), ExecutionStatus.SUCCEEDED)

    # Malformed Step in database (started_at missing)
    malformed_step_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Step(
                id=malformed_step_id,
                run_id=persisted_run.id,
                kind="validate",
                attempt=99,
                status="RUNNING",
                started_at=None,
            )
        )

    async with session_factory() as session:
        repo = PostgresControllerStepRepository(session)
        with pytest.raises(ControllerStepDataError):
            await repo.get(persisted_run.id, malformed_step_id)
