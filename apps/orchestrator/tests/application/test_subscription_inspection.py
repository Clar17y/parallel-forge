"""Projection limits are enforced before accessing persistence."""

from uuid import uuid4

import pytest
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery


@pytest.mark.asyncio
async def test_projection_rejects_unbounded_direct_queries():
    def unavailable():
        raise AssertionError("database must not be touched")

    query = SubscriptionTaskQuery(unavailable)
    with pytest.raises(ValueError):
        await query.tasks(uuid4(), offset=0, limit=101)
    with pytest.raises(ValueError):
        await query.attempts(uuid4(), uuid4(), offset=-1, limit=25)
