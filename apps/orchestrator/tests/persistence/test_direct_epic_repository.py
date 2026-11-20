"""Persistence integration tests for direct-run epic shape and child tasks."""

import asyncio
import uuid

import pytest
from forge.persistence.models import Project
from forge.persistence.repositories.direct import PostgresDirectRepository


async def _seed_project(session_factory) -> uuid.UUID:
    pid = uuid.uuid4()
    async with session_factory() as session:
        project = Project(
            id=pid,
            name="Test Project",
            canonical_path=f"/tmp/proj-{pid}",
            canonical_path_key=f"/tmp/proj-{pid}",
            default_branch="main",
            github_repository="test/repo",
        )
        session.add(project)
        await session.commit()
    return pid


@pytest.mark.integration
async def test_epic_run_lifecycle_and_child_tasks(session_factory):
    pid = await _seed_project(session_factory)
    repo = PostgresDirectRepository(session_factory)

    run = await repo.create(
        project_id=pid,
        task_text="Epic: build authentication and dashboard",
        client="antigravity_cli",
        model="gemini-3.8-flash-medium",
        effort="medium",
        shape="epic",
        review_plan_first=True,
    )
    assert run.shape == "epic"
    assert run.review_plan_first is True
    assert run.state == "QUEUED"

    # Create child tasks
    t1 = await repo.create_task(
        run_id=run.id, ordinal=0, task_text="Child 1: auth", shape="quick"
    )
    t2 = await repo.create_task(
        run_id=run.id, ordinal=1, task_text="Child 2: dashboard", shape="planned"
    )
    tasks = await repo.tasks(run.id)
    assert len(tasks) == 2
    assert tasks[0].id == t1.id
    assert tasks[0].ordinal == 0
    assert tasks[0].task_text == "Child 1: auth"
    assert tasks[1].id == t2.id
    assert tasks[1].ordinal == 1
    assert tasks[1].shape == "planned"

    # Park run in AWAITING_DECOMPOSITION_APPROVAL
    await repo.update_run(run.id, state="AWAITING_DECOMPOSITION_APPROVAL")

    # Owner reorders / edits pending tasks while parked
    updated_tasks = await repo.replace_pending_tasks(
        run.id,
        [
            {"id": t2.id, "task": "Child 2: dashboard (reordered first)", "shape": "planned"},
            {"id": t1.id, "task": "Child 1: auth (now second)", "shape": "quick"},
        ],
        expected_version=(await repo.get(run.id)).control_version,
    )
    assert updated_tasks[0].id == t2.id
    assert updated_tasks[1].id == t1.id
    assert len(updated_tasks) == 2
    assert updated_tasks[0].ordinal == 0
    assert "dashboard" in updated_tasks[0].task_text
    assert updated_tasks[1].ordinal == 1
    assert "auth" in updated_tasks[1].task_text

    # Owner approves decomposition
    approved = await repo.approve_decomposition(run.id, expected_version=(await repo.get(run.id)).control_version)
    assert approved.state == "QUEUED"

    # Replayed approve fails
    with pytest.raises(ValueError, match="not awaiting decomposition approval"):
        await repo.approve_decomposition(run.id)


@pytest.mark.integration
async def test_replace_pending_tasks_guards(session_factory):
    pid = await _seed_project(session_factory)
    repo = PostgresDirectRepository(session_factory)

    run = await repo.create(
        project_id=pid,
        task_text="Epic: feature",
        client="antigravity_cli",
        model="gemini-3.8-flash-medium",
        effort=None,
        shape="epic",
    )
    await repo.create_task(run_id=run.id, ordinal=0, task_text="Task 1", shape="quick")

    # Cannot edit while RUNNING
    await repo.update_run(run.id, state="RUNNING")
    with pytest.raises(ValueError, match="not awaiting decomposition approval"):
        await repo.replace_pending_tasks(run.id, [{"task": "Edited"}])

    # Park in AWAITING_DECOMPOSITION_APPROVAL
    await repo.update_run(run.id, state="AWAITING_DECOMPOSITION_APPROVAL")

    # If a task has started (state != PENDING), cannot replace
    tasks = await repo.tasks(run.id)
    await repo.update_task(tasks[0].id, state="RUNNING")
    with pytest.raises(ValueError, match="already started"):
        await repo.replace_pending_tasks(run.id, [{"task": "Edited"}])


@pytest.mark.integration
async def test_cancel_awaiting_decomposition_approval(session_factory):
    pid = await _seed_project(session_factory)
    repo = PostgresDirectRepository(session_factory)

    run = await repo.create(
        project_id=pid,
        task_text="Epic: to cancel",
        client="antigravity_cli",
        model="gemini-3.8-flash-medium",
        effort=None,
        shape="epic",
    )
    await repo.update_run(run.id, state="AWAITING_DECOMPOSITION_APPROVAL")
    cancelled = await repo.request_cancel(run.id)
    assert cancelled.state == "CANCELLED"
    assert cancelled.cancel_requested is True


