"""Quota pool identity and policy, independent of credentials and transport."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from forge.domain.subscription import AuthMode, BillingMode, RouteSpec

_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class QuotaPoolKey:
    provider: str
    account: str
    pool: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.account, self.pool):
            if type(value) is not str or _LABEL.fullmatch(value) is None:
                raise ValueError("quota pool identity requires opaque lowercase labels")


@dataclass(frozen=True, slots=True)
class QuotaRoutePool:
    provider: str
    client: str
    account: str
    pool: str
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY
    model: str | None = None

    def __post_init__(self) -> None:
        QuotaPoolKey(self.provider, self.account, self.pool)
        if not self.client or len(self.client) > 96:
            raise ValueError("quota route client is invalid")
        if not isinstance(self.auth_mode, AuthMode) or not isinstance(
            self.billing_mode, BillingMode
        ):
            raise TypeError("quota route modes are invalid")
        if self.model is not None and (not self.model or len(self.model) > 255):
            raise ValueError("quota route model is invalid")

    def matches(self, route: RouteSpec) -> bool:
        return (self.provider, self.client, self.auth_mode, self.billing_mode) == (
            route.provider,
            route.client,
            route.auth_mode,
            route.billing_mode,
        ) and (self.model is None or self.model == route.model)


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    route_pools: tuple[QuotaRoutePool, ...] = ()
    unknown_reset_cooldown_seconds: int = 3600

    def __post_init__(self) -> None:
        if type(self.unknown_reset_cooldown_seconds) is not int or not (
            60 <= self.unknown_reset_cooldown_seconds <= 86400
        ):
            raise ValueError("quota probe cooldown must be 60..86400 seconds")
        routes = tuple(self.route_pools)
        if len(routes) > 256 or any(not isinstance(route, QuotaRoutePool) for route in routes):
            raise ValueError("invalid quota route pool policy")
        selectors = [(r.provider, r.client, r.auth_mode, r.billing_mode, r.model) for r in routes]
        if len(selectors) != len(set(selectors)):
            raise ValueError("duplicate quota route pool selector")
        object.__setattr__(self, "route_pools", routes)

    def key_for(self, route: RouteSpec) -> QuotaPoolKey:
        matching = [item for item in self.route_pools if item.matches(route)]
        if matching:
            selected = next((item for item in matching if item.model is not None), matching[0])
            return QuotaPoolKey(selected.provider, selected.account, selected.pool)
        # Default: one local account per provider/mode, shared across models.
        # Separately authenticated clients require explicit operator mappings.
        return QuotaPoolKey(
            route.provider, "local", f"{route.auth_mode.value}-{route.billing_mode.value}"
        )


@dataclass(frozen=True, slots=True)
class PoolQuotaStatus:
    key: QuotaPoolKey
    revision: int
    status: Literal["blocked", "unknown", "eligible"]
    observed_at: datetime | None
    reason: str | None
    reset_at: datetime | None
    next_eligible_at: datetime | None
    retry_basis: Literal["known_reset", "probe_cooldown"] | None
    probe_attempt_id: UUID | None = None
    recovered_at: datetime | None = None

    @property
    def allows_attempt(self) -> bool:
        return self.status != "blocked"
