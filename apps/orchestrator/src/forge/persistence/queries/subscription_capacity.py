"""Read-only capacity observations use the same accounting as launch admission."""

from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.observability.redaction import redact_value
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository


@dataclass(frozen=True, slots=True)
class CapacityLimit:
    active: int
    limit: int


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    provider: str
    active: int
    limit: int


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    observed_at: datetime
    policy_version: int
    host: CapacityLimit
    run: CapacityLimit
    providers: tuple[ProviderCapacity, ...]
    queue_order: str = "least_recently_served_run_then_oldest_task"

    def projection(self) -> dict[str, object]:
        value = asdict(self)
        value["providers"] = [
            {**asdict(provider), "provider": str(redact_value(provider.provider))}
            for provider in self.providers
        ]
        return value

    def waits(self, provider: str) -> list[str]:
        dimensions = [("host", self.host), ("run", self.run)]
        selected = next((value for value in self.providers if value.provider == provider), None)
        if selected is not None:
            dimensions.append(("provider", CapacityLimit(selected.active, selected.limit)))
        return [name for name, value in dimensions if value.active >= value.limit]


async def capacity_observation(
    session: AsyncSession, run: SubscriptionSchedulerRun | None, providers: set[str]
) -> CapacityObservation | None:
    if run is None or not run.admitted:
        return None
    # The caller's repeatable-read transaction gives all dimensions one snapshot.
    # These scheduler methods only read. In particular they share the existing
    # proof that excludes stopped pauses while retaining unproved stops.
    scheduler = PostgresSchedulingRepository(session)
    policy = await scheduler._current_policy()
    observed_at = await session.scalar(select(func.transaction_timestamp()))
    if not isinstance(observed_at, datetime):
        raise TypeError("capacity observation clock is unavailable")
    return CapacityObservation(
        observed_at=observed_at,
        policy_version=policy.version,
        host=CapacityLimit(await scheduler._active_count(), policy.global_limit),
        run=CapacityLimit(
            await scheduler._active_count(run_id=run.run_id), run.effective_run_limit
        ),
        providers=tuple(
            [
                ProviderCapacity(
                    provider,
                    await scheduler._active_count(provider=provider),
                    policy.provider_limit,
                )
                for provider in sorted(providers)
            ]
        ),
    )
