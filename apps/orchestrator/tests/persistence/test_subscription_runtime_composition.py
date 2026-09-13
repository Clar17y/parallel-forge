"""Composed registry reports use the same exact pins as launch admission."""

import pytest
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PRIMARY, WRITER, prepared_counter_case


@pytest.mark.integration
async def test_composed_worker_reports_registry_without_spending_attempts(
    session_factory, tmp_path
):
    case = await prepared_counter_case(session_factory, tmp_path)
    reporter = case.handlers.subscription_status
    try:
        async with case.factory() as work:
            before = await work.subscription_budget.usage(case.run.id)
        requests = len(case.script.requests)
        await reporter.publish()
        view = await SubscriptionRuntimeStatusStore(session_factory).status()
        assert len(view["workers"]) == 1
        report = view["workers"][0]
        assert report["state"] == "current" and report["worker_instance_id"] == reporter.instance_id
        assert {row["model"] for row in report["routes"]} == {PRIMARY.model, WRITER.model}
        assert all(row["billing_mode"] == "allowance_only" for row in report["routes"])
        async with case.factory() as work:
            assert await work.subscription_budget.usage(case.run.id) == before
        assert len(case.script.requests) == requests
    finally:
        await case.handlers.aclose()
        await reporter.close()
    assert (await SubscriptionRuntimeStatusStore(session_factory).status())["workers"][0][
        "state"
    ] == "stopped"
