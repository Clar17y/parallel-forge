"""Closed verifier-facing observations; raw provider output is never retained."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class ProviderObservationRequired(RuntimeError):
    """Raised until an approved production probe is composed."""


class CapabilityObservationProducer(Protocol):
    """Code-owned live observation boundary; callers cannot choose a verifier."""

    async def observe(
        self, *, authorize_provider_contact: bool = False
    ) -> CapabilityObservationSet: ...


@dataclass(frozen=True, slots=True)
class ClientIdentityObservation:
    client: str
    client_version: str
    executable_digest: str
    client_home_digest: str
    executable_unchanged: Literal[True] = True
    reported_client_version: str = ""


@dataclass(frozen=True, slots=True)
class AccountAuthenticationObservation:
    account: str
    auth_mode: Literal["subscription"]
    account_kind: Literal["chatgpt", "subscription"] = "subscription"
    authenticated: Literal[True] = True


@dataclass(frozen=True, slots=True)
class RouteIdentityObservation:
    model: str
    effort: str
    catalog_supported: Literal[True] = True
    turn_completed: Literal[True] = True
    turn_observation_digest: str = "0" * 64


@dataclass(frozen=True, slots=True)
class SubscriptionRouteBindingObservation:
    auth_mode: Literal["subscription"]
    billing_mode: Literal["allowance_only"]
    paid_credential_names_scrubbed: Literal[True]
    fallback_disabled: Literal[True]
    subscription_route_observed: Literal[True] = True


@dataclass(frozen=True, slots=True)
class ToolIsolationObservation:
    tool_surface: tuple[str, ...]
    isolated: Literal[True]
    advertised_tool_surface_digest: str = ""
    forbidden_tool_calls: Literal[0] = 0
    side_effect_canaries_clear: Literal[True] = True


@dataclass(frozen=True, slots=True)
class CapabilityObservationSet:
    client_identity: ClientIdentityObservation
    account_authentication: AccountAuthenticationObservation
    route_identity: RouteIdentityObservation
    subscription_route_binding: SubscriptionRouteBindingObservation
    tool_isolation: ToolIsolationObservation
    verifier_id: str = "forge-codex-official"
    verifier_version: str = "1"


class UnsupportedCapabilityProbe:
    """Production composition deliberately has no live provider probe yet."""

    async def observe(self, *_: object, **__: object) -> CapabilityObservationSet:
        raise ProviderObservationRequired("provider_observation_required")


__all__ = [
    "AccountAuthenticationObservation",
    "CapabilityObservationProducer",
    "CapabilityObservationSet",
    "ClientIdentityObservation",
    "ProviderObservationRequired",
    "RouteIdentityObservation",
    "SubscriptionRouteBindingObservation",
    "ToolIsolationObservation",
    "UnsupportedCapabilityProbe",
]
