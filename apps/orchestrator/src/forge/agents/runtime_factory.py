"""Request-time construction of gateways pinned to a frozen runtime route."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from forge.agents.adk_gateway import AdkToolProvider, GoogleAdkGateway
from forge.agents.adk_runtime import AdkRuntimeProtocol
from forge.agents.errors import AgentGatewayError
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.agents import AgentGateway
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionGateway,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.agent import AgentRequest, AgentResult
from forge.domain.subscription import AuthMode, BillingMode, RouteBinding, RouteSpec
from forge.observability.usage import PricingCatalog

if TYPE_CHECKING:
    from forge.agents.client_process import ClientProcessLifecycle
    from forge.application.services.subscription_broker import SubscriptionToolBroker


class RouteUnavailable(AgentGatewayError):
    """The approved route has no safe local implementation at request time."""

    _MESSAGE = "agent route is unavailable"


@runtime_checkable
class RuntimeAdapter(Protocol):
    """An adapter registered for precisely one effective route pin."""

    @property
    def route(self) -> RouteSpec: ...

    def gateway_for(
        self, binding: RouteBinding, tool_provider: AdkToolProvider
    ) -> AgentGateway: ...


@runtime_checkable
class SubscriptionRuntimeAdapter(Protocol):
    """Construct an attempt-bound gateway using trusted broker composition."""

    @property
    def route(self) -> RouteSpec: ...

    def gateway_for(
        self,
        request: SubscriptionInvocationRequest,
        *,
        broker: SubscriptionToolBroker,
        lifecycle: ClientProcessLifecycle,
    ) -> SubscriptionGateway: ...


class AgentRuntimeFactory:
    """Resolve only an explicitly frozen effective route; never select a fallback."""

    def __init__(
        self,
        adapters: Iterable[RuntimeAdapter] = (),
        *,
        subscription_adapters: Iterable[SubscriptionRuntimeAdapter] = (),
    ) -> None:
        registered: dict[RouteSpec, RuntimeAdapter] = {}
        for adapter in tuple(adapters):
            if not isinstance(adapter, RuntimeAdapter):
                raise TypeError("runtime adapter must implement RuntimeAdapter")
            route = adapter.route
            if not isinstance(route, RouteSpec) or route in registered:
                raise ValueError("runtime adapter route is invalid or duplicated")
            registered[route] = adapter
        self._adapters = registered
        self._adapter_resolver: Callable[[RouteSpec], RuntimeAdapter | None] | None = None
        self._subscription_adapters: dict[RouteSpec, SubscriptionRuntimeAdapter] = {}
        for subscription_adapter in tuple(subscription_adapters):
            if not isinstance(subscription_adapter, SubscriptionRuntimeAdapter):
                raise TypeError("subscription adapter must implement SubscriptionRuntimeAdapter")
            route = subscription_adapter.route
            if (
                not isinstance(route, RouteSpec)
                or route.auth_mode is not AuthMode.SUBSCRIPTION
                or route in self._subscription_adapters
            ):
                raise ValueError("subscription adapter route is invalid or duplicated")
            self._subscription_adapters[route] = subscription_adapter

    @property
    def subscription_routes(self) -> frozenset[RouteSpec]:
        """Locally constructible pins, without implying provider quota is available."""
        return frozenset(self._subscription_adapters)

    def subscription_gateway_for(
        self,
        request: SubscriptionInvocationRequest,
        *,
        broker: SubscriptionToolBroker,
        lifecycle: ClientProcessLifecycle,
    ) -> SubscriptionGateway:
        if type(request) is not SubscriptionInvocationRequest:
            raise RouteUnavailable()
        route = request.task.route.effective
        adapter = self._subscription_adapters.get(route)
        if adapter is None or adapter.route != route:
            raise RouteUnavailable()
        try:
            gateway = adapter.gateway_for(request, broker=broker, lifecycle=lifecycle)
        except Exception:  # noqa: BLE001 - adapter construction is a trust boundary
            raise RouteUnavailable() from None
        if not isinstance(gateway, SubscriptionGateway):
            raise RouteUnavailable()
        return _PinnedSubscriptionGateway(request, gateway)

    @classmethod
    def with_resolver(
        cls,
        resolver: Callable[[RouteSpec], RuntimeAdapter | None],
        *,
        subscription_adapters: Iterable[SubscriptionRuntimeAdapter] = (),
    ) -> AgentRuntimeFactory:
        """Create a factory whose resolver still receives the exact route pin."""
        if not callable(resolver):
            raise TypeError("runtime adapter resolver must be callable")
        instance = cls(subscription_adapters=subscription_adapters)
        instance._adapter_resolver = resolver
        return instance

    def gateway_for(self, binding: RouteBinding, tool_provider: AdkToolProvider) -> AgentGateway:
        if not isinstance(binding, RouteBinding) or not isinstance(tool_provider, AdkToolProvider):
            raise RouteUnavailable()
        adapter = self._adapters.get(binding.effective)
        if adapter is None:
            resolver = self._adapter_resolver
            if resolver is not None:
                try:
                    adapter = resolver(binding.effective)
                except Exception:  # noqa: BLE001 - adapter lookup is a trust boundary
                    raise RouteUnavailable() from None
        if adapter is None or adapter.route != binding.effective:
            raise RouteUnavailable()
        try:
            gateway = adapter.gateway_for(binding, tool_provider)
        except RouteUnavailable:
            raise
        except Exception:  # noqa: BLE001 - adapter configuration is a trust boundary
            raise RouteUnavailable() from None
        if not isinstance(gateway, AgentGateway):
            raise RouteUnavailable()
        return _PinnedGateway(binding, gateway)


@dataclass(frozen=True, slots=True)
class _PinnedSubscriptionGateway:
    request: SubscriptionInvocationRequest
    gateway: SubscriptionGateway

    async def execute(self, request: SubscriptionInvocationRequest) -> SubscriptionInvocationResult:
        if type(request) is not SubscriptionInvocationRequest or request != self.request:
            raise RouteUnavailable()
        try:
            result = await self.gateway.execute(request)
        except SubscriptionInterrupted as interruption:
            if (
                type(interruption.result) is not SubscriptionInvocationResult
                or interruption.result.attempt != request.attempt
            ):
                raise SubscriptionInterrupted(
                    SubscriptionInvocationResult(
                        attempt=request.attempt, failure=SubscriptionFailure.PROTOCOL
                    )
                ) from None
            raise
        if type(result) is not SubscriptionInvocationResult or result.attempt != request.attempt:
            raise RouteUnavailable()
        return result


@dataclass(frozen=True, slots=True)
class _PinnedGateway:
    binding: RouteBinding
    gateway: AgentGateway

    async def execute(self, request: AgentRequest) -> AgentResult:
        if (
            type(request) is not AgentRequest
            or request.provider != self.binding.effective.provider
            or request.model != self.binding.effective.model
        ):
            raise RouteUnavailable()
        return await self.gateway.execute(request)


@dataclass(frozen=True, slots=True)
class GoogleAdkRuntimeAdapter:
    """Retained v0.1 Google ADK adapter for one explicit API-key route only."""

    route: RouteSpec
    prompt_loader: PromptLoader
    runtime_supplier: Callable[[], AdkRuntimeProtocol]
    pricing_catalog_supplier: Callable[[], PricingCatalog]
    currency: str = "USD"
    gateway_builder: Callable[..., AgentGateway] = GoogleAdkGateway

    def __post_init__(self) -> None:
        if (
            not isinstance(self.route, RouteSpec)
            or self.route.provider != "google"
            or self.route.client != "google_adk"
            or self.route.auth_mode is not AuthMode.API_KEY
            or self.route.billing_mode is not BillingMode.PAID_OPT_IN
            or not isinstance(self.prompt_loader, PromptLoader)
            or not callable(self.runtime_supplier)
            or not callable(self.pricing_catalog_supplier)
            or not callable(self.gateway_builder)
            or type(self.currency) is not str
            or not self.currency
        ):
            raise ValueError("Google ADK adapter route is invalid")

    def gateway_for(self, binding: RouteBinding, tool_provider: AdkToolProvider) -> AgentGateway:
        if binding.effective != self.route:
            raise RouteUnavailable()
        runtime = self.runtime_supplier()
        pricing = self.pricing_catalog_supplier()
        if not isinstance(runtime, AdkRuntimeProtocol) or not isinstance(pricing, PricingCatalog):
            raise RouteUnavailable()
        return self.gateway_builder(
            runtime=runtime,
            prompt_loader=self.prompt_loader,
            tool_provider=tool_provider,
            pricing_catalog=pricing,
            supported_provider=self.route.provider,
            currency=self.currency,
        )


def legacy_google_api_binding(*, provider: str, model: str) -> RouteBinding:
    """Build the sole retained v0.1 route explicitly from a legacy request pin."""
    if provider != "google" or type(model) is not str or not model:
        raise RouteUnavailable()
    route = RouteSpec(
        provider="google",
        client="google_adk",
        model=model,
        auth_mode=AuthMode.API_KEY,
        billing_mode=BillingMode.PAID_OPT_IN,
    )
    return RouteBinding(requested=route, effective=route)


@dataclass(frozen=True, slots=True)
class LegacyGoogleRequestGateway:
    """Compatibility gateway that selects the retained legacy route explicitly."""

    factory: AgentRuntimeFactory
    tool_provider: AdkToolProvider

    async def execute(self, request: AgentRequest) -> AgentResult:
        if type(request) is not AgentRequest:
            raise RouteUnavailable()
        binding = legacy_google_api_binding(provider=request.provider, model=request.model)
        gateway = self.factory.gateway_for(binding, self.tool_provider)
        return await gateway.execute(request)


__all__ = [
    "AgentRuntimeFactory",
    "GoogleAdkRuntimeAdapter",
    "LegacyGoogleRequestGateway",
    "RouteUnavailable",
    "RuntimeAdapter",
    "SubscriptionRuntimeAdapter",
    "legacy_google_api_binding",
]
