"""Operator inspection and evidence reporting for shared quota pools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import MutationRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey
from forge.observability.redaction import redact_value


class SubscriptionQuotaServiceError(RuntimeError):
    pass


class QuotaExhaustionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=96)
    account: str = Field(min_length=1, max_length=255)
    pool: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=512)
    reset_at: datetime | None = None

    @model_validator(mode="after")
    def validate_reset(self) -> Self:
        if self.reset_at is not None and self.reset_at.tzinfo is None:
            raise ValueError("reset_at must include a timezone")
        return self


class QuotaActor(Protocol):
    @property
    def actor_id(self) -> UUID: ...


class QuotaOperatorRepository(Protocol):
    async def status(self, key: QuotaPoolKey) -> PoolQuotaStatus: ...

    async def list_status(self, *, offset: int = 0, limit: int = 100) -> list[PoolQuotaStatus]: ...

    async def report_exhaustion(
        self,
        key: QuotaPoolKey,
        exhaustion: QuotaExhaustion,
        *,
        idempotency_key: str,
        source_attempt_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PoolQuotaStatus: ...


class QuotaUnitOfWork(Protocol):
    quota: QuotaOperatorRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


class SubscriptionQuotaService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], QuotaUnitOfWork],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def list(self, *, offset: int = 0, limit: int = 100) -> Sequence[PoolQuotaStatus]:
        if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
            raise ValueError("invalid quota listing bounds")
        async with self._unit_of_work_factory() as work:
            values = await work.quota.list_status(offset=offset, limit=limit)
            await work.commit()
            return values

    async def report_exhaustion(
        self,
        *,
        actor: QuotaActor,
        idempotency_key: str,
        request: QuotaExhaustionReport,
    ) -> PoolQuotaStatus:
        if not isinstance(actor, (AuthenticatedActor,)) and not hasattr(actor, "actor_id"):
            raise TypeError("operator actor is required")
        body = request if isinstance(request, QuotaExhaustionReport) else QuotaExhaustionReport.model_validate(request)
        key = QuotaPoolKey(provider=body.provider, account=body.account, pool=body.pool)
        digest = _digest(body.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="subscription.quota.report_exhaustion",
                scope=f"quota:{key.provider}:{key.account}:{key.pool}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                result = await work.quota.status(key)
                await work.commit()
                return result
            observed_at = self._now()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("quota clock must be timezone-aware")
            observed_at = observed_at.astimezone(UTC)
            evidence = QuotaExhaustion(
                observed_at=observed_at, reason="operator_report", reset_at=body.reset_at
            )
            result = await work.quota.report_exhaustion(
                key,
                evidence,
                actor_id=actor.actor_id,
                idempotency_key=idempotency_key,
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="subscription.quota_exhaustion_reported",
                subject_type="subscription_quota_pool",
                subject_id=_subject_id(key),
                correlation_id=receipt.id,
                payload={
                    "request_digest": digest,
                    "provider": key.provider,
                    "account": key.account,
                    "pool": key.pool,
                    "reason": str(redact_value(body.reason)),
                    "reset_at": evidence.reset_at.isoformat() if evidence.reset_at else None,
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=200,
                response_payload=_status_payload(result),
                resource_kind="subscription_quota_pool",
                resource_id=_subject_id(key),
            )
            await work.commit()
            return result


def _status_payload(value: PoolQuotaStatus) -> dict[str, object]:
    return {
        "provider": value.key.provider,
        "account": value.key.account,
        "pool": value.key.pool,
        "revision": value.revision,
        "status": value.status,
    }


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _actor_source(actor: QuotaActor) -> str:
    return "web_session" if isinstance(actor, AuthenticatedActor) else "local_cli"


def _subject_id(key: QuotaPoolKey) -> UUID:
    raw = hashlib.sha256(f"{key.provider}:{key.account}:{key.pool}".encode()).digest()[:16]
    return UUID(bytes=raw)


__all__ = ["QuotaExhaustionReport", "SubscriptionQuotaService", "SubscriptionQuotaServiceError"]
