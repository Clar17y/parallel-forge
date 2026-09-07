"""Delivery controller changes participate in the caller's unit of work."""

from uuid import uuid4

import pytest
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_controller_step_and_event_roll_back_with_delivery_uow(
    session_factory, persisted_run
):
    step_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.controller_steps.admit(persisted_run.id, step_id, "validate", 1)
        events = await work.events.list_after(persisted_run.id, 0)
        assert any(event.event_type == "controller_step.admitted" for event in events)
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.controller_steps.get(persisted_run.id, step_id) is None
        events = await work.events.list_after(persisted_run.id, 0)
        assert not any(event.event_type == "controller_step.admitted" for event in events)
