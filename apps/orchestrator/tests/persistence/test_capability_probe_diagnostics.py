"""Safe live-probe failures are durable diagnostics, never runtime authority."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from forge.domain.capability_evidence import CapabilityEvidenceIdentity
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    SpecialistPurpose,
)
from forge.domain.subscription_readiness import ReadinessReason
from forge.domain.tool import ToolName
from forge.persistence.repositories.capability_probe_diagnostics import (
    PostgresCapabilityProbeDiagnosticStore,
)
from sqlalchemy import text


@pytest_asyncio.fixture(autouse=True)
async def _remove_diagnostics(session_factory):
    yield
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM capability_probe_diagnostics"))


def identity(*, model: str = "gpt-6-astra") -> CapabilityEvidenceIdentity:
    return CapabilityEvidenceIdentity(
        provider="openai",
        client="codex_app_server",
        client_version="0.154.0",
        executable_digest="1" * 64,
        client_home_digest="2" * 64,
        account="3" * 64,
        model=model,
        effort=ReasoningEffort.LOW,
        role=SpecialistPurpose.PRIMARY,
        tool_surface=(ToolName.REPOSITORY_READ_FILE,),
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


@pytest.mark.integration
async def test_report_resolve_update_and_expiry_are_identity_bound(
    session_factory,
) -> None:
    clock = [datetime(2026, 9, 20, 12, tzinfo=UTC)]
    store = PostgresCapabilityProbeDiagnosticStore(session_factory, clock=lambda: clock[0])
    selected = identity()

    first = await store.report(
        selected,
        ReadinessReason.SIGNED_OUT,
        observed_at=clock[0],
        expires_at=clock[0] + timedelta(hours=1),
    )
    assert first.revision == 1 and first.reason is ReadinessReason.SIGNED_OUT
    assert await store.resolve(selected) == first
    assert await store.resolve(identity(model="gpt-5.6-sol")) is None

    clock[0] += timedelta(minutes=5)
    second = await store.report(
        selected,
        ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND,
        observed_at=clock[0],
        expires_at=clock[0] + timedelta(hours=1),
    )
    assert second.revision == 2
    assert (await store.resolve(selected)).reason is ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND

    delayed = await store.report(
        selected,
        ReadinessReason.SIGNED_OUT,
        observed_at=clock[0] - timedelta(minutes=1),
        expires_at=clock[0] + timedelta(minutes=30),
    )
    assert delayed == second
    clock[0] = second.expires_at
    assert await store.resolve(selected) is None


@pytest.mark.integration
async def test_concurrent_reports_are_serialized_without_raw_identity_fields(
    session_factory,
) -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    store = PostgresCapabilityProbeDiagnosticStore(session_factory, clock=lambda: now)
    selected = identity()
    reports = await asyncio.gather(
        *(
            store.report(
                selected,
                reason,
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            )
            for reason in (ReadinessReason.SIGNED_OUT, ReadinessReason.ISOLATION_UNPROVED)
        )
    )
    resolved = await store.resolve(selected)

    assert resolved is not None and resolved.revision == 2
    assert resolved.reason in {
        ReadinessReason.SIGNED_OUT,
        ReadinessReason.ISOLATION_UNPROVED,
    }
    assert sorted(item.revision for item in reports) == [1, 2]
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT identity_digest, reason FROM capability_probe_diagnostics")
            )
        ).one()
    assert tuple(row) == (selected.digest, resolved.reason.value)
    assert selected.account not in repr(row) and selected.client_home_digest not in repr(row)


@pytest.mark.integration
async def test_invalid_reason_and_time_windows_are_rejected(session_factory) -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    store = PostgresCapabilityProbeDiagnosticStore(session_factory, clock=lambda: now)
    selected = identity()

    for reason in (ReadinessReason.STALE_WORKER, ReadinessReason.QUOTA_EXHAUSTED):
        with pytest.raises(ValueError, match="reason"):
            await store.report(
                selected,
                reason,
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            )
    with pytest.raises(ValueError, match="time"):
        await store.report(
            selected,
            ReadinessReason.SIGNED_OUT,
            observed_at=now,
            expires_at=now,
        )
    with pytest.raises(ValueError, match="time"):
        await store.report(
            selected,
            ReadinessReason.SIGNED_OUT,
            observed_at=now + timedelta(minutes=6),
            expires_at=now + timedelta(hours=1),
        )
