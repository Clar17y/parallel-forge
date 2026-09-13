"""Resume supported settled decisions through their existing evidence guards."""

from collections.abc import Callable
from dataclasses import dataclass

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_decisions import PendingDecisionKind
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.subscription_acceptance_dispatch import (
    SubscriptionAcceptanceDispatch,
)
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_handoff_application import (
    SubscriptionHandoffApplication,
)
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService


@dataclass(frozen=True, slots=True)
class SubscriptionDecisionRecoveryReport:
    applied: int = 0
    deferred: int = 0
    unsupported: int = 0
    stopped_tasks: int = 0
    deferred_stops: int = 0


class SubscriptionDecisionRecovery:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        artifacts: ArtifactStore,
        *,
        page_size: int = 100,
        handoffs: SubscriptionHandoffApplication | None = None,
        candidates: SubscriptionCandidateApplication | None = None,
        acceptance: SubscriptionAcceptanceDispatch | None = None,
        task_controls: SubscriptionTaskControlService | None = None,
    ) -> None:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("recovery page size must be 1 to 100")
        self._factory, self._page_size = work_factory, page_size
        self._plans = SubscriptionPlanGateService(artifacts, work_factory)
        self._decisions = SubscriptionDecisionApplication(work_factory)
        self._handoffs = handoffs
        self._candidates = candidates
        self._acceptance = acceptance
        self._task_controls = task_controls

    async def reconcile_all(self) -> SubscriptionDecisionRecoveryReport:
        applied = deferred = unsupported = 0
        controls = await self._task_controls.reconcile_all() if self._task_controls else None
        cursor = None
        while True:
            async with self._factory() as work:
                candidates = await work.subscription_decisions.pending_applications(
                    cursor, self._page_size
                )
                await work.rollback()
            if not candidates:
                return SubscriptionDecisionRecoveryReport(
                    applied,
                    deferred,
                    unsupported,
                    stopped_tasks=controls.stopped if controls else 0,
                    deferred_stops=controls.deferred if controls else 0,
                )
            for candidate in candidates:
                try:
                    if candidate.kind is PendingDecisionKind.PLAN:
                        await self._plans.request_settled(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.DELEGATE:
                        await self._decisions.apply_delegation(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.WAIT:
                        await self._decisions.apply_wait(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.REASSIGN:
                        await self._decisions.apply_reassignment(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.SCOPE_REQUEST:
                        await self._decisions.apply_scope_request(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.SCOPE_RESPONSE:
                        await self._decisions.apply_scope_response(candidate.attempt_id)
                    elif candidate.kind is PendingDecisionKind.TASK_ACCEPTANCE:
                        await self._decisions.prepare_acceptance(candidate.attempt_id)
                    elif (
                        candidate.kind is PendingDecisionKind.FINAL_ACCEPTANCE
                        and self._acceptance is not None
                    ):
                        await self._acceptance.apply(candidate.attempt_id)
                    elif (
                        candidate.kind is PendingDecisionKind.HANDOFF and self._handoffs is not None
                    ):
                        await self._handoffs.apply(candidate.attempt_id)
                    elif (
                        candidate.kind is PendingDecisionKind.REVIEW_SELECTION
                        and self._candidates is not None
                    ):
                        await self._candidates.apply(candidate.attempt_id)
                    else:
                        unsupported += 1
                        continue
                except Exception:  # noqa: BLE001 - defer this source and continue other runs
                    # No untrusted provider text or exception details enter the
                    # report. Cancellation propagates; source guards stay intact.
                    deferred += 1
                else:
                    applied += 1
            cursor = candidates[-1].attempt_id
