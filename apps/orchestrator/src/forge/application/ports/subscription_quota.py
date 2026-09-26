"""Durable provider quota pool boundary."""

from typing import Protocol
from uuid import UUID

from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPolicy, QuotaPoolKey


class SubscriptionQuotaRepository(Protocol):
    policy: QuotaPolicy

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
