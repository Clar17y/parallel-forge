"""Short, separate PostgreSQL transactions for worker registration diagnostics."""

from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime
from itertools import islice
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.payload import validate_durable_payload
from forge.domain.subscription import AuthMode, RouteSpec
from forge.domain.subscription_readiness import ReadinessReason, SubscriptionRouteReadiness
from forge.observability.redaction import redact_value
from forge.persistence.models.subscription_runtime_status import SubscriptionWorkerStatus

RUNTIME_REPORT_SECONDS = 15
RUNTIME_FRESH_SECONDS = 45


class SubscriptionRuntimeStatusStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory, self._clock = session_factory, clock

    async def report(
        self, worker_instance_id: UUID, routes: Iterable[RouteSpec | SubscriptionRouteReadiness]
    ) -> bool:
        """Renew a diagnostic inventory; stopped instances cannot revive."""
        _identity(worker_instance_id)
        payload = _routes(routes)
        async with self._factory() as session:
            now = await self._now(session)
            statement = insert(SubscriptionWorkerStatus).values(
                worker_instance_id=worker_instance_id, routes=payload, last_seen_at=now
            )
            updated = await session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[SubscriptionWorkerStatus.worker_instance_id],
                    set_={
                        "last_seen_at": func.greatest(SubscriptionWorkerStatus.last_seen_at, now),
                        # A delayed reporter must not roll a newer snapshot back.
                        "routes": case(
                            (
                                statement.excluded.last_seen_at
                                >= SubscriptionWorkerStatus.last_seen_at,
                                statement.excluded.routes,
                            ),
                            else_=SubscriptionWorkerStatus.routes,
                        ),
                    },
                    where=SubscriptionWorkerStatus.stopped_at.is_(None),
                ).returning(SubscriptionWorkerStatus.worker_instance_id)
            )
            await session.commit()
            return updated is not None

    async def stop(self, worker_instance_id: UUID) -> None:
        _identity(worker_instance_id)
        async with self._factory() as session:
            now = await self._now(session)
            statement = insert(SubscriptionWorkerStatus).values(
                worker_instance_id=worker_instance_id, routes=[], last_seen_at=now, stopped_at=now
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SubscriptionWorkerStatus.worker_instance_id],
                    set_={"stopped_at": func.greatest(SubscriptionWorkerStatus.last_seen_at, now)},
                    where=SubscriptionWorkerStatus.stopped_at.is_(None),
                )
            )
            await session.commit()

    async def status(self, *, offset: int = 0, limit: int = 25) -> dict[str, object]:
        if (
            type(offset) is not int
            or type(limit) is not int
            or not 0 <= offset <= 1_000_000
            or not 1 <= limit <= 100
        ):
            raise ValueError("runtime status page bounds are invalid")
        async with self._factory() as session:
            now = await self._now(session)
            rows = list(
                await session.scalars(
                    select(SubscriptionWorkerStatus)
                    .order_by(
                        SubscriptionWorkerStatus.last_seen_at.desc(),
                        SubscriptionWorkerStatus.worker_instance_id,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return {
                "observed_at": now,
                "fresh_for_seconds": RUNTIME_FRESH_SECONDS,
                "workers": [_worker_view(row, now) for row in rows[:limit]],
                "has_more": len(rows) > limit,
            }

    async def _now(self, session: AsyncSession) -> datetime:
        now = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("runtime status clock must be timezone aware")
        return now


def _worker_view(row: SubscriptionWorkerStatus, now: datetime) -> dict[str, object]:
    state = (
        "stopped"
        if row.stopped_at is not None
        else "current"
        if 0 <= (now - row.last_seen_at).total_seconds() < RUNTIME_FRESH_SECONDS
        else "stale"
    )
    return {
        "worker_instance_id": row.worker_instance_id,
        "last_seen_at": row.last_seen_at,
        "stopped_at": row.stopped_at,
        "state": state,
        "routes": _display_routes(row.routes, stale=state != "current"),
    }


def _identity(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError("runtime worker instance must be a non-nil UUID")


def _routes(routes: Iterable[RouteSpec | SubscriptionRouteReadiness]) -> list[dict[str, object]]:
    values = tuple(islice(routes, 65))
    if len(values) > 64 or any(
        not isinstance(route, (RouteSpec, SubscriptionRouteReadiness)) for route in values
    ):
        raise ValueError("runtime subscription routes are invalid")
    unique: dict[RouteSpec, RouteSpec | SubscriptionRouteReadiness] = {}
    for route in values:
        value = route.route if isinstance(route, SubscriptionRouteReadiness) else route
        if value.auth_mode is not AuthMode.SUBSCRIPTION:
            raise ValueError("runtime subscription routes are invalid")
        existing = unique.get(value)
        if existing is not None and existing != route:
            raise ValueError("runtime subscription routes contain a duplicate identity")
        unique.setdefault(value, route)

    payload: list[dict[str, object]] = []
    for route in unique.values():
        value = route.route if isinstance(route, SubscriptionRouteReadiness) else route
        wire: dict[str, object]
        if isinstance(route, SubscriptionRouteReadiness):
            wire = route.wire()
        else:
            wire = {key: str(item) for key, item in asdict(route).items()}
        payload.append(wire)
    payload.sort(
        key=lambda route: (
            str(route["provider"]),
            str(route["client"]),
            str(route["model"]),
            str(route["effort"]),
        )
    )
    validate_durable_payload(payload)
    return payload


def _display_routes(routes: object, *, stale: bool) -> list[dict[str, object]]:
    """Decode legacy v1 inventory without treating it as readiness evidence."""
    if not isinstance(routes, list):
        return []
    result: list[dict[str, object]] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        value = dict(redact_value(route))
        if "schema_version" not in value:
            value.update(
                {
                    "schema_version": 1,
                    "configured": False,
                    "admitted": False,
                    "reason": ReadinessReason.UNKNOWN.value,
                    "quota": "unknown",
                    "warnings": [],
                }
            )
        else:
            value.setdefault("warnings", [])
        if stale:
            value["effective_reason"] = ReadinessReason.STALE_WORKER.value
        elif value.get("quota") == "blocked" and value.get("reason") == ReadinessReason.READY.value:
            value["effective_reason"] = ReadinessReason.QUOTA_EXHAUSTED.value
        else:
            value["effective_reason"] = value.get("reason", ReadinessReason.UNKNOWN.value)
        result.append(value)
    return result
