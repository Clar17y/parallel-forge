"""Explicit Codex registration for the shared production/evaluation factory."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from forge.agents.codex_gateway import CodexCapabilityVerifier, CodexGateway, CodexInstallation
from forge.agents.runtime_factory import RouteUnavailable
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.local_cli import LocalCliTrust
from forge.domain.provider_quota import utc_now
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec

if TYPE_CHECKING:
    from forge.agents.client_process import ClientProcessLifecycle
    from forge.application.services.subscription_broker import SubscriptionToolBroker


@dataclass(frozen=True, slots=True)
class CodexRuntimeAdapter:
    """Construct one gateway per attempt from trusted installation dependencies.

    Registration describes a locally constructible route, not quota or account
    availability. The gateway applies the selected local-client trust policy.
    Neither construction nor gateway binding performs client or account IO.
    """

    installation: CodexInstallation = field(repr=False)
    verifier: CodexCapabilityVerifier | None = field(default=None, repr=False)
    trust: LocalCliTrust = field(default=LocalCliTrust.VERIFIED, kw_only=True)
    now: Callable[[], datetime] = field(default=utc_now, repr=False, kw_only=True)
    route: RouteSpec = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation, CodexInstallation)
            or not isinstance(self.trust, LocalCliTrust)
            or self.trust is LocalCliTrust.VERIFIED
            and not callable(getattr(self.verifier, "verify", None))
            or not callable(self.now)
        ):
            raise TypeError("Codex runtime requires trusted installation and verification")
        try:
            effort = ReasoningEffort(self.installation.effort)
        except ValueError:
            raise ValueError("Codex runtime requires a supported reasoning effort") from None
        object.__setattr__(
            self,
            "route",
            RouteSpec(
                provider="openai",
                client="codex_app_server",
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
    ) -> CodexGateway:
        if (
            type(request) is not SubscriptionInvocationRequest
            or request.task.route.effective != self.route
        ):
            raise RouteUnavailable()
        return CodexGateway(
            self.installation,
            self.verifier,
            broker=broker,
            lifecycle=lifecycle,
            now=self.now,
            trust=self.trust,
        )
