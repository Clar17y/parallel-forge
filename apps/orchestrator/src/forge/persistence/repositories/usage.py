"""PostgreSQL persistence and grouped totals for model usage evidence."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.observability.usage import PricingCatalog, UsageRecord
from forge.persistence.models import AgentExecution, ModelUsage
from forge.persistence.repositories.runs import PersistenceDataError


class UsageRepositoryError(RuntimeError):
    """Usage evidence cannot be attached to the requested execution."""


class UsageNotFound(UsageRepositoryError):
    """The requested usage record does not exist."""


class UsageRepository:
    """Persist one priced usage row per explicit short transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        run_id: UUID,
        agent_execution_id: UUID,
        usage: UsageRecord,
        *,
        catalog: PricingCatalog,
        currency: str = "USD",
    ) -> UsageRecord:
        """Price with the supplied catalog version and persist the evidence."""

        return await self.add_priced(
            run_id,
            agent_execution_id,
            catalog.price(usage, currency=currency),
        )

    async def add_priced(
        self,
        run_id: UUID,
        agent_execution_id: UUID,
        usage: UsageRecord,
    ) -> UsageRecord:
        """Persist a record that already has a known or explicitly unknown price."""

        if not isinstance(run_id, UUID) or not isinstance(agent_execution_id, UUID):
            raise TypeError("usage run and agent execution identifiers must be UUID values")
        if usage.pricing_version is None or usage.currency is None:
            raise ValueError("usage must be priced or explicitly unknown before persistence")
        if usage.run_id not in {None, run_id} or usage.agent_execution_id not in {
            None,
            agent_execution_id,
        }:
            raise UsageRepositoryError("usage identity conflicts with the persistence target")

        record_id = usage.id or uuid4()
        try:
            async with self._session_factory() as session, session.begin():
                execution = await session.scalar(
                    select(AgentExecution).where(
                        AgentExecution.id == agent_execution_id,
                        AgentExecution.run_id == run_id,
                    )
                )
                if execution is None:
                    raise UsageRepositoryError(
                        "agent execution does not belong to the requested run"
                    )
                if execution.provider != usage.provider or execution.model != usage.model:
                    raise UsageRepositoryError(
                        "usage provider and model do not match the agent execution"
                    )
                record = ModelUsage(
                    id=record_id,
                    run_id=run_id,
                    agent_execution_id=agent_execution_id,
                    provider=usage.provider,
                    model=usage.model,
                    prompt_version=usage.prompt_version,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    duration_ms=usage.duration_ms,
                    tool_call_count=usage.tool_call_count,
                    provider_request_id=usage.provider_request_id,
                    pricing_version=usage.pricing_version,
                    estimated_cost_minor=usage.estimated_cost_minor,
                    currency=usage.currency,
                    unknown_price_reason=usage.unknown_price_reason,
                )
                session.add(record)
                await session.flush()
                await session.refresh(record)
                return _usage_from_record(record)
        except IntegrityError as error:
            raise UsageRepositoryError("usage evidence violated a database invariant") from error

    async def get(self, usage_id: UUID) -> UsageRecord:
        """Load one usage record and fail closed on malformed stored values."""

        async with self._session_factory() as session:
            record = await session.get(ModelUsage, usage_id)
            if record is None:
                raise UsageNotFound(f"usage record {usage_id} was not found")
            return _usage_from_record(record)

    async def totals(self, run_id: UUID) -> tuple[Mapping[str, object], ...]:
        """Aggregate deterministically by provider, model, and currency."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    ModelUsage.provider,
                    ModelUsage.model,
                    ModelUsage.currency,
                    func.count(ModelUsage.id).label("request_count"),
                    func.sum(ModelUsage.input_tokens).label("input_tokens"),
                    func.sum(ModelUsage.cached_input_tokens).label("cached_input_tokens"),
                    func.sum(ModelUsage.output_tokens).label("output_tokens"),
                    func.sum(ModelUsage.duration_ms).label("duration_ms"),
                    func.sum(ModelUsage.tool_call_count).label("tool_call_count"),
                    func.sum(func.coalesce(ModelUsage.estimated_cost_minor, 0)).label(
                        "known_estimated_cost_minor"
                    ),
                    func.sum(case((ModelUsage.estimated_cost_minor.is_(None), 1), else_=0)).label(
                        "unknown_price_count"
                    ),
                )
                .where(ModelUsage.run_id == run_id)
                .group_by(ModelUsage.provider, ModelUsage.model, ModelUsage.currency)
                .order_by(ModelUsage.provider, ModelUsage.model, ModelUsage.currency)
            )
            totals: list[Mapping[str, object]] = []
            for row in result:
                unknown_count = int(row.unknown_price_count or 0)
                totals.append(
                    {
                        "provider": row.provider,
                        "model": row.model,
                        "currency": row.currency,
                        "request_count": int(row.request_count or 0),
                        "input_tokens": int(row.input_tokens or 0),
                        "cached_input_tokens": int(row.cached_input_tokens or 0),
                        "output_tokens": int(row.output_tokens or 0),
                        "duration_ms": int(row.duration_ms or 0),
                        "tool_call_count": int(row.tool_call_count or 0),
                        "known_estimated_cost_minor": int(row.known_estimated_cost_minor or 0),
                        "unknown_price_count": unknown_count,
                        "estimate_complete": unknown_count == 0,
                    }
                )
            return tuple(totals)


def _usage_from_record(record: ModelUsage) -> UsageRecord:
    try:
        return UsageRecord(
            id=record.id,
            run_id=record.run_id,
            agent_execution_id=record.agent_execution_id,
            provider=record.provider,
            model=record.model,
            prompt_version=record.prompt_version,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cached_input_tokens=record.cached_input_tokens,
            duration_ms=record.duration_ms,
            tool_call_count=record.tool_call_count,
            provider_request_id=record.provider_request_id,
            pricing_version=record.pricing_version,
            estimated_cost_minor=record.estimated_cost_minor,
            currency=record.currency,
            unknown_price_reason=record.unknown_price_reason,
            created_at=record.created_at,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("persisted model usage is malformed") from error


__all__ = ["UsageNotFound", "UsageRepository", "UsageRepositoryError"]
