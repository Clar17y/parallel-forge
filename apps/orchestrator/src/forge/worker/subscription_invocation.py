"""Own one queued subscription invocation through durable decision application."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_execution import (
    SubscriptionAdmission,
    SubscriptionSettlement,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionGateway,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.subscription_acceptance_dispatch import (
    SubscriptionAcceptanceDispatch,
)
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_plan_gate import (
    SubscriptionPlanGateOutcome,
    SubscriptionPlanGateService,
)
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.plan import PlanOutput
from forge.domain.subscription import (
    AcceptDecision,
    AttemptTelemetry,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ReviewSelection,
    RouteSpec,
    ScopeRequestDecision,
    TaskBudget,
    WaitDecision,
)
from forge.worker.subscription_runtime import SubscriptionAttemptOutcome, SubscriptionAttemptRunner


@dataclass(frozen=True, slots=True)
class SubscriptionInvocationSession:
    """Trusted, lazy gateway and its bound broker revocation callback.

    Session construction must not launch a provider. All provider work belongs
    in gateway.execute, under the attempt runner's lifetime and lease.
    """

    gateway: SubscriptionGateway
    revoke: Callable[[], Awaitable[None]]

    def __post_init__(self) -> None:
        if not isinstance(self.gateway, SubscriptionGateway) or not callable(self.revoke):
            raise TypeError("invocation session requires a gateway and revocation callback")


@dataclass(frozen=True, slots=True)
class SubscriptionWorkOutcome:
    admission: SubscriptionAdmission
    attempt: SubscriptionAttemptOutcome
    application: SubscriptionSettlement | SubscriptionPlanGateOutcome | None = None


class SubscriptionInvocationWorker:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        session_factory: Callable[
            [SubscriptionAdmission, SubscriptionInvocationRequest], SubscriptionInvocationSession
        ],
        *,
        artifacts: ArtifactStore,
        owner: str,
        reservation: TaskBudget,
        candidates: SubscriptionCandidateApplication | None = None,
        acceptance: SubscriptionAcceptanceDispatch | None = None,
        eligible_routes: frozenset[RouteSpec] | None = None,
    ) -> None:
        if not owner.strip() or not callable(session_factory):
            raise ValueError("worker owner and session factory are required")
        if (
            not isinstance(reservation, TaskBudget)
            or reservation.max_provider_attempts != 1
            or reservation.max_repairs != 0
        ):
            raise ValueError("reserve exactly one provider attempt and no repair unit")
        self._owner, self._reservation = owner, reservation
        self._eligible_routes = eligible_routes
        self._sessions = session_factory
        self._executor = SubscriptionDecisionExecutor(work_factory)
        self._requests = SubscriptionRequestBuilder(work_factory)
        self._runner = SubscriptionAttemptRunner(work_factory)
        self._decisions = SubscriptionDecisionApplication(work_factory)
        self._plans = SubscriptionPlanGateService(artifacts, work_factory)
        self._candidates = candidates
        self._acceptance = acceptance

    async def run_once(
        self, *, stop_event: asyncio.Event | None = None
    ) -> SubscriptionWorkOutcome | None:
        stop = stop_event if stop_event is not None else asyncio.Event()
        operation = asyncio.create_task(self._run_once(stop))
        cancelled = False
        while True:
            try:
                outcome = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                cancelled = True
                stop.set()
        if cancelled:
            raise asyncio.CancelledError
        return outcome

    async def _run_once(self, stop: asyncio.Event) -> SubscriptionWorkOutcome | None:
        if stop.is_set():
            return None
        admission = await self._executor.admit_next(
            self._owner, self._reservation, eligible_routes=self._eligible_routes
        )
        if admission is None:
            return None
        # Do not cancel an admission transaction with an unknown commit outcome.
        # A shutdown received during admission is accounted before returning.
        if stop.is_set():
            return await self._not_invoked(admission, SubscriptionFailure.INTERRUPTED)
        try:
            request = await self._requests.build(admission)
            if stop.is_set():
                raise asyncio.CancelledError
            session = self._sessions(admission, request)
            if not isinstance(session, SubscriptionInvocationSession):
                raise TypeError("invalid invocation session")
        except asyncio.CancelledError:
            return await self._not_invoked(admission, SubscriptionFailure.INTERRUPTED)
        except Exception as error:  # noqa: BLE001 - setup failures retain a closed classification
            failure = (
                SubscriptionFailure.POLICY_DENIED
                if isinstance(error, ValueError)
                else SubscriptionFailure.UNAVAILABLE
            )
            return await self._not_invoked(admission, failure)
        attempt = await self._runner.execute(
            admission, request, session.gateway, session.revoke, stop_event=stop
        )
        application: SubscriptionSettlement | SubscriptionPlanGateOutcome | None = None
        if not stop.is_set() and attempt.settlement.disposition == "decision_pending":
            decision = attempt.result.decision
            if isinstance(decision, PlanOutput):
                application = await self._plans.request_settled(admission.attempt.attempt_id)
            elif isinstance(decision, DelegateDecision):
                application = await self._decisions.apply_delegation(admission.attempt.attempt_id)
            elif isinstance(decision, WaitDecision):
                application = await self._decisions.apply_wait(admission.attempt.attempt_id)
            elif isinstance(decision, BoundReassignDecision):
                application = await self._decisions.apply_reassignment(admission.attempt.attempt_id)
            elif isinstance(decision, ScopeRequestDecision):
                application = await self._decisions.apply_scope_request(
                    admission.attempt.attempt_id
                )
            elif isinstance(decision, BoundScopeResponseDecision):
                application = await self._decisions.apply_scope_response(
                    admission.attempt.attempt_id
                )
            elif (
                isinstance(decision, AcceptDecision) and decision.task_id != admission.task.task_id
            ):
                application = await self._decisions.prepare_acceptance(admission.attempt.attempt_id)
            elif isinstance(decision, AcceptDecision) and self._acceptance is not None:
                application = await self._acceptance.apply(admission.attempt.attempt_id)
            elif isinstance(decision, ReviewSelection) and self._candidates is not None:
                application = await self._candidates.apply(admission.attempt.attempt_id)
            # Other typed decisions stay durably pending until their application
            # services can prove the required authority and evidence.
        return SubscriptionWorkOutcome(admission, attempt, application)

    async def _not_invoked(
        self, admission: SubscriptionAdmission, failure: SubscriptionFailure
    ) -> SubscriptionWorkOutcome:
        result = SubscriptionInvocationResult(
            attempt=admission.attempt,
            failure=failure,
            failure_detail="subscription invocation stopped before provider execution",
            telemetry=AttemptTelemetry(
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                duration_ms=0,
                tool_call_count=0,
                named_check_count=0,
                estimated_api_cost_minor=0,
            ),
        )
        settlement = await self._executor.settle(admission, result)
        return SubscriptionWorkOutcome(admission, SubscriptionAttemptOutcome(result, settlement))