@pytest.mark.integration
@pytest.mark.parametrize("race", ["edit_edit", "edit_approve", "cancel_approve", "cancel_edit"])
async def test_owner_operations_serialize_and_reject_stale_snapshots(session_factory, race):
    repo = PostgresDirectRepository(session_factory)
    run = await repo.create(project_id=await _seed_project(session_factory), task_text="epic", client="muse_code", model="m", effort=None, shape="epic")
    task = await repo.create_task(run_id=run.id, ordinal=0, task_text="original")
    parked = await repo.update_run(run.id, state="AWAITING_DECOMPOSITION_APPROVAL")
    actor = uuid.uuid4()

    def edit(text):
        return repo.replace_pending_tasks(run.id, [{"id": task.id, "task": text, "shape": "quick"}], expected_version=parked.control_version, actor_id=actor)

    approve = lambda: repo.approve_decomposition(run.id, expected_version=parked.control_version, actor_id=actor)
    first, second = {
        "edit_edit": (lambda: edit("first"), lambda: edit("second")),
        "edit_approve": (lambda: edit("first"), approve),
        "cancel_approve": (lambda: repo.request_cancel(run.id), approve),
        "cancel_edit": (lambda: repo.request_cancel(run.id), lambda: edit("first")),
    }[race]
    results = await asyncio.gather(first(), second(), return_exceptions=True)
    assert all(not isinstance(r, Exception) or isinstance(r, ValueError) for r in results)
    current = await repo.get(run.id)
    assert (await repo.tasks(run.id))[0].id == task.id
    if race.startswith("cancel"):
        assert current.state == "CANCELLED" and current.cancel_requested
        with pytest.raises(ValueError):
            await repo.approve_decomposition(run.id, expected_version=current.control_version)
    else:
        assert sum(isinstance(r, ValueError) for r in results) == 1
        assert len(current.owner_actions) == 1
        assert current.owner_actions[0]["actor_id"] == str(actor)
        assert current.owner_actions[0]["snapshot_version"] == parked.control_version
        assert current.owner_actions[0]["warnings"] == []
        if current.owner_actions[0]["action"] == "edit_tasks":
            action = current.owner_actions[0]
            assert action["before_tasks"][0]["id"] == str(task.id)
            assert action["after_tasks"][0]["id"] == str(task.id)
            assert action["before_tasks"][0]["task_sha256"] != action["after_tasks"][0]["task_sha256"]


@pytest.mark.integration
async def test_cancel_before_atomic_decomposition_acceptance_creates_no_children(session_factory):
    from forge.domain.direct import DirectDecomposeResult, DirectDecomposeTask

    repo = PostgresDirectRepository(session_factory)
    run = await repo.create(project_id=await _seed_project(session_factory), task_text="epic", client="muse_code", model="m", effort=None, shape="epic")
    await repo.update_run(run.id, state="DECOMPOSING")
    await repo.request_cancel(run.id)
    accepted = await repo.accept_decomposition(run.id, DirectDecomposeResult(status="done", summary="tasks", tasks=(DirectDecomposeTask(task="one", shape="quick"), DirectDecomposeTask(task="two", shape="planned"))))
    assert accepted.state == "CANCELLED"
    assert await repo.tasks(run.id) == ()


@pytest.mark.integration
@pytest.mark.parametrize("state", ["DONE", "FAILED", "CANCELLED", "NEEDS_DECISION"])
async def test_terminal_cancel_is_idempotent_without_new_audit_or_version(session_factory, state):
    repo = PostgresDirectRepository(session_factory)
    run = await repo.create(project_id=await _seed_project(session_factory), task_text="epic", client="muse_code", model="m", effort=None, shape="epic")
    terminal = await repo.update_run(run.id, state=state, cancel_requested=state == "CANCELLED")
    for _ in range(2):
        assert await repo.request_cancel(run.id, actor_id=uuid.uuid4()) == terminal


@pytest.mark.integration
@pytest.mark.parametrize("state", ["RUNNING", "AWAITING_DECOMPOSITION_APPROVAL"])
async def test_accepted_cancel_records_locked_actor_snapshot(session_factory, state):
    repo = PostgresDirectRepository(session_factory)
    run = await repo.create(project_id=await _seed_project(session_factory), task_text="epic", client="muse_code", model="m", effort=None, shape="epic")
    before = await repo.update_run(run.id, state=state)
    actor = uuid.uuid4()
    cancelled = await repo.request_cancel(run.id, actor_id=actor)
    assert cancelled.cancel_requested
    assert cancelled.control_version == before.control_version + 1
    assert cancelled.owner_actions[-1]["actor_id"] == str(actor)
    assert cancelled.owner_actions[-1]["snapshot_version"] == before.control_version
    assert cancelled.owner_actions[-1]["state"] == state
