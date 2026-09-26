"""Subscription usage is authenticated, bounded and honest about missing data."""

from copy import deepcopy
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
from forge.application.services.auth import AuthenticationError
from pydantic import ValidationError


def _page():
    unknown = {"known_total": None, "measured_attempts": 0, "unknown_attempts": 2}
    return {
        "items": [
            {
                "project_id": str(uuid4()),
                "run_id": str(uuid4()),
                "purpose": "implementation",
                "effective_route": {
                    "provider": "google",
                    "client": "gemini_cli",
                    "model": "flash",
                    "effort": "medium",
                    "auth_mode": "subscription",
                    "billing_mode": "allowance_only",
                },
                "currency": None,
                "attempts": 2,
                "recorded_results": 1,
                "failed_results": 1,
                "pending_results": 1,
                "input_tokens": {"known_total": 0, "measured_attempts": 1, "unknown_attempts": 1},
                **{
                    name: deepcopy(unknown)
                    for name in (
                        "output_tokens",
                        "cached_input_tokens",
                        "duration_ms",
                        "tool_calls",
                        "named_checks",
                        "estimated_api_cost_minor",
                    )
                },
            }
        ],
        "has_more": False,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempts", -1),
        ("attempts", True),
        ("attempts", "2"),
        ("attempts", 0),
        ("recorded_results", 3),
        ("failed_results", 2),
        ("pending_results", 0),
    ],
)
def test_usage_schema_rejects_impossible_result_counts(field, value):
    payload = _page()
    payload["items"][0][field] = value
    with pytest.raises(ValidationError):
        SubscriptionUsagePage.model_validate(payload)


@pytest.mark.parametrize(
    "metric",
    [
        {"known_total": -1, "measured_attempts": 1, "unknown_attempts": 1},
        {"known_total": True, "measured_attempts": 1, "unknown_attempts": 1},
        {"known_total": None, "measured_attempts": 1, "unknown_attempts": 1},
        {"known_total": 0, "measured_attempts": 0, "unknown_attempts": 2},
        {"known_total": 0, "measured_attempts": 1, "unknown_attempts": 0},
    ],
)
def test_usage_schema_rejects_impossible_measurements(metric):
    payload = _page()
    payload["items"][0]["input_tokens"] = metric
    with pytest.raises(ValidationError):
        SubscriptionUsagePage.model_validate(payload)


def test_usage_schema_does_not_invent_a_cost_currency():
    payload = _page()
    payload["items"][0]["estimated_api_cost_minor"] = {
        "known_total": 10,
        "measured_attempts": 1,
        "unknown_attempts": 1,
    }
    with pytest.raises(ValidationError):
        SubscriptionUsagePage.model_validate(payload)


class Query:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def usage(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _assessment():
    unknown = {
        "numerator": None,
        "denominator": 10,
        "primary_attempts": 1,
        "all_attempts": 2,
        "numerator_measured_attempts": 0,
        "numerator_unknown_attempts": 1,
        "denominator_measured_attempts": 1,
        "denominator_unknown_attempts": 1,
        "coverage": 0.5,
        "share": None,
    }
    return {
        "primary_turns": 1,
        "all_attempts": 2,
        "delegation_decisions": 0,
        "wait_decisions": 0,
        "repair_debits": 0,
        "fallback_attempts": 0,
        "preferred_attempts": 2,
        "unknown_route_attempts": 0,
        "unverified_decisions": 0,
        "shares": {
            name: deepcopy(unknown) for name in ("input_tokens", "output_tokens", "duration_ms")
        },
        "waits": {
            "decisions": 0,
            "continued": 0,
            "unfinished": 0,
            "ended_without_continuation": 0,
            "measured_intervals": 0,
            "unknown_intervals": 0,
            "elapsed_ms": None,
        },
        "outcomes": [],
        "outcomes_has_more": False,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("all_attempts", -1),
        ("all_attempts", True),
        ("primary_turns", 3),
        ("fallback_attempts", 1),
        ("repair_debits", 3),
        ("unverified_decisions", 3),
        ("wait_decisions", 1),
        ("outcomes_has_more", "false"),
    ],
)
def test_assessment_schema_rejects_inconsistent_scalar_counts(field, value):
    from forge.api.schemas.subscription_usage import SubscriptionUsageAssessment

    payload = _assessment()
    payload[field] = value
    with pytest.raises(ValidationError):
        SubscriptionUsageAssessment.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("numerator", 0),
        ("denominator", None),
        ("share", 0.0),
        ("coverage", 1.0),
        ("numerator_unknown_attempts", 0),
        ("denominator_measured_attempts", True),
        ("coverage", float("nan")),
        ("share", "0.5"),
    ],
)
def test_assessment_schema_rejects_fabricated_measurements(field, value):
    from forge.api.schemas.subscription_usage import SubscriptionUsageAssessment

    payload = _assessment()
    payload["shares"]["input_tokens"][field] = value
    with pytest.raises(ValidationError):
        SubscriptionUsageAssessment.model_validate(payload)


def test_assessment_schema_requires_facts_and_closed_metric_names():
    from forge.api.schemas.subscription_usage import SubscriptionUsageAssessment

    for missing in ("repair_debits", "waits", "unknown_route_attempts"):
        payload = _assessment()
        del payload[missing]
        with pytest.raises(ValidationError):
            SubscriptionUsageAssessment.model_validate(payload)
    payload = _assessment()
    payload["shares"]["invented_allowance"] = payload["shares"]["input_tokens"]
    with pytest.raises(ValidationError):
        SubscriptionUsageAssessment.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [("continued", 1), ("unknown_intervals", 1), ("elapsed_ms", 0), ("unfinished", True)],
)
def test_assessment_wait_schema_requires_matching_interval_coverage(field, value):
    from forge.api.schemas.subscription_usage import SubscriptionUsageAssessment

    payload = _assessment()
    payload["waits"][field] = value
    with pytest.raises(ValidationError):
        SubscriptionUsageAssessment.model_validate(payload)


