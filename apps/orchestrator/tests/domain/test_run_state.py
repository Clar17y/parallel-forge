from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest
from forge.domain.ids import ProjectId, RunId, TaskId
from forge.domain.run import RunSnapshot, RunState, SuspensionKind


def new_run() -> RunSnapshot:
    return RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4())


def test_new_snapshot_has_stable_ids_and_created_defaults() -> None:
    run_id = uuid4()
    project_id = uuid4()
    task_id = uuid4()

    run = RunSnapshot(id=run_id, project_id=project_id, task_id=task_id)

    assert run.id == run_id
    assert run.project_id == project_id
    assert run.task_id == task_id
    assert run.state is RunState.CREATED
    assert run.version == 0
    assert run.suspended_state is None
    assert run.suspension_kind is None
    assert run.local_remediation_count == 0
    assert run.remote_remediation_count == 0


def test_snapshot_is_immutable_and_state_changes_return_a_new_snapshot() -> None:
    original = new_run()
    changed = original.with_state(RunState.PLANNING)

    assert changed is not original
    assert original.state is RunState.CREATED
    assert original.version == 0
    assert changed.state is RunState.PLANNING
    assert changed.version == 1
    assert changed.suspended_state is None

    with pytest.raises(FrozenInstanceError):
        original.state = RunState.PLANNING  # type: ignore[misc]


def test_identifiers_are_uuid_new_types() -> None:
    assert len({id(ProjectId), id(RunId), id(TaskId)}) == 3
    assert isinstance(RunId(uuid4()), UUID)


def test_suspension_kind_distinguishes_pause_and_intervention() -> None:
    assert SuspensionKind.PAUSE.value == "PAUSE"
    assert SuspensionKind.INTERVENTION.value == "INTERVENTION"
