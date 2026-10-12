"""Own one queued subscription invocation through durable decision application."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.repository import RepositoryReader
from forge.application.ports.scheduling import SchedulingLeaseRevoked
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
from forge.application.ports.task_usage import TaskUsageObservationStore, TaskUsageObserver
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitWorkingTreeSnapshot, ManagedWorktree
from forge.application.services.jev import JevService
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
from forge.application.services.task_usage_composition import configured_run_cohort
from forge.application.services.task_usage_monitor import RunTaskUsageMonitor
from forge.domain.plan import PlanOutput
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription import (
    AcceptDecision,
    AttemptTelemetry,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ForwardFeedbackDecision,
    ReviewSelection,
    RouteSpec,
    ScopeRequestDecision,
    TaskBudget,
    WaitDecision,
)
from forge.domain.task_usage_contract import (
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
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
        jev_service: JevService | None = None,
        review_snapshot: Callable[[ProjectPolicy, ManagedWorktree], GitWorkingTreeSnapshot]
        | None = None,
        review_reader: Callable[[ProjectPolicy, ManagedWorktree], RepositoryReader] | None = None,
        usage_sink: TaskUsageObserver | None = None,
        usage_bindings: TaskUsageObservationStore | None = None,
        usage_monitor: RunTaskUsageMonitor | None = None,
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
        self._work_factory = work_factory
        self._eligible_routes = eligible_routes
        self._sessions = session_factory
        self._executor = SubscriptionDecisionExecutor(work_factory)
        self._requests = SubscriptionRequestBuilder(
            work_factory, jev_service=jev_service, snapshot=review_snapshot, reader=review_reader
        )
        self._runner = SubscriptionAttemptRunner(work_factory)
        self._decisions = SubscriptionDecisionApplication(work_factory)
        self._plans = SubscriptionPlanGateService(artifacts, work_factory)
        self._candidates = candidates
        self._acceptance = acceptance
        if (usage_sink is None) != (usage_bindings is None):
            raise ValueError("usage sink and binding store must be configured together")
        self._usage_sink, self._usage_bindings = usage_sink, usage_bindings
        self._usage_monitor = usage_monitor

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
            request = await self._build_with_renewal(admission, stop)
            if stop.is_set():
                raise asyncio.CancelledError
            usage_binding = await self._bind_usage(admission)
            session = self._sessions(admission, request)
            if not isinstance(session, SubscriptionInvocationSession):
                raise TypeError("invalid invocation session")
        except asyncio.CancelledError:
            return await self._not_invoked(admission, SubscriptionFailure.INTERRUPTED)
        except SchedulingLeaseRevoked:
            return await self._not_invoked(admission, SubscriptionFailure.INTERRUPTED)
        except Exception as error:  # noqa: BLE001 - setup failures retain a closed classification
            failure = (
                SubscriptionFailure.POLICY_DENIED
                if isinstance(error, ValueError)
                else SubscriptionFailure.UNAVAILABLE
            )
            return await self._not_invoked(admission, failure)
        attempt = await self._runner.execute(
            admission,
            request,
            session.gateway,
            session.revoke,
            stop_event=stop,
            usage_binding=usage_binding,
            usage_sink=self._usage_sink,
        )
        application: SubscriptionSettlement | SubscriptionPlanGateOutcome | None = None
        checkpoint_active = (
            self._usage_monitor is not None
            and usage_binding is not None
            and await self._usage_monitor.is_active(usage_binding.subject)
        )
        if (
            not stop.is_set()
            and not checkpoint_active
            and attempt.settlement.disposition == "decision_pending"
        ):
            decision = attempt.result.decision
            if isinstance(decision, PlanOutput):
                application = await self._plans.request_settled(admission.attempt.attempt_id)
            elif isinstance(decision, DelegateDecision):
                application = await self._decisions.apply_delegation(admission.attempt.attempt_id)
            elif isinstance(decision, WaitDecision):
                application = await self._decisions.apply_wait(admission.attempt.attempt_id)
            elif isinstance(decision, BoundReassignDecision):
                application = await self._decisions.apply_reassignment(admission.attempt.attempt_id)
            elif isinstance(decision, ForwardFeedbackDecision):
                application = await self._decisions.apply_feedback(admission.attempt.attempt_id)
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
        if self._usage_monitor is not None and usage_binding is not None:
            await self._usage_monitor.advance_subject(usage_binding.subject)
        return SubscriptionWorkOutcome(admission, attempt, application)

    async def _bind_usage(self, admission: SubscriptionAdmission) -> WorkUnitBinding | None:
        if self._usage_bindings is None:
            return None
        async with self._work_factory() as work:
            run = await work.runs.get(admission.attempt.run_id)
        subject = SubjectKey(
            project_id=run.project_id, kind="run", subject_id=admission.attempt.run_id
        )
        configured = configured_run_cohort(admission.envelope)
        effective = admission.task.route.effective
        route = RouteCohort.from_members(
            (
                RouteMember(
                    role=admission.task.purpose.value,
                    provider=effective.provider,
                    model=effective.model,
                    reasoning_effort=effective.effort.value,
                    ordinal=0,
                ),
            )
        )
        prior = await self._usage_bindings.binding_for_source(
            subject.project_id, "subscription_attempt", admission.attempt.attempt_id
        )
        binding = prior or WorkUnitBinding(
            subject=subject,
            work_unit_id=uuid4(),
            admitted_phase_id="implementation",
            configured_route=configured,
            effective_route=route,
            source=SourceBinding(
                kind="subscription_attempt",
                source_id=admission.attempt.attempt_id,
                parent_id=admission.attempt.task_id,
            ),
        )
        if binding.configured_route != configured or binding.effective_route != route:
            raise ValueError("stored usage route differs from admitted attempt")
        await self._usage_bindings.bind(binding)
        return binding

    async def _build_with_renewal(
        self,
        admission: SubscriptionAdmission,
        stop: asyncio.Event,
        *,
        heartbeat_seconds: float = 5,
    ) -> SubscriptionInvocationRequest:
        """Keep admission alive while optional source scoring prepares the request."""
        build = asyncio.create_task(self._requests.build(admission))
        stopping = asyncio.create_task(stop.wait())
        try:
            while not build.done():
                await asyncio.wait(
                    {build, stopping},
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopping.done():
                    raise asyncio.CancelledError
                if not build.done():
                    async with self._work_factory() as work:
                        await work.scheduler.renew(admission.lease, timedelta(seconds=30))
                        await work.commit()
            return await build
        except BaseException:
            if not build.done():
                build.cancel()
            await asyncio.gather(build, return_exceptions=True)
            raise
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

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
