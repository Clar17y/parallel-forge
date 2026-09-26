"""Bind lazy controlled tools and durable client lifecycle to one invocation."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from forge.application.ports.subscription_execution import SubscriptionAdmission
from forge.application.ports.subscription_gateway import (
    SubscriptionGateway,
    SubscriptionInvocationRequest,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.tools import ControlledToolService
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName, ToolResult
from forge.worker.subscription_broker import DurableClientProcessLifecycle
from forge.worker.subscription_invocation import SubscriptionInvocationSession

type ToolServiceFactory = Callable[
    [SubscriptionAdmission, SubscriptionInvocationRequest], Awaitable[ControlledToolService]
]
type GatewayFactory = Callable[
    [
        SubscriptionAdmission,
        SubscriptionInvocationRequest,
        SubscriptionToolBroker,
        DurableClientProcessLifecycle,
    ],
    SubscriptionGateway,
]


class ControlledSubscriptionSessionFactory:
    """Construct only authority closures; provider and tool IO remain lazy.

    The gateway factory is trusted composition, responsible for resolving the
    exact frozen route and verifying official-client capabilities. It must not
    launch the client until execute is called by the lease runner.
    """

    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        *,
        tools: ToolServiceFactory,
        gateway: GatewayFactory,
    ) -> None:
        self._work_factory, self._tools, self._gateway = work_factory, tools, gateway

    def __call__(
        self, admission: SubscriptionAdmission, request: SubscriptionInvocationRequest
    ) -> SubscriptionInvocationSession:
        if (
            request.attempt != admission.attempt
            or request.task != admission.task
            or request.envelope != admission.envelope
            or request.run_state is None
            or request.attempt_budget is None
        ):
            raise ValueError("subscription session differs from durable admission")
        authority = request.authorization
        context = SubscriptionToolAuthorizationContext(
            run_id=authority.run_id,
            task_id=authority.task_id,
            attempt_id=authority.attempt_id,
            worktree_id=authority.worktree_id,
            purpose=authority.role,
            policy_version=authority.policy_version,
            permitted_tools=authority.permitted_tools,
        )
        initialized = asyncio.Lock()
        controlled: ControlledSubscriptionEffect | None = None

        async def effect(
            operation_id: UUID, name: ToolName, arguments: Mapping[str, object]
        ) -> ToolResult:
            nonlocal controlled
            async with initialized:
                if controlled is None:
                    service = await self._tools(admission, request)
                    if not isinstance(service, ControlledToolService):
                        raise TypeError("subscription tools require the controlled service")
                    controlled = ControlledSubscriptionEffect(service, context)
            return await controlled(operation_id, name, arguments)

        broker = SubscriptionToolBroker(
            self._work_factory,
            lease=admission.lease,
            authority=authority,
            effect=effect,
            owned_paths=admission.task.owned_paths,
            expected_candidate_epoch=admission.candidate_epoch,
        )
        lifecycle = DurableClientProcessLifecycle(
            self._work_factory,
            attempt_id=admission.attempt.attempt_id,
            worker_identity=admission.lease.owner,
        )
        gateway = self._gateway(admission, request, broker, lifecycle)
        return SubscriptionInvocationSession(gateway, broker.revoke)
