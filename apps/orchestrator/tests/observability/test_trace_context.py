"""Tests for task-local correlation context."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from forge.observability.context import CorrelationContext, bind_context, current_context


def test_nested_context_merges_and_restores_after_exception() -> None:
    run_id = uuid4()
    step_id = uuid4()

    assert current_context() == CorrelationContext()
    with pytest.raises(RuntimeError), bind_context(run_id=run_id):
        assert current_context().to_dict() == {"run_id": str(run_id)}
        with bind_context(step_id=step_id):
            assert current_context().to_dict() == {
                "run_id": str(run_id),
                "step_id": str(step_id),
            }
        raise RuntimeError("restore")
    assert current_context() == CorrelationContext()


async def _read_in_scope(identifier: UUID) -> CorrelationContext:
    with bind_context(run_id=identifier):
        await asyncio.sleep(0)
        return current_context()


async def _read_parallel(first: UUID, second: UUID) -> list[CorrelationContext]:
    return list(await asyncio.gather(_read_in_scope(first), _read_in_scope(second)))


def test_context_does_not_leak_between_async_tasks() -> None:
    first = uuid4()
    second = uuid4()
    values = asyncio.run(_read_parallel(first, second))

    assert {item.run_id for item in values} == {first, second}
    assert current_context() == CorrelationContext()


def test_context_serializes_only_non_null_uuid_values() -> None:
    context = CorrelationContext(command_id=uuid4(), operation_intent_id=uuid4())

    assert set(context.to_dict()) == {"command_id", "operation_intent_id"}
    assert all(isinstance(value, str) for value in context.to_dict().values())