def _outcome():
    item = _page()["items"][0]
    return {
        **{
            key: item[key]
            for key in ("project_id", "run_id", "purpose", "effective_route", "currency")
        },
        "attempts": 2,
        "distinct_tasks": 1,
        "terminal_tasks": 1,
        "verified_results": 1,
        "unverified_results": 0,
        "pending_results": 1,
        "failed_results": 1,
        "applied_decisions": 0,
        "completed_handoffs": 0,
        "task_acceptances": 0,
        "fallback_attempts": 0,
        "latest_fallback_reason": None,
        "latest_result_disposition": "failed",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("distinct_tasks", 3),
        ("terminal_tasks", 2),
        ("pending_results", 0),
        ("applied_decisions", 1),
        ("task_acceptances", 1),
        ("completed_handoffs", 1),
        ("latest_result_disposition", None),
        ("latest_fallback_reason", "invented"),
    ],
)
def test_assessment_outcome_schema_rejects_result_and_task_conflation(field, value):
    from forge.api.schemas.subscription_usage import SubscriptionUsageOutcome

    payload = _outcome()
    payload[field] = value
    with pytest.raises(ValidationError):
        SubscriptionUsageOutcome.model_validate(payload)


def test_assessment_detail_page_cannot_hide_different_route_or_pagination_scope():
    value = _page()
    outcome = _outcome()
    for name in ("project_id", "run_id", "purpose", "effective_route", "currency"):
        outcome[name] = deepcopy(value["items"][0][name])
    value["assessment"] = _assessment()
    value["assessment"]["outcomes"] = [outcome]
    SubscriptionUsagePage.model_validate(value)
    for mutation in ("route", "has_more"):
        changed = deepcopy(value)
        if mutation == "route":
            changed["assessment"]["outcomes"][0]["effective_route"]["client"] = "different-client"
        else:
            changed["assessment"]["outcomes_has_more"] = True
        with pytest.raises(ValidationError):
            SubscriptionUsagePage.model_validate(changed)


async def test_usage_api_explicitly_selects_assessment_without_changing_default_query(
    task10_client,
    task10_route_context,
    route_headers,
):
    value = {"items": [], "has_more": False, "assessment": _assessment()}
    query = Query(value)
    task10_route_context.app.state.subscription_usage_query = query
    response = await task10_client.get(
        "/api/subscription-usage?include_assessment=true", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200 and response.json() == value
    assert query.calls == [{"run_id": None, "offset": 0, "limit": 25, "include_assessment": True}]


async def test_usage_api_serializes_durable_uuid_values_without_optional_assessment(
    task10_client, task10_route_context, route_headers
):
    from uuid import UUID

    expected = _page()
    stored = deepcopy(expected)
    for name in ("run_id", "project_id"):
        stored["items"][0][name] = UUID(stored["items"][0][name])
    task10_route_context.app.state.subscription_usage_query = Query(stored)
    response = await task10_client.get(
        "/api/subscription-usage", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200 and response.json() == expected


async def test_usage_api_rejects_invalid_measurements_without_optional_assessment(
    task10_client, task10_route_context, route_headers
):
    stored = _page()
    stored["items"][0]["input_tokens"]["known_total"] = None
    task10_route_context.app.state.subscription_usage_query = Query(stored)
    with pytest.raises(ValidationError):
        await task10_client.get("/api/subscription-usage", headers={"Host": route_headers["Host"]})


@pytest.mark.asyncio
async def test_usage_api_preserves_zero_and_unknown_and_bounds_query(
    task10_client,
    task10_route_context,
    route_headers,
):
    page = _page()
    query = Query(page)
    task10_route_context.app.state.subscription_usage_query = query
    headers = {"Host": route_headers["Host"]}
    run_id = uuid4()
    response = await task10_client.get(
        f"/api/subscription-usage?run_id={run_id}&offset=2&limit=20",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == page
    assert query.calls == [{"run_id": run_id, "offset": 2, "limit": 20}]
    for parameter in ("limit=0", "limit=101", "offset=-1", "offset=1000001", "run_id=invalid"):
        assert (
            await task10_client.get(f"/api/subscription-usage?{parameter}", headers=headers)
        ).status_code == 422
    assert len(query.calls) == 1


@pytest.mark.asyncio
async def test_usage_api_authentication_precedes_projection(
    task10_client,
    task10_route_context,
    route_headers,
):
    query = Query(_page())
    task10_route_context.app.state.subscription_usage_query = query
    task10_route_context.auth.error = AuthenticationError()
    response = await task10_client.get(
        "/api/subscription-usage", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 401
    assert query.calls == []


@pytest.mark.asyncio
async def test_usage_api_distinguishes_empty_missing_and_unavailable(
    task10_client,
    task10_route_context,
    route_headers,
):
    headers = {"Host": route_headers["Host"]}
    state = task10_route_context.app.state
    state.subscription_usage_query = Query({"items": [], "has_more": False})
    assert (await task10_client.get("/api/subscription-usage", headers=headers)).json() == {
        "items": [],
        "has_more": False,
    }
    state.subscription_usage_query = Query(None)
    assert (
        await task10_client.get(f"/api/subscription-usage?run_id={uuid4()}", headers=headers)
    ).status_code == 404
    state.subscription_usage_query = None
    assert (await task10_client.get("/api/subscription-usage", headers=headers)).status_code == 503
