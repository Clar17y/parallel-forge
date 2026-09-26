"""Exact official Gemini registration for the subscription runtime factory."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.agents.gemini_gateway import GeminiCapabilityVerifier, GeminiGateway, GeminiInstallation
from forge.agents.runtime_factory import RouteUnavailable
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec

if TYPE_CHECKING:
    from forge.agents.client_process import ClientProcessLifecycle
    from forge.application.services.subscription_broker import SubscriptionToolBroker


@dataclass(frozen=True, slots=True)
class GeminiRuntimeAdapter:
    """Construct the legacy ACP gateway with an explicit local-client trust policy."""

    installation: GeminiInstallation = field(repr=False)
    verifier: GeminiCapabilityVerifier | None = field(default=None, repr=False)
    trust: LocalCliTrust = field(default=LocalCliTrust.VERIFIED, kw_only=True)
    route: RouteSpec = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation, GeminiInstallation)
            or not isinstance(self.trust, LocalCliTrust)
            or self.trust is LocalCliTrust.VERIFIED
            and not callable(getattr(self.verifier, "verify", None))
        ):
            raise TypeError("Gemini runtime requires trusted installation and verification")
        if self.installation.effort is None:
            raise ValueError("Gemini runtime requires an explicit reasoning effort")
        object.__setattr__(
            self,
            "route",
            RouteSpec(
                provider="google",
                client="gemini_cli",
                model=self.installation.model,
                effort=ReasoningEffort(self.installation.effort),
                auth_mode=AuthMode.SUBSCRIPTION,
                billing_mode=BillingMode.ALLOWANCE_ONLY,
            ),
        )

    def gateway_for(
        self,
        request: SubscriptionInvocationRequest,
        *,
        broker: SubscriptionToolBroker,
        lifecycle: ClientProcessLifecycle,
    ) -> GeminiGateway:
        if (
            type(request) is not SubscriptionInvocationRequest
            or request.task.route.effective != self.route
        ):
            raise RouteUnavailable()
        return GeminiGateway(
            self.installation,
            self.verifier,
            broker=broker,
            lifecycle=lifecycle,
            trust=self.trust,
        )
