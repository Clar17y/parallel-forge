"""Approval evidence retains its run, task and attempt lineage."""

import pytest
from sqlalchemy import inspect


@pytest.mark.integration
async def test_plan_gate_lineage_is_restricted_and_run_lookup_indexed(session_factory):
    async with session_factory() as session:
        keys, indexes = await session.run_sync(
            lambda sync: (
                inspect(sync.bind).get_foreign_keys("subscription_plan_gates"),
                inspect(sync.bind).get_indexes("subscription_plan_gates"),
            )
        )
    lineage = {tuple(key["constrained_columns"]): key for key in keys}
    for column, table in (
        ("run_id", "runs"),
        ("task_id", "subscription_tasks"),
        ("attempt_id", "subscription_attempts"),
    ):
        assert lineage[(column,)]["referred_table"] == table
        assert lineage[(column,)]["options"].get("ondelete") == "RESTRICT"
    assert any(index["column_names"] == ["run_id"] for index in indexes)
