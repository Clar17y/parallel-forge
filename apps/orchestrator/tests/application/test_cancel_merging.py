from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import CancelRunHandler, ControlCommandRejected
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import (
    RunCommandRequest,
    RunCommandService,
    RunCommandValidationError,
)
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunSnapshot, RunState


def _run(*, state: RunState, suspended_state: RunState | None = None) -> RunSnapshot:
    return RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=state,
        suspended_state=suspended_state,
    )


@pytest.mark.parametrize(
    "run",
    [
        _run(state=RunState.MERGING),
        _run(state=RunState.PAUSED, suspended_state=RunState.MERGING),
        _run(
            state=RunState.AWAITING_HUMAN_INTERVENTION,
            suspended_state=RunState.MERGING,
        ),
    ],
)
def test_merge_settlement_states_are_recognized_for_cancellation(run: RunSnapshot) -> None:
    from forge.application.services.runs import cancellation_rejection_reason

    assert cancellation_rejection_reason(run) == (
        "cancellation is unavailable while merge settlement is in progress"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state, suspended_state",
    [
        (RunState.MERGING, None),
        (RunState.PAUSED, RunState.MERGING),
        (RunState.AWAITING_HUMAN_INTERVENTION, RunState.MERGING),
    ],
)
async def test_command_admission_refuses_merge_settlement_without_enqueue(
    state: RunState, suspended_state: RunState | None
) -> None:
    run = _run(state=state, suspended_state=suspended_state)

    class Commands:
        enqueues = 0

        async def get_by_idempotency_key(self, key: str) -> None:
            del key

        async def enqueue(self, **kwargs: object) -> None:
            del kwargs
            self.enqueues += 1
            raise AssertionError("merge cancellation must be refused before enqueue")

    commands = Commands()

    class Runs:
        async def get_for_update(self, run_id: object) -> RunSnapshot:
            assert run_id == run.id
            return run

    class Work:
        def __init__(self) -> None:
            self.commands = commands
            self.runs = Runs()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def commit(self) -> None:
            raise AssertionError("refused cancellation must not commit")

    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    request = RunCommandRequest(command_type="cancel", expected_run_version=run.version)

    with pytest.raises(RunCommandValidationError, match="merge settlement"):
        await RunCommandService(lambda: Work()).enqueue(
            actor=actor,
            run_id=run.id,
            idempotency_key=f"cancel-{state.value}",
            request=request,
        )
    assert commands.enqueues == 0


@pytest.mark.asyncio
async def test_durable_cancel_handler_refuses_merge_settlement() -> None:
    run = _run(state=RunState.PAUSED, suspended_state=RunState.MERGING)
    now = datetime.now(UTC)
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run.id,
        command_type="cancel",
        idempotency_key="cancel-merge",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=run.version,
        actor_id=uuid4(),
        payload_schema_version=1,
        attempt=1,
        available_at=now,
        lease_owner="worker",
        lease_expires_at=now,
        created_at=now,
    )

    class Commands:
        async def assert_current_lease(self, value: CommandEnvelope) -> CommandEnvelope:
            return value

    class Runs:
        async def get_for_update(self, run_id: object) -> RunSnapshot:
            assert run_id == run.id
            return run

    with pytest.raises(ControlCommandRejected, match="merge settlement"):
        await CancelRunHandler()(command, SimpleNamespace(commands=Commands(), runs=Runs()))
