"""Tests for immutable usage records and Decimal pricing."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest
from forge.observability.usage import PricingCatalog, UsageRecord, UsageRecorder


def test_cost_uses_versioned_integer_minor_units() -> None:
    catalog = PricingCatalog.from_mapping(
        version="2026-08-21",
        entries={"gemini:test": {"input_per_million": "1.25", "output_per_million": "5.00"}},
    )
    usage = UsageRecord(
        provider="gemini", model="test", input_tokens=2_000_000, output_tokens=500_000
    )

    assert catalog.estimate_minor_units(usage, currency="USD") == 500


def test_cached_input_has_its_own_price_and_rounds_once_at_minor_units() -> None:
    catalog = PricingCatalog.from_mapping(
        version="v1",
        entries={
            "provider:model": {
                "input_per_million": "1.00",
                "output_per_million": "2.00",
                "cached_input_per_million": "0.10",
            }
        },
    )
    usage = UsageRecord(
        provider="provider",
        model="model",
        input_tokens=1,
        output_tokens=1,
        cached_input_tokens=1_000_001,
    )

    assert catalog.estimate_minor_units(usage, currency="USD") == 10


@pytest.mark.parametrize(
    "entries",
    [
        {"provider:model": {"input_per_million": 1.0, "output_per_million": "1"}},
        {"provider:model": {"input_per_million": "-1", "output_per_million": "1"}},
        {"provider:model": {"input_per_million": "NaN", "output_per_million": "1"}},
        {"bad key": {"input_per_million": "1", "output_per_million": "1"}},
    ],
)
def test_catalog_rejects_unsafe_price_entries(entries: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PricingCatalog.from_mapping(version="v1", entries=entries)  # type: ignore[arg-type]


def test_unknown_prices_are_explicit_and_usage_is_immutable() -> None:
    catalog = PricingCatalog.from_mapping(
        version="v1",
        entries={"provider:model": {"input_per_million": "1", "output_per_million": "1"}},
    )
    usage = UsageRecord(provider="missing", model="model", input_tokens=1)

    assert catalog.estimate_minor_units(usage, currency="USD") is None
    assert catalog.unknown_reason(usage, currency="USD") == "unknown_model_price"
    with pytest.raises(FrozenInstanceError):
        usage.provider = "other"  # type: ignore[misc]


def test_missing_cached_price_and_currency_are_distinguished() -> None:
    catalog = PricingCatalog.from_mapping(
        version="v1",
        entries={"provider:model": {"input_per_million": "1", "output_per_million": "1"}},
    )
    cached = UsageRecord(provider="provider", model="model", cached_input_tokens=1)

    assert catalog.unknown_reason(cached, currency="USD") == "unknown_cached_input_price"
    assert catalog.unknown_reason(cached, currency="EUR") == "unknown_currency"


class MemoryUsageWriter:
    def __init__(self) -> None:
        self.received: tuple[UUID, UUID, UsageRecord] | None = None

    async def add_priced(
        self, run_id: UUID, agent_execution_id: UUID, usage: UsageRecord
    ) -> UsageRecord:
        self.received = (run_id, agent_execution_id, usage)
        return usage


@pytest.mark.asyncio
async def test_usage_recorder_persists_catalog_version_and_explicit_unknown_reason() -> None:
    writer = MemoryUsageWriter()
    recorder = UsageRecorder(
        writer,
        catalog=PricingCatalog.from_mapping(version="catalog-v1", entries={}),
    )
    run_id = uuid4()
    execution_id = uuid4()

    stored = await recorder.record(
        run_id,
        execution_id,
        UsageRecord(provider="provider", model="missing", input_tokens=10),
    )

    assert writer.received == (run_id, execution_id, stored)
    assert stored.pricing_version == "catalog-v1"
    assert stored.currency == "USD"
    assert stored.estimated_cost_minor is None
    assert stored.unknown_price_reason == "unknown_model_price"


def test_usage_rejects_boolean_counts() -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        UsageRecord(provider="provider", model="model", input_tokens=True)  # type: ignore[arg-type]
