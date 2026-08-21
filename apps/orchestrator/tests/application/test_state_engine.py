from uuid import uuid4

import pytest
from forge.application.services.state_engine import LEGAL, StateEngine
from forge.domain.errors import InvalidTransition
from forge.domain.run import RunSnapshot, RunState, SuspensionKind


def new_run() -> RunSnapshot:
    return RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4())


def test_happy_path_reaches_pr_approval() -> None:
    engine = StateEngine()
    run = new_run()
    for target in (
        RunState.PLANNING,
        RunState.AWAITING_PLAN_APPROVAL,
        RunState.PREPARING_WORKTREE,
        RunState.IMPLEMENTING,
        RunState.VALIDATING,
        RunState.REVIEWING,
        RunState.AWAITING_PR_APPROVAL,
    ):
        run = engine.transition(run, target)

    assert run.state is RunState.AWAITING_PR_APPROVAL
    assert run.version == 7


@pytest.mark.parametrize("state", tuple(RunState))
def test_legal_transition_map_has_an_entry_for_every_state(state: RunState) -> None:
    assert state in LEGAL


@pytest.mark.parametrize(
    ("source", "target"),
    tuple((source, target) for source, targets in LEGAL.items() for target in targets),
)
def test_every_declared_legal_transition_is_accepted(source: RunState, target: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)

    transitioned = StateEngine().transition(run, target)

    assert transitioned.state is target
    assert transitioned.version == run.version + 1


def test_cannot_publish_without_pr_approval_state() -> None:
    with pytest.raises(InvalidTransition):
        StateEngine().transition(new_run(), RunState.PUBLISHING_PR)


@pytest.mark.parametrize("terminal", (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED))
def test_terminal_run_cannot_advance(terminal: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=terminal)

    with pytest.raises(InvalidTransition):
        StateEngine().transition(run, RunState.PLANNING)


def test_pause_and_resume_restore_the_exact_state() -> None:
    engine = StateEngine()
    run = engine.transition(new_run(), RunState.PLANNING)
    paused = engine.pause(run)
    resumed = engine.resume(paused)

    assert paused.state is RunState.PAUSED
    assert paused.suspended_state is RunState.PLANNING
    assert paused.suspension_kind is SuspensionKind.PAUSE
    assert paused.version == run.version + 1
    assert resumed.state is RunState.PLANNING
    assert resumed.suspended_state is None
    assert resumed.suspension_kind is None
    assert resumed.version == paused.version + 1


@pytest.mark.parametrize("state", (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED))
def test_pause_rejects_terminal_runs(state: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=state)

    with pytest.raises(InvalidTransition):
        StateEngine().pause(run)


def test_resume_requires_a_paused_snapshot_with_a_suspended_state() -> None:
    engine = StateEngine()

    with pytest.raises(InvalidTransition):
        engine.resume(new_run())

    malformed = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.PAUSED,
    )
    with pytest.raises(InvalidTransition):
        engine.resume(malformed)


def test_intervene_preserves_the_active_state_for_operator_resolution() -> None:
    engine = StateEngine()
    run = engine.transition(new_run(), RunState.PLANNING)

    intervened = engine.intervene(run)

    assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert intervened.suspended_state is RunState.PLANNING
    assert intervened.suspension_kind is SuspensionKind.INTERVENTION
    assert intervened.version == run.version + 1


def test_intervene_requires_an_intervention_edge_from_the_active_state() -> None:
    with pytest.raises(InvalidTransition):
        StateEngine().intervene(new_run())


def test_resolve_intervention_uses_the_suspended_state_legal_targets() -> None:
    engine = StateEngine()
    run = engine.transition(new_run(), RunState.PLANNING)
    intervened = engine.intervene(run)

    resolved = engine.resolve_intervention(intervened, RunState.AWAITING_PLAN_APPROVAL)

    assert resolved.state is RunState.AWAITING_PLAN_APPROVAL
    assert resolved.suspended_state is None
    assert resolved.suspension_kind is None
    assert resolved.version == intervened.version + 1


def test_resolve_intervention_rejects_targets_not_legal_from_suspended_state() -> None:
    engine = StateEngine()
    intervened = engine.intervene(engine.transition(new_run(), RunState.PLANNING))

    with pytest.raises(InvalidTransition):
        engine.resolve_intervention(intervened, RunState.PUBLISHING_PR)


def test_intervention_and_pause_do_not_mutate_the_input_snapshot() -> None:
    engine = StateEngine()
    run = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.IMPLEMENTING,
    )

    paused = engine.pause(run)
    intervened = engine.intervene(run)

    assert run.state is RunState.IMPLEMENTING
    assert run.suspended_state is None
    assert paused.state is RunState.PAUSED
    assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
