from uuid import uuid4

import pytest
from forge.application.services.state_engine import LEGAL, StateEngine
from forge.domain.errors import InvalidTransition
from forge.domain.run import RunSnapshot, RunState, SuspensionKind

EXPECTED_LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.PLANNING, RunState.CANCELLED}),
    RunState.PLANNING: frozenset(
        {
            RunState.AWAITING_PLAN_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_PLAN_APPROVAL: frozenset(
        {RunState.PLANNING, RunState.PREPARING_WORKTREE, RunState.CANCELLED}
    ),
    RunState.PREPARING_WORKTREE: frozenset(
        {
            RunState.IMPLEMENTING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.IMPLEMENTING: frozenset(
        {
            RunState.VALIDATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.VALIDATING: frozenset(
        {
            RunState.REVIEWING,
            RunState.REMEDIATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.REVIEWING: frozenset(
        {
            RunState.REMEDIATING,
            RunState.AWAITING_PR_APPROVAL,
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.REMEDIATING: frozenset(
        {
            RunState.VALIDATING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_PR_APPROVAL: frozenset(
        {RunState.REMEDIATING, RunState.PUBLISHING_PR, RunState.CANCELLED}
    ),
    RunState.PUBLISHING_PR: frozenset(
        {
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.MONITORING_PR: frozenset(
        {
            RunState.REMEDIATING,
            RunState.AWAITING_MERGE_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_HUMAN_INTERVENTION: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.AWAITING_MERGE_APPROVAL: frozenset(
        {
            RunState.MONITORING_PR,
            RunState.MERGING,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.CANCELLED,
        }
    ),
    RunState.MERGING: frozenset(
        {
            RunState.COMPLETED,
            RunState.MONITORING_PR,
            RunState.AWAITING_HUMAN_INTERVENTION,
            RunState.FAILED,
        }
    ),
    RunState.PAUSED: frozenset({RunState.CANCELLED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

PAUSEABLE_STATES = (
    RunState.CREATED,
    RunState.PLANNING,
    RunState.AWAITING_PLAN_APPROVAL,
    RunState.PREPARING_WORKTREE,
    RunState.IMPLEMENTING,
    RunState.VALIDATING,
    RunState.REVIEWING,
    RunState.REMEDIATING,
    RunState.AWAITING_PR_APPROVAL,
    RunState.PUBLISHING_PR,
    RunState.MONITORING_PR,
    RunState.AWAITING_HUMAN_INTERVENTION,
    RunState.AWAITING_MERGE_APPROVAL,
    RunState.MERGING,
)

INTERVENTION_SOURCES = (
    RunState.PLANNING,
    RunState.PREPARING_WORKTREE,
    RunState.IMPLEMENTING,
    RunState.VALIDATING,
    RunState.REVIEWING,
    RunState.REMEDIATING,
    RunState.PUBLISHING_PR,
    RunState.MONITORING_PR,
    RunState.AWAITING_MERGE_APPROVAL,
    RunState.MERGING,
)


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


def test_legal_transition_map_matches_the_independent_contract() -> None:
    assert LEGAL == EXPECTED_LEGAL_TRANSITIONS


@pytest.mark.parametrize(
    ("source", "target"),
    tuple(
        (source, target)
        for source, targets in EXPECTED_LEGAL_TRANSITIONS.items()
        for target in targets
    ),
)
def test_every_declared_legal_transition_is_accepted(source: RunState, target: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)

    transitioned = StateEngine().transition(run, target)

    assert transitioned.state is target
    assert transitioned.version == run.version + 1


@pytest.mark.parametrize(
    ("source", "target"),
    tuple(
        (source, target)
        for source in RunState
        for target in RunState
        if target not in EXPECTED_LEGAL_TRANSITIONS[source]
    ),
)
def test_every_undeclared_transition_is_rejected(source: RunState, target: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)

    with pytest.raises(InvalidTransition):
        StateEngine().transition(run, target)


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


@pytest.mark.parametrize("state", PAUSEABLE_STATES)
def test_pause_succeeds_for_every_allowed_nonterminal_state(state: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=state)

    paused = StateEngine().pause(run)

    assert paused.state is RunState.PAUSED
    assert paused.suspended_state is state
    assert paused.suspension_kind is SuspensionKind.PAUSE
    assert paused.version == run.version + 1


def test_pause_rejects_an_already_paused_run() -> None:
    paused = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.PAUSED,
        suspended_state=RunState.PLANNING,
        suspension_kind=SuspensionKind.PAUSE,
    )

    with pytest.raises(InvalidTransition):
        StateEngine().pause(paused)


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


@pytest.mark.parametrize(
    "state", tuple(state for state in RunState if state is not RunState.PAUSED)
)
def test_resume_rejects_every_non_paused_state(state: RunState) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=state)

    with pytest.raises(InvalidTransition):
        StateEngine().resume(run)


def test_intervene_preserves_the_active_state_for_operator_resolution() -> None:
    engine = StateEngine()
    run = engine.transition(new_run(), RunState.PLANNING)

    intervened = engine.intervene(run)

    assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert intervened.suspended_state is RunState.PLANNING
    assert intervened.suspension_kind is SuspensionKind.INTERVENTION
    assert intervened.version == run.version + 1


@pytest.mark.parametrize("source", INTERVENTION_SOURCES)
def test_intervene_succeeds_for_every_source_with_an_intervention_edge(
    source: RunState,
) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)

    intervened = StateEngine().intervene(run)

    assert intervened.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert intervened.suspended_state is source
    assert intervened.suspension_kind is SuspensionKind.INTERVENTION
    assert intervened.version == run.version + 1


def test_pause_during_intervention_preserves_context_through_resume_and_resolution() -> None:
    engine = StateEngine()
    run = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        local_remediation_count=2,
        remote_remediation_count=1,
    )
    planning = engine.transition(run, RunState.PLANNING)
    intervened = engine.intervene(planning)
    paused = engine.pause(intervened)
    resumed = engine.resume(paused)
    resolved = engine.resolve_intervention(resumed, RunState.AWAITING_PLAN_APPROVAL)

    assert planning.version == 1
    assert intervened.version == 2
    assert paused.version == 3
    assert resumed.version == 4
    assert resolved.version == 5
    assert paused.state is RunState.PAUSED
    assert paused.suspended_state is RunState.AWAITING_HUMAN_INTERVENTION
    assert paused.suspension_kind is SuspensionKind.PAUSE
    assert resumed.state is RunState.AWAITING_HUMAN_INTERVENTION
    assert resumed.suspended_state is RunState.PLANNING
    assert resumed.suspension_kind is SuspensionKind.INTERVENTION
    assert resolved.state is RunState.AWAITING_PLAN_APPROVAL
    assert resolved.suspended_state is None
    assert resolved.suspension_kind is None
    assert resolved.id == run.id
    assert resolved.project_id == run.project_id
    assert resolved.task_id == run.task_id
    assert resolved.local_remediation_count == 2
    assert resolved.remote_remediation_count == 1


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


@pytest.mark.parametrize(
    ("source", "target"),
    tuple(
        (source, target)
        for source in INTERVENTION_SOURCES
        for target in EXPECTED_LEGAL_TRANSITIONS[source]
    ),
)
def test_resolve_intervention_accepts_every_permitted_target(
    source: RunState, target: RunState
) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)
    intervened = StateEngine().intervene(run)

    resolved = StateEngine().resolve_intervention(intervened, target)

    assert resolved.state is target
    assert resolved.version == run.version + 2


@pytest.mark.parametrize(
    ("source", "target"),
    tuple(
        (source, target)
        for source in INTERVENTION_SOURCES
        for target in RunState
        if target not in EXPECTED_LEGAL_TRANSITIONS[source]
    ),
)
def test_resolve_intervention_rejects_every_forbidden_target(
    source: RunState, target: RunState
) -> None:
    run = RunSnapshot(id=uuid4(), project_id=uuid4(), task_id=uuid4(), state=source)
    intervened = StateEngine().intervene(run)

    with pytest.raises(InvalidTransition):
        StateEngine().resolve_intervention(intervened, target)


def test_resolve_intervention_rejects_targets_not_legal_from_suspended_state() -> None:
    engine = StateEngine()
    intervened = engine.intervene(engine.transition(new_run(), RunState.PLANNING))

    with pytest.raises(InvalidTransition):
        engine.resolve_intervention(intervened, RunState.PUBLISHING_PR)


def test_resolve_intervention_rejects_non_intervention_snapshots() -> None:
    with pytest.raises(InvalidTransition):
        StateEngine().resolve_intervention(new_run(), RunState.PLANNING)

    malformed = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.AWAITING_HUMAN_INTERVENTION,
    )
    with pytest.raises(InvalidTransition):
        StateEngine().resolve_intervention(malformed, RunState.FAILED)


def test_resume_rejects_a_paused_snapshot_with_missing_suspension_kind() -> None:
    malformed = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.PAUSED,
        suspended_state=RunState.PLANNING,
        suspension_kind=None,
    )

    with pytest.raises(InvalidTransition):
        StateEngine().resume(malformed)


def test_resolve_rejects_missing_intervention_kind_for_a_fabricated_pr_approval() -> None:
    malformed = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=RunState.AWAITING_HUMAN_INTERVENTION,
        suspended_state=RunState.AWAITING_PR_APPROVAL,
        suspension_kind=None,
    )

    with pytest.raises(InvalidTransition):
        StateEngine().resolve_intervention(malformed, RunState.PUBLISHING_PR)


def test_exported_legal_policy_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        LEGAL[RunState.CREATED] = frozenset()  # type: ignore[index]

    with pytest.raises(AttributeError):
        LEGAL.clear()  # type: ignore[attr-defined]


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
