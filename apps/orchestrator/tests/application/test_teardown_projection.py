"""Terminal state alone never grants resource-removal authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projections import ProjectionService


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [None, False, True])
@pytest.mark.parametrize("state", ["CANCELLED", "AWAITING_HUMAN_INTERVENTION"])
async def test_teardown_requires_positive_server_resource_eligibility(eligible, state):
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    result = {"run": {"state": state, "version": 3}, "candidate": {}, "next_gate": None}
    if eligible is not None:
        result["teardown_eligible"] = eligible
    query = SimpleNamespace(run_projection=AsyncMock(return_value=result))
    projection = await ProjectionService(query).run_projection(uuid4(), actor)
    assert (
        "teardown_run_resources" in [item["name"] for item in projection["available_commands"]]
    ) is (eligible is True)
    assert "teardown_eligible" not in projection
