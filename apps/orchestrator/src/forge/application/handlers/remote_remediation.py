"""Route a remote repair through the run's frozen execution mode."""

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.executions import ExecutionOutcome
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.development import DevelopmentService
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
    SubscriptionRemoteRemediationDecision,
)
from forge.domain.command import CommandEnvelope


class RemoteRemediationHandler:
    def __init__(
        self,
        legacy: DevelopmentService,
        subscription: SubscriptionRemoteRemediationController | None = None,
    ) -> None:
        self._legacy, self._subscription = legacy, subscription

    async def __call__(
        self, command: CommandEnvelope, work: UnitOfWork
    ) -> ExecutionOutcome | SubscriptionRemoteRemediationDecision:
        if await work.subscription.envelope_for_run(command.run_id) is not None:
            if self._subscription is None:
                raise CommandRecoveryRequired("subscription remote repair is not configured")
            return await self._subscription.execute(command, work)
        return await self._legacy.execute(command, work)
