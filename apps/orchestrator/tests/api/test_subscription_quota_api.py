from datetime import UTC, datetime

import pytest
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey


def _status() -> PoolQuotaStatus:
    return PoolQuotaStatus(
        key=QuotaPoolKey(provider="openai", account="team-a", pool="allowance"),
        revision=1,
        status="unknown",
        observed_at=datetime(2026, 9, 12, tzinfo=UTC),
        reason="probe_pending",
        reset_at=None,
        next_eligible_at=None,
        retry_basis=None,
    )


class FakeQuotaService:
    async def list(self, **kwargs):
        return [_status()]

    async def report_exhaustion(self, **kwargs):
        return _status()


@pytest.mark.asyncio
async def test_quota_routes_require_operator_and_csrf_for_report(
    task10_client, task10_route_context, route_headers
) -> None:
    task10_route_context.app.state.subscription_quota_service = FakeQuotaService()
    listing = await task10_client.get("/api/subscription-quota", headers=route_headers)
    assert listing.status_code == 200
    assert listing.json()[0]["status"] == "unknown"
    payload = {"provider": "openai", "account": "team-a", "pool": "allowance", "reason": "monthly limit"}
    denied = await task10_client.post(
        "/api/subscription-quota/reports",
        json=payload,
        headers={**route_headers, "Idempotency-Key": "quota-1", "X-CSRF-Token": "wrong"},
    )
    assert denied.status_code == 403
    accepted = await task10_client.post(
        "/api/subscription-quota/reports",
        json=payload,
        headers={**route_headers, "Idempotency-Key": "quota-1"},
    )
    assert accepted.status_code == 200


@pytest.mark.asyncio
async def test_quota_report_rejects_naive_reset_and_unknown_fields(
    task10_client, task10_route_context, route_headers
) -> None:
    task10_route_context.app.state.subscription_quota_service = FakeQuotaService()
    base = {"provider": "openai", "account": "team-a", "pool": "allowance", "reason": "x"}
    naive = await task10_client.post(
        "/api/subscription-quota/reports",
        json={**base, "reset_at": "2026-09-12T13:00:00"},
        headers={**route_headers, "Idempotency-Key": "quota-2"},
    )
    assert naive.status_code == 422
    extra = await task10_client.post(
        "/api/subscription-quota/reports",
        json={**base, "unexpected": "secret"},
        headers={**route_headers, "Idempotency-Key": "quota-3"},
    )
    assert extra.status_code == 422
