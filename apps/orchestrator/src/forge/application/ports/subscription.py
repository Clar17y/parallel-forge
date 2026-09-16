"""Typed durable subscription-runtime persistence contract."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.application.ports.tool_recovery import VerifiedTerminalEffect
from forge.domain.subscription import (
    AcceptDecision,
    AttemptIdentity,
    BoundScopeResponseDecision,
    BudgetPool,
    DelegateDecision,
    ExecutionEnvelope,
    ForwardFeedbackDecision,
    LogicalTaskContract,
    OperatorProfile,
    ReassignDecision,
    ReviewSelection,
    RouteBinding,
    ScopeRequestDecision,
    ScopeResponseDecision,
    TaskBudget,
    TaskHandoff,
    ToolCallBinding,
    WaitDecision,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolRequest

type DecisionRecord = (
    TaskHandoff
    | DelegateDecision
    | WaitDecision
    | ScopeRequestDecision
    | ScopeResponseDecision
    | AcceptDecision
    | ReassignDecision
    | ReviewSelection
    | ForwardFeedbackDecision
)


@dataclass(frozen=True, slots=True)
class InvocationTaskOutcome:
    task_id: UUID
    state: str
    version: int
    pause_requested: bool
    cancel_requested: bool
    recorded_handoff: TaskHandoff | None
    scope_request_attempt_id: UUID | None = None
    scope_request: ScopeRequestDecision | None = None
    scope_response_attempt_id: UUID | None = None
    scope_response: BoundScopeResponseDecision | None = None
    acceptance_attempt_id: UUID | None = None
    accepted_handoff_attempt_id: UUID | None = None
    acceptance: AcceptDecision | None = None


class SubscriptionRepository(Protocol):
    async def interrupted_effect_ids(
        self, after_id: UUID | None, limit: int
    ) -> tuple[UUID, ...]: ...
    async def reconcile_interrupted_effect(
        self, effect_id: UUID, *, verified_terminal: VerifiedTerminalEffect | None = None
    ) -> bool: ...
    async def authorize_tool(
        self, context: SubscriptionToolAuthorizationContext, request: ToolRequest
    ) -> LogicalTaskContract | None: ...
    async def store_profile(self, profile: OperatorProfile) -> OperatorProfile: ...
    async def list_profiles(self) -> list[OperatorProfile]: ...
    async def profile(self, profile_id: UUID, version: int) -> OperatorProfile: ...
    async def append_profile(
        self, profile: OperatorProfile, *, expected_current_version: int
    ) -> OperatorProfile: ...
    async def project_profile(self, project_id: UUID) -> OperatorProfile | None: ...
    async def select_project_profile_expected(
        self,
        project_id: UUID,
        profile: OperatorProfile,
        *,
        expected_profile_id: UUID | None,
        expected_profile_version: int | None,
    ) -> None: ...
    async def select_project_profile(self, project_id: UUID, profile: OperatorProfile) -> None: ...
    async def freeze_envelope(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...
    async def envelope_for_run(self, run_id: UUID) -> ExecutionEnvelope | None: ...
    async def create_task(
        self, contract: LogicalTaskContract, *, idempotency_key: str
    ) -> LogicalTaskContract: ...
    async def get_task(self, run_id: UUID, task_id: UUID) -> LogicalTaskContract: ...
    async def invocation_tasks(
        self, run_id: UUID, excluding_task_id: UUID
    ) -> tuple[LogicalTaskContract, ...]: ...
    async def invocation_outcomes(
        self, run_id: UUID, task_ids: tuple[UUID, ...]
    ) -> tuple[InvocationTaskOutcome, ...]: ...
    async def create_attempt(
        self,
        attempt: AttemptIdentity,
        *,
        route_payload: RouteBinding,
        idempotency_key: str,
    ) -> AttemptIdentity: ...
    async def bind_operation(
        self, binding: ToolCallBinding, *, run_id: UUID, task_id: UUID
    ) -> ToolCallBinding: ...
    async def record_operation_receipt(
        self,
        binding: ToolCallBinding,
        *,
        run_id: UUID,
        task_id: UUID,
        receipt: Mapping[str, object],
    ) -> ToolCallBinding: ...
    async def operation_receipt(
        self, binding: ToolCallBinding, *, run_id: UUID, task_id: UUID
    ) -> Mapping[str, object] | None: ...
    async def operation_evidence(
        self, operation_id: UUID, *, run_id: UUID, task_id: UUID, attempt_id: UUID
    ) -> tuple[ToolCallBinding, Mapping[str, object]] | None: ...

    async def launch_intent(
        self, attempt_id: UUID, launch_id: str, *, worker_identity: str
    ) -> None: ...
    async def launch_started(
        self,
        attempt_id: UUID,
        launch_id: str,
        *,
        worker_identity: str,
        pid: int,
        process_start_token: str,
    ) -> None: ...
    async def launch_finished(
        self,
        attempt_id: UUID,
        launch_id: str,
        *,
        worker_identity: str,
        terminal: SubscriptionLaunchTerminalProof | None,
        uncertain: bool,
    ) -> None: ...
    async def initialize_budget(
        self, run_id: UUID, task_id: UUID | None, total: TaskBudget
    ) -> BudgetPool: ...
    async def reserve_budget(
        self,
        run_id: UUID,
        task_id: UUID,
        budget: TaskBudget,
        *,
        reservation_id: UUID,
        idempotency_key: str,
    ) -> BudgetPool: ...
    async def settle_budget(
        self,
        run_id: UUID,
        task_id: UUID,
        budget: TaskBudget,
        *,
        reservation_id: UUID,
        consumed: bool,
    ) -> BudgetPool: ...
    async def record_decision[D: DecisionRecord](self, record: D, *, idempotency_key: str) -> D: ...
