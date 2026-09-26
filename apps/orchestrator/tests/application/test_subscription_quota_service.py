from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from forge.api.schemas.subscription_quota import QuotaExhaustionReportRequest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_quota import SubscriptionQuotaService
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _status(status: str = "blocked") -> PoolQuotaStatus:
    return PoolQuotaStatus(
        key=QuotaPoolKey(provider="openai", account="team-a", pool="allowance"),
        revision=1,
        status=status,
        observed_at=NOW,
        reason="operator_report",
        reset_at=NOW + timedelta(hours=1),
        next_eligible_at=NOW + timedelta(hours=1),
        retry_basis="known_reset",
    )


class _Mutations:
    def __init__(self, replay: bool = False) -> None:
        self.replay = replay
        self.completed = 0

    async def reserve(self, **kwargs):
        from forge.application.ports.mutations import ApiMutationRecord

        return ApiMutationRecord(
            id=UUID(int=1), actor_id=kwargs["actor_id"], action=kwargs["action"],
            scope=kwargs["scope"], key_hash="hash", request_digest=kwargs["request_digest"],
            lifecycle_state="completed" if self.replay else "reserved", response_status=200 if self.replay else None,
            response_payload=None, resource_kind=None, resource_id=None, is_replay=self.replay,
        )

    async def complete(self, *_args, **_kwargs):
        self.completed += 1


class _Quota:
    def __init__(self, replay: bool = False) -> None:
        self.replay = replay
        self.reported = 0

    async def list_status(self, **_kwargs):
        return [_status()]

    async def status(self, _key):
        return _status()

    async def report_exhaustion(self, *_args, **_kwargs):
        self.reported += 1
        return _status()


class _Audit:
    def __init__(self) -> None:
        self.payload = None

    async def append(self, **kwargs):
        self.payload = kwargs["payload"]


class _Uow:
    def __init__(self, replay: bool = False) -> None:
        self.quota, self.mutations, self.audit = _Quota(replay), _Mutations(replay), _Audit()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_report_is_audited_and_uses_server_observed_operator_classifier() -> None:
    work = _Uow()
    service = SubscriptionQuotaService(lambda: work)
    result = await service.report_exhaustion(
        actor=LocalOperatorProfileActor(),
        idempotency_key="quota-report-1",
        request=QuotaExhaustionReportRequest(
            provider="openai", account="team-a", pool="allowance", reason="monthly limit",
        ),
    )
    assert result.status == "blocked"
    assert work.quota.reported == 1
    assert work.audit.payload["reason"] == "monthly limit"
    assert work.audit.payload["reset_at"] is None


@pytest.mark.asyncio
async def test_exact_replay_does_not_report_or_append_audit() -> None:
    work = _Uow(replay=True)
    service = SubscriptionQuotaService(lambda: work)
    await service.report_exhaustion(
        actor=LocalOperatorProfileActor(),
        idempotency_key="quota-report-1",
        request=QuotaExhaustionReportRequest(
            provider="openai", account="team-a", pool="allowance", reason="monthly limit",
            reset_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    assert work.quota.reported == 0
    assert work.audit.payload is None


@pytest.mark.asyncio
async def test_audit_reason_redacts_credential_shaped_text() -> None:
    work = _Uow()
    service = SubscriptionQuotaService(lambda: work)
    await service.report_exhaustion(
        actor=LocalOperatorProfileActor(),
        idempotency_key="quota-secret-1",
        request=QuotaExhaustionReportRequest(
            provider="openai", account="team-a", pool="allowance", reason="token=secret-value"
        ),
    )
    assert "secret-value" not in work.audit.payload["reason"]
