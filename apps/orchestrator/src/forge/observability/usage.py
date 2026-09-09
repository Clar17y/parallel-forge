"""Immutable provider usage evidence and versioned Decimal pricing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID

_CATALOG_KEY = re.compile(r"\A[^\s:]+:[^\s]+\Z")
_PRICE_FIELDS = frozenset({"input_per_million", "output_per_million", "cached_input_per_million"})


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageRecord:
    """One model request's measured and optionally priced usage."""

    provider: str
    model: str
    prompt_version: str = "unversioned"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    duration_ms: int = 0
    tool_call_count: int = 0
    provider_request_id: str | None = None
    pricing_version: str | None = None
    estimated_cost_minor: int | None = None
    currency: str | None = None
    unknown_price_reason: str | None = None
    id: UUID | None = None
    run_id: UUID | None = None
    agent_execution_id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _bounded_nonempty(self.provider, "usage provider", 96)
        _bounded_nonempty(self.model, "usage model", 255)
        _bounded_nonempty(self.prompt_version, "usage prompt version", 96)
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "duration_ms",
            "tool_call_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.provider_request_id is not None:
            _bounded_nonempty(self.provider_request_id, "provider request id", 255)
        if self.estimated_cost_minor is not None and (
            type(self.estimated_cost_minor) is not int or self.estimated_cost_minor < 0
        ):
            raise ValueError("estimated cost must be nonnegative integer minor units")
        identity_values = (self.id, self.run_id, self.agent_execution_id)
        if any(value is not None and not isinstance(value, UUID) for value in identity_values):
            raise TypeError("usage identifiers must be UUID values")
        if self.created_at is not None and (
            self.created_at.tzinfo is None or self.created_at.utcoffset() is None
        ):
            raise ValueError("usage creation time must be timezone-aware")

        price_fields = (
            self.pricing_version,
            self.estimated_cost_minor,
            self.currency,
            self.unknown_price_reason,
        )
        if all(value is None for value in price_fields):
            return
        if self.pricing_version is None or self.currency is None:
            raise ValueError("priced usage requires pricing version and currency")
        _bounded_nonempty(self.pricing_version, "pricing version", 96)
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("usage currency must be three uppercase letters")
        if (self.estimated_cost_minor is None) == (self.unknown_price_reason is None):
            raise ValueError("usage requires exactly one known cost or unknown-price reason")
        if self.unknown_price_reason is not None:
            _bounded_nonempty(self.unknown_price_reason, "unknown-price reason", 255)


@dataclass(frozen=True, slots=True)
class _PriceEntry:
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None


class PricingCatalog:
    """A versioned immutable price table using decimal major-unit rates."""

    def __init__(
        self,
        *,
        version: str,
        entries: Mapping[tuple[str, str], _PriceEntry],
        currency_minor_exponents: Mapping[str, int],
    ) -> None:
        _bounded_nonempty(version, "pricing version", 96)
        self.version = version
        self._entries = dict(entries)
        self._currency_minor_exponents = dict(currency_minor_exponents)

    @classmethod
    def from_mapping(
        cls,
        *,
        version: str,
        entries: Mapping[str, Mapping[str, str]],
        currency_minor_exponents: Mapping[str, int] | None = None,
    ) -> PricingCatalog:
        """Validate an external catalog without accepting binary floats."""

        parsed: dict[tuple[str, str], _PriceEntry] = {}
        for key, raw_entry in entries.items():
            if not isinstance(key, str) or _CATALOG_KEY.fullmatch(key) is None:
                raise ValueError("pricing keys must use nonempty provider:model syntax")
            if not isinstance(raw_entry, Mapping):
                raise TypeError("pricing entries must be mappings")
            if set(raw_entry) - _PRICE_FIELDS or not {
                "input_per_million",
                "output_per_million",
            } <= set(raw_entry):
                raise ValueError("pricing entry fields are invalid")
            provider, model = key.split(":", 1)
            parsed[(provider, model)] = _PriceEntry(
                input_per_million=_decimal_rate(raw_entry["input_per_million"]),
                output_per_million=_decimal_rate(raw_entry["output_per_million"]),
                cached_input_per_million=(
                    _decimal_rate(raw_entry["cached_input_per_million"])
                    if "cached_input_per_million" in raw_entry
                    else None
                ),
            )
        exponents = (
            {"USD": 2} if currency_minor_exponents is None else dict(currency_minor_exponents)
        )
        if not exponents:
            raise ValueError("at least one currency exponent is required")
        for currency, exponent in exponents.items():
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("catalog currencies must be three uppercase letters")
            if type(exponent) is not int or exponent < 0 or exponent > 6:
                raise ValueError("currency minor exponent must be an integer from 0 to 6")
        return cls(version=version, entries=parsed, currency_minor_exponents=exponents)

    def unknown_reason(self, usage: UsageRecord, *, currency: str) -> str | None:
        """Return the stable reason that this request cannot be estimated."""

        if currency not in self._currency_minor_exponents:
            return "unknown_currency"
        entry = self._entries.get((usage.provider, usage.model))
        if entry is None:
            return "unknown_model_price"
        if usage.cached_input_tokens and entry.cached_input_per_million is None:
            return "unknown_cached_input_price"
        return None

    def estimate_minor_units(self, usage: UsageRecord, *, currency: str) -> int | None:
        """Estimate once at the currency minor-unit boundary using half-up rounding."""

        if self.unknown_reason(usage, currency=currency) is not None:
            return None
        entry = self._entries[(usage.provider, usage.model)]
        total_major = (
            Decimal(usage.input_tokens) * entry.input_per_million
            + Decimal(usage.output_tokens) * entry.output_per_million
            + Decimal(usage.cached_input_tokens) * (entry.cached_input_per_million or Decimal(0))
        ) / Decimal(1_000_000)
        exponent = self._currency_minor_exponents[currency]
        total_minor = total_major * (Decimal(10) ** exponent)
        return int(total_minor.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def price(self, usage: UsageRecord, *, currency: str) -> UsageRecord:
        """Attach this catalog's known estimate or explicit unknown reason."""

        normalized_currency = currency.upper()
        reason = self.unknown_reason(usage, currency=normalized_currency)
        estimate = self.estimate_minor_units(usage, currency=normalized_currency)
        return replace(
            usage,
            pricing_version=self.version,
            currency=normalized_currency,
            estimated_cost_minor=estimate,
            unknown_price_reason=reason,
        )


class _UsageWriter(Protocol):
    async def add_priced(
        self, run_id: UUID, agent_execution_id: UUID, usage: UsageRecord
    ) -> UsageRecord: ...


class UsageRecorder:
    """Apply one catalog version before crossing the usage persistence port."""

    def __init__(
        self,
        repository: _UsageWriter,
        *,
        catalog: PricingCatalog,
        currency: str = "USD",
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._currency = currency.upper()

    async def record(
        self,
        run_id: UUID,
        agent_execution_id: UUID,
        usage: UsageRecord,
    ) -> UsageRecord:
        """Price and persist one request without converting unknown cost to zero."""

        return await self._repository.add_priced(
            run_id,
            agent_execution_id,
            self._catalog.price(usage, currency=self._currency),
        )


def _decimal_rate(value: object) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("pricing rates must be decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("pricing rates must be finite decimal strings") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("pricing rates must be finite and nonnegative")
    return parsed


def _bounded_nonempty(value: object, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1-{maximum} characters")


__all__ = ["PricingCatalog", "UsageRecord", "UsageRecorder"]
