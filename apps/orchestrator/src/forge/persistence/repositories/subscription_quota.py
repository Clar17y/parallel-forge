"""Atomic quota admission and monotone exhaustion observations in PostgreSQL."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.operation import canonical_digest
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import (
    ExecutionEnvelope,
    LogicalTaskContract,
    RouteBinding,
    RouteMapping,
    RouteSpec,
    SpecialistPurpose,
    decode_subscription_record,
)
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPolicy, QuotaPoolKey
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaAdmission,
    SubscriptionQuotaObservation,
    SubscriptionQuotaPool,
)
from forge.persistence.repositories.subscription_launch import launches_confirmed


class QuotaAdmissionDenied(ValueError):
    """A blocked pool cannot issue launch authority."""


def exhaustion_payload(value: QuotaExhaustion) -> dict[str, object]:
    return {
        "observed_at": value.observed_at.astimezone(UTC).isoformat(),
        "reason": value.reason,
        "reset_at": value.reset_at.astimezone(UTC).isoformat() if value.reset_at else None,
    }


def _key(row: SubscriptionQuotaPool | SubscriptionQuotaAdmission) -> QuotaPoolKey:
    return QuotaPoolKey(row.provider, row.account, row.pool)


class PostgresSubscriptionQuotaRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        policy: QuotaPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self.policy = policy or QuotaPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("quota clock must be timezone-aware")
        return value.astimezone(UTC)

    def _status(self, key: QuotaPoolKey, row: SubscriptionQuotaPool | None) -> PoolQuotaStatus:
        if row is None:
            return PoolQuotaStatus(key, 0, "unknown", None, None, None, None, None)
        state: Literal["blocked", "unknown", "eligible"] = "unknown"
        if row.blocked:
            state = (
                "blocked"
                if (
                    row.probe_attempt_id is not None
                    or row.next_eligible_at is None
                    or row.next_eligible_at > self.now()
                )
                else "eligible"
            )
        return PoolQuotaStatus(
            key,
            row.revision,
            state,
            row.observed_at,
            row.reason,
            row.reset_at,
            row.next_eligible_at,
            cast(Literal["known_reset", "probe_cooldown"] | None, row.retry_basis),
            row.probe_attempt_id,
            row.recovered_at,
        )

    async def status(self, key: QuotaPoolKey) -> PoolQuotaStatus:
        row = await self._session.get(SubscriptionQuotaPool, (key.provider, key.account, key.pool))
        return self._status(key, row)

    async def list_status(self, *, offset: int = 0, limit: int = 100) -> list[PoolQuotaStatus]:
        if (
            type(offset) is not int
            or type(limit) is not int
            or not (0 <= offset <= 1_000_000 and 1 <= limit <= 100)
        ):
            raise ValueError("invalid quota status page")
        rows = await self._session.scalars(
            select(SubscriptionQuotaPool)
            .order_by(
                SubscriptionQuotaPool.provider,
                SubscriptionQuotaPool.account,
                SubscriptionQuotaPool.pool,
            )
            .offset(offset)
            .limit(limit)
        )
        return [self._status(_key(row), row) for row in rows]

    async def _lock(self, key: QuotaPoolKey) -> SubscriptionQuotaPool:
        await self._session.execute(
            insert(SubscriptionQuotaPool)
            .values(
                provider=key.provider,
                account=key.account,
                pool=key.pool,
                revision=0,
                blocked=False,
            )
            .on_conflict_do_nothing()
        )
        row = await self._session.get(
            SubscriptionQuotaPool,
            (key.provider, key.account, key.pool),
            with_for_update=True,
            populate_existing=True,
        )
        assert row is not None
        return row

    async def report_exhaustion(
        self,
        key: QuotaPoolKey,
        exhaustion: QuotaExhaustion,
        *,
        idempotency_key: str,
        source_attempt_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> PoolQuotaStatus:
        if not isinstance(key, QuotaPoolKey) or not isinstance(exhaustion, QuotaExhaustion):
            raise TypeError("typed quota evidence required")
        if (source_attempt_id is None) == (actor_id is None):
            raise ValueError("quota evidence requires exactly one source")
        source = source_attempt_id or actor_id
        if (
            not isinstance(source, UUID)
            or source.int == 0
            or not idempotency_key
            or len(idempotency_key) > 255
        ):
            raise ValueError("quota evidence source is invalid")
        if source_attempt_id is not None:
            admission = await self._session.get(SubscriptionQuotaAdmission, source_attempt_id)
            if admission is None or _key(admission) != key:
                raise ValueError("quota evidence differs from admitted account")
        row = await self._lock(key)
        source_key = canonical_digest(
            {
                "provider": key.provider,
                "account": key.account,
                "pool": key.pool,
                "attempt": str(source_attempt_id) if source_attempt_id else None,
                "actor": str(actor_id) if actor_id else None,
                "key": idempotency_key,
            }
        )
        payload = exhaustion_payload(exhaustion)
        digest = canonical_digest(payload)
        prior = await self._session.scalar(
            select(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_key == source_key
            )
        )
        if prior is not None:
            if prior.evidence_digest != digest:
                raise ValueError("quota evidence replay conflicts")
            return self._status(key, row)
        eligible = exhaustion.reset_at or (
            exhaustion.observed_at + timedelta(seconds=self.policy.unknown_reset_cooldown_seconds)
        )
        basis = "known_reset" if exhaustion.reset_at else "probe_cooldown"
        self._session.add(
            SubscriptionQuotaObservation(
                id=uuid4(),
                provider=key.provider,
                account=key.account,
                pool=key.pool,
                source_key=source_key,
                source_attempt_id=source_attempt_id,
                actor_id=actor_id,
                observed_at=exhaustion.observed_at,
                reason=exhaustion.reason,
                reset_at=exhaustion.reset_at,
                next_eligible_at=eligible,
                retry_basis=basis,
                evidence_digest=digest,
            )
        )
        # Every new confirmed observation invalidates all older success permits.
        # Preserve the longer active horizon even if later evidence is shorter.
        if not row.blocked or row.next_eligible_at is None or eligible >= row.next_eligible_at:
            row.next_eligible_at, row.reset_at, row.retry_basis = (
                eligible,
                exhaustion.reset_at,
                basis,
            )
        if row.observed_at is None or exhaustion.observed_at >= row.observed_at:
            row.observed_at, row.reason = exhaustion.observed_at, exhaustion.reason
        row.revision += 1
        row.blocked = True
        row.recovered_at = None
        await self._session.flush()
        return self._status(key, row)

    async def _recover_probe(self, row: SubscriptionQuotaPool) -> None:
        """Only durable stopped/expired authority permits reclaiming a lost probe."""
        if row.probe_attempt_id is None:
            return
        run_id = await self._session.scalar(
            select(SubscriptionAttempt.run_id).where(SubscriptionAttempt.id == row.probe_attempt_id)
        )
        if run_id is None:
            return
        # Renewal and launch intent hold the run lock until commit. Reading
        # their old MVCC rows without that fence could reclaim a live probe.
        # Never wait pool -> run: settlement takes run -> pool.
        locked = await self._session.scalar(
            select(Run.id).where(Run.id == run_id).with_for_update(skip_locked=True)
        )
        if locked is None:
            return
        admission = await self._session.get(
            SubscriptionQuotaAdmission, row.probe_attempt_id, populate_existing=True
        )
        attempt = await self._session.get(
            SubscriptionAttempt, row.probe_attempt_id, populate_existing=True
        )
        if admission is None or attempt is None:
            return  # No proof: preserve the fence.
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == attempt.run_id,
                SubscriptionScheduledTask.task_id == attempt.task_row_id,
            )
            .execution_options(populate_existing=True)
        )
        if admission.finished_at is None and (
            scheduled is None
            or (
                scheduled.lease_owner == attempt.lease_owner
                and scheduled.lease_generation == attempt.lease_generation
                and scheduled.lease_expires_at is not None
                and scheduled.lease_expires_at > self.now()
            )
        ):
            return
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == attempt.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        if not launches_confirmed(
            launches, None, require_decision=False, worker_identity=attempt.lease_owner
        ):
            return
        # Lease expiry alone never proves an existing client stopped. Empty
        # launch history plus revoked lease proves it cannot acquire launch authority.
        admission.finished_at = admission.finished_at or self.now()
        row.probe_attempt_id = None
        if row.revision == admission.revision:
            self._cooldown(row)
        await self._session.flush()

    def _cooldown(self, row: SubscriptionQuotaPool) -> None:
        eligible = self.now() + timedelta(seconds=self.policy.unknown_reset_cooldown_seconds)
        if row.next_eligible_at is None or eligible > row.next_eligible_at:
            row.next_eligible_at, row.reset_at, row.retry_basis = eligible, None, "probe_cooldown"
        row.blocked = True

    async def eligible(self, route: RouteSpec) -> bool:
        key = self.policy.key_for(route)
        row = await self._lock(key)
        await self._recover_probe(row)
        return self._status(key, row).allows_attempt

    async def route_for_task(
        self,
        logical: SubscriptionTask,
        *,
        eligible_routes: frozenset[RouteSpec] | None = None,
    ) -> RouteBinding | None:
        """Choose only frozen, safe specialist fallback; caller owns run/task fences."""
        contract = decode_subscription_record(logical.payload)
        if not isinstance(contract, LogicalTaskContract):
            raise TypeError("invalid quota task")
        available = eligible_routes is None or contract.route.effective in eligible_routes
        if available and await self.eligible(contract.route.effective):
            return contract.route
        if contract.purpose is SpecialistPurpose.PRIMARY or contract.route.is_primary:
            return None
        stored = await self._session.get(SubscriptionEnvelope, contract.run_id)
        envelope = decode_subscription_record(stored.payload) if stored else None
        if not isinstance(envelope, ExecutionEnvelope):
            return None
        try:
            preferred = envelope.route_for(contract.purpose)
        except KeyError:
            return None
        if contract.route.requested != preferred.requested:
            return None
        # After a fallback exhausts, the frozen preferred pool may be eligible
        # again. Preserve its exact mapping instead of manufacturing a new one.
        for route in (preferred.effective, *envelope.fallbacks_for(contract.purpose)):
            if eligible_routes is not None and route not in eligible_routes:
                continue
            if route.billing_mode is not contract.route.effective.billing_mode:
                continue
            if route.auth_mode is not contract.route.effective.auth_mode:
                continue
            if not await self.eligible(route):
                continue
            if route == preferred.effective:
                return preferred
            mapping = RouteMapping(
                requested=contract.route.requested,
                effective=route,
                approved_by=f"profile:{envelope.profile_id}",
                approval_id=f"profile:{envelope.profile_id}:version:{envelope.profile_version}",
                reason=(
                    "Confirmed quota exhaustion; frozen specialist fallback"
                    if available
                    else "Runtime route unavailable; frozen specialist fallback"
                ),
            )
            return replace(contract.route, effective=route, mapping_applied=mapping)
        return None

    async def admit(self, route: RouteSpec, attempt_id: UUID) -> None:
        key = self.policy.key_for(route)
        row = await self._lock(key)
        await self._recover_probe(row)
        if not self._status(key, row).allows_attempt:
            raise QuotaAdmissionDenied("provider quota pool is blocked")
        prior = await self._session.get(SubscriptionQuotaAdmission, attempt_id)
        if prior is not None:
            raise ValueError("quota admission cannot be issued twice")
        self._session.add(
            SubscriptionQuotaAdmission(
                attempt_id=attempt_id,
                provider=key.provider,
                account=key.account,
                pool=key.pool,
                revision=row.revision,
                admitted_at=self.now(),
                probe=row.blocked,
            )
        )
        if row.blocked:
            row.probe_attempt_id = attempt_id
        await self._session.flush()

    async def settle(
        self,
        attempt_id: UUID,
        *,
        exhaustion: QuotaExhaustion | None,
        succeeded: bool,
        stopped: bool,
    ) -> None:
        admission = await self._session.get(SubscriptionQuotaAdmission, attempt_id)
        if admission is None:
            # Pre-migration attempts have no account binding; do not invent one.
            return
        key = _key(admission)
        # Recovery locks the pool before updating its admission. Settlement must
        # use that same order, including a late response from an expired worker.
        row = await self._lock(key)
        admission = await self._session.get(
            SubscriptionQuotaAdmission,
            attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        assert admission is not None
        if exhaustion is not None:
            await self.report_exhaustion(
                key, exhaustion, source_attempt_id=attempt_id, idempotency_key="provider-result"
            )
        if not stopped:
            return
        admission.finished_at = self.now()
        if row.probe_attempt_id == attempt_id:
            row.probe_attempt_id = None
            if succeeded and exhaustion is None and row.revision == admission.revision:
                row.blocked = False
                row.recovered_at = self.now()
            elif exhaustion is None and row.revision == admission.revision:
                self._cooldown(row)
        await self._session.flush()
