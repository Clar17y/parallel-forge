"""Integration coverage for atomic run-state transitions."""

import pytest
from forge.domain.errors import InvalidTransition
from forge.domain.run import RunState
from forge.persistence.repositories.runs import ConcurrencyConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork


@pytest.mark.integration
async def test_transition_and_event_commit_together(uow, persisted_run) -> None:
    async with uow:
        await uow.runs.transition(
            run_id=persisted_run.id,
            expected_version=0,
            target=RunState.PLANNING,
            event_type="run.planning_started",
            event_payload={"source": "worker"},
        )
        await uow.commit()

    async with uow:
        loaded = await uow.runs.get(persisted_run.id)
        events = await uow.events.list_after(persisted_run.id, sequence=0)

    assert loaded.state is RunState.PLANNING
    assert loaded.version == 1
    assert [(event.sequence, event.event_type) for event in events] == [(1, "run.planning_started")]
    assert events[0].actor_class == "system"
    assert events[0].payload == {"source": "worker"}


@pytest.mark.integration
async def test_stale_transition_writes_neither_state_nor_event(uow, persisted_run) -> None:
    with pytest.raises(ConcurrencyConflict):
        async with uow:
            await uow.runs.transition(
                run_id=persisted_run.id,
                expected_version=9,
                target=RunState.PLANNING,
                event_type="run.planning_started",
                event_payload={},
            )

    async with uow:
        loaded = await uow.runs.get(persisted_run.id)
        events = await uow.events.list_after(persisted_run.id, sequence=0)

    assert loaded.version == 0
    assert events == []


@pytest.mark.integration
async def test_invalid_transition_writes_neither_state_nor_event(uow, persisted_run) -> None:
    with pytest.raises(InvalidTransition):
        async with uow:
            await uow.runs.transition(
                run_id=persisted_run.id,
                expected_version=0,
                target=RunState.VALIDATING,
                event_type="run.invalid",
                event_payload={},
            )

    async with uow:
        loaded = await uow.runs.get(persisted_run.id)
        events = await uow.events.list_after(persisted_run.id, sequence=0)

    assert loaded == persisted_run
    assert events == []


@pytest.mark.integration
async def test_successful_transition_without_commit_rolls_back(uow, persisted_run) -> None:
    async with uow:
        await uow.runs.transition(
            run_id=persisted_run.id,
            expected_version=0,
            target=RunState.PLANNING,
            event_type="run.planning_started",
            event_payload={},
        )

    async with uow:
        loaded = await uow.runs.get(persisted_run.id)
        events = await uow.events.list_after(persisted_run.id, sequence=0)

    assert loaded == persisted_run
    assert events == []


@pytest.mark.integration
async def test_failure_before_event_insert_rolls_back_state(
    uow, persisted_run, monkeypatch
) -> None:
    async with uow:

        async def fail(_event) -> object:
            raise RuntimeError("injected before event insert")

        monkeypatch.setattr(uow.events, "append", fail)
        with pytest.raises(RuntimeError, match="before event insert"):
            await uow.runs.transition(
                run_id=persisted_run.id,
                expected_version=0,
                target=RunState.PLANNING,
                event_type="run.planning_started",
                event_payload={},
            )

    async with uow:
        assert await uow.runs.get(persisted_run.id) == persisted_run
        assert await uow.events.list_after(persisted_run.id, sequence=0) == []


@pytest.mark.integration
async def test_failure_after_event_insert_rolls_back_state_and_event(
    uow, persisted_run, monkeypatch
) -> None:
    async with uow:
        append = uow.events.append

        async def append_then_fail(event):
            await append(event)
            raise RuntimeError("injected after event insert")

        monkeypatch.setattr(uow.events, "append", append_then_fail)
        with pytest.raises(RuntimeError, match="after event insert"):
            await uow.runs.transition(
                run_id=persisted_run.id,
                expected_version=0,
                target=RunState.PLANNING,
                event_type="run.planning_started",
                event_payload={},
            )

    async with uow:
        assert await uow.runs.get(persisted_run.id) == persisted_run
        assert await uow.events.list_after(persisted_run.id, sequence=0) == []


@pytest.mark.integration
async def test_concurrent_expected_version_allows_one_transition(
    session_factory, persisted_run
) -> None:
    async def attempt():
        async with PostgresUnitOfWork(session_factory) as work:
            try:
                changed = await work.runs.transition(
                    run_id=persisted_run.id,
                    expected_version=0,
                    target=RunState.PLANNING,
                    event_type="run.planning_started",
                    event_payload={},
                )
                await work.commit()
                return changed, None
            except ConcurrencyConflict as error:
                return None, error

    first, second = await __import__("asyncio").gather(attempt(), attempt())
    results = (first, second)
    assert sum(result[0] is not None for result in results) == 1
    assert sum(result[1] is not None for result in results) == 1

    async with PostgresUnitOfWork(session_factory) as work:
        loaded = await work.runs.get(persisted_run.id)
        events = await work.events.list_after(persisted_run.id, sequence=0)
    assert loaded.version == 1
    assert len(events) == 1
