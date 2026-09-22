"""Exact Claude registration for the shared subscription runtime factory."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from forge.agents.claude_gateway import ClaudeCapabilityVerifier, ClaudeGateway, ClaudeInstallation
from forge.agents.runtime_factory import RouteUnavailable
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.local_cli import LocalCliTrust
from forge.domain.provider_quota import utc_now
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec

if TYPE_CHECKING:
    from forge.agents.client_process import ClientProcessLifecycle
    from forge.application.services.subscription_broker import SubscriptionToolBroker


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeAdapter:
    """Bind a fresh attempt gateway using the selected local-client trust policy."""

    installation: ClaudeInstallation = field(repr=False)
    verifier: ClaudeCapabilityVerifier | None = field(default=None, repr=False)
    trust: LocalCliTrust = field(default=LocalCliTrust.VERIFIED, kw_only=True)
    now: Callable[[], datetime] = field(default=utc_now, repr=False, kw_only=True)
    route: RouteSpec = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation, ClaudeInstallation)
            or not isinstance(self.trust, LocalCliTrust)
            or self.trust is LocalCliTrust.VERIFIED
            and not callable(getattr(self.verifier, "verify", None))
            or not callable(self.now)
        ):
            raise TypeError("Claude runtime requires trusted installation and verification")
        try:
            effort = ReasoningEffort(self.installation.effort)
        except ValueError:
            raise ValueError("Claude runtime requires a supported reasoning effort") from None
        object.__setattr__(
            self,
            "route",
            RouteSpec(
                provider="anthropic",
                client="claude_code",
                model=self.installation.model,
                effort=effort,
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
    ) -> ClaudeGateway:
        if (
            type(request) is not SubscriptionInvocationRequest
            or request.task.route.effective != self.route
        ):
            raise RouteUnavailable()
        return ClaudeGateway(
            self.installation,
            self.verifier,
            broker=broker,
            lifecycle=lifecycle,
            now=self.now,
            trust=self.trust,
        )
