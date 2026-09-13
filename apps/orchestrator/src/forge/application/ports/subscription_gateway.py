"""Neutral boundary for one frozen subscription-provider attempt."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from forge.domain.plan import PlanOutput
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptDecision,
    AttemptIdentity,
    AttemptTelemetry,
    BrokerAuthorizationBinding,
    DelegateDecision,
    ExecutionEnvelope,
    LogicalTaskContract,
    ReassignDecision,
    ReviewSelection,
    ScopeRequestDecision,
    ScopeResponseDecision,
    TaskBudget,
    TaskHandoff,
    WaitDecision,
)
from forge.domain.subscription_budget import budget_ceiling
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof

type SubscriptionDecision = (
    TaskHandoff
    | PlanOutput
    | DelegateDecision
    | WaitDecision
    | ScopeRequestDecision
    | ScopeResponseDecision
    | AcceptDecision
    | ReassignDecision
    | ReviewSelection
)


class SubscriptionFailure(StrEnum):
    UNAVAILABLE = "unavailable"
    PROTOCOL = "protocol"
    INTERRUPTED = "interrupted"
    DEADLINE = "deadline"
    POLICY_DENIED = "policy_denied"
    QUOTA = "quota"
    AUTHENTICATION = "authentication"
    UNCERTAIN = "uncertain"
    THROTTLED = "throttled"
    OUTAGE = "outage"
    UNSUPPORTED = "unsupported"
    BUDGET = "budget"


def _freeze(value: object, *, depth: int = 0, counter: list[int] | None = None) -> object:
    counter = [0, 0] if counter is None else counter
    counter[0] += 1
    if depth > 24 or counter[0] > 10_000 or counter[1] > 1_048_576:
        raise ValueError("subscription context exceeds its bound")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("untrusted context keys must be strings")
        counter[1] += sum(len(key.encode("utf-8")) for key in value)
        return MappingProxyType(
            {key: _freeze(item, depth=depth + 1, counter=counter) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item, depth=depth + 1, counter=counter) for item in value)
    if type(value) is float and not math.isfinite(value):
        raise TypeError("untrusted context must not contain nonfinite numbers")
    if value is None or type(value) in (str, int, float, bool):
        counter[1] += len(str(value).encode("utf-8"))
        if counter[1] > 1_048_576:
            raise ValueError("subscription context exceeds its bound")
        return value
    raise TypeError("untrusted context must contain JSON values")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionInvocationRequest:
    task: LogicalTaskContract
    attempt: AttemptIdentity
    envelope: ExecutionEnvelope
    authorization: BrokerAuthorizationBinding
    prompt_version: str
    trusted_system_prompt: str
    untrusted_context: Mapping[str, object]
    known_tasks: tuple[LogicalTaskContract, ...] = ()
    run_state: RunState | None = None
    attempt_budget: TaskBudget | None = None

    def __post_init__(self) -> None:
        if self.run_state is not None and not isinstance(self.run_state, RunState):
            raise TypeError("run_state must be a RunState")
        if not isinstance(self.task, LogicalTaskContract) or not isinstance(
            self.attempt, AttemptIdentity
        ):
            raise TypeError("task and attempt must be subscription contracts")
        if not isinstance(self.authorization, BrokerAuthorizationBinding):
            raise TypeError("authorization must be a broker authorization binding")
        if (self.attempt.run_id, self.attempt.task_id) != (self.task.run_id, self.task.task_id):
            raise ValueError("attempt identity does not match task")
        if (
            self.authorization.run_id,
            self.authorization.task_id,
            self.authorization.attempt_id,
        ) != (self.attempt.run_id, self.attempt.task_id, self.attempt.attempt_id):
            raise ValueError("authorization identity does not match attempt")
        if self.authorization.role is not self.task.purpose:
            raise ValueError("authorization role does not match task purpose")
        if not isinstance(self.envelope, ExecutionEnvelope) or (
            self.envelope.run_id != self.attempt.run_id
            or self.envelope.safety_policy_version != self.authorization.policy_version
        ):
            raise ValueError("authorization envelope differs")
        binding = self.task.route
        if not self.envelope.permits_route(self.task.purpose, binding):
            raise ValueError("task route exceeds frozen envelope")
        if self.task.budget.billing_mode is not binding.effective.billing_mode:
            raise ValueError("task budget billing differs from route")
        if self.attempt_budget is not None:
            reserved = self.attempt_budget
            if not isinstance(reserved, TaskBudget):
                raise TypeError("attempt budget must be a TaskBudget")
            policy = self.task.budget.unknown_telemetry_policy
            unknown = reserved.unknown_telemetry_policy
            if (
                reserved.billing_mode is not self.task.budget.billing_mode
                or reserved.max_provider_attempts != 1
                or reserved.max_repairs != 0
                or not budget_ceiling(reserved).fits_within(self.task.budget)
                or (unknown.allow_unknown_tokens and not policy.allow_unknown_tokens)
                or (unknown.allow_unknown_cost and not policy.allow_unknown_cost)
                or (unknown.allow_unknown_quota and not policy.allow_unknown_quota)
                or unknown.max_uncertain_attempts > policy.max_uncertain_attempts
            ):
                raise ValueError("attempt budget exceeds task authority")
        if type(self.prompt_version) is not str or not self.prompt_version:
            raise ValueError("prompt_version must be nonempty")
        if type(self.trusted_system_prompt) is not str or not self.trusted_system_prompt:
            raise ValueError("trusted_system_prompt must be nonempty")
        if (
            len(self.prompt_version.encode("utf-8")) > 96
            or len(self.trusted_system_prompt.encode("utf-8")) > 65_536
        ):
            raise ValueError("trusted prompt exceeds its bound")
        if not isinstance(self.untrusted_context, Mapping):
            raise TypeError("untrusted context must be an object")
        object.__setattr__(self, "untrusted_context", _freeze(self.untrusted_context))
        known = tuple(self.known_tasks)
        if len(known) > 256 or any(
            not isinstance(task, LogicalTaskContract) or task.run_id != self.attempt.run_id
            for task in known
        ):
            raise ValueError("known task context is invalid")
        if len({task.task_id for task in known}) != len(known) or self.task.task_id in {
            task.task_id for task in known
        }:
            raise ValueError("known task context has duplicate identities")
        object.__setattr__(self, "known_tasks", known)

    @property
    def budget(self) -> TaskBudget:
        """Execution ceiling; the task contract still owns aggregate authority."""
        return self.attempt_budget if self.attempt_budget is not None else self.task.budget


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionInvocationResult:
    attempt: AttemptIdentity
    decision: SubscriptionDecision | None = None
    telemetry: AttemptTelemetry = field(default_factory=AttemptTelemetry)
    failure: SubscriptionFailure | None = None
    failure_detail: str | None = None
    quota_exhaustion: QuotaExhaustion | None = None
    launch_proof: SubscriptionLaunchTerminalProof | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, AttemptIdentity):
            raise TypeError("result requires exact attempt identity")
        if self.launch_proof is not None and not isinstance(
            self.launch_proof, SubscriptionLaunchTerminalProof
        ):
            raise TypeError("launch proof must be typed supervisor evidence")
        if self.failure is not None and not isinstance(self.failure, SubscriptionFailure):
            raise TypeError("invalid subscription failure")
        if self.quota_exhaustion is not None and not isinstance(
            self.quota_exhaustion, QuotaExhaustion
        ):
            raise TypeError("quota exhaustion must be typed evidence")
        if self.quota_exhaustion is not None and self.failure not in {
            SubscriptionFailure.QUOTA,
            SubscriptionFailure.UNCERTAIN,
            SubscriptionFailure.INTERRUPTED,
            SubscriptionFailure.DEADLINE,
        }:
            raise ValueError("quota exhaustion requires quota failure")
        if self.failure_detail is not None and (
            type(self.failure_detail) is not str or len(self.failure_detail) > 1024
        ):
            raise ValueError("failure detail exceeds its bound")
        if not isinstance(self.telemetry, AttemptTelemetry):
            raise TypeError("telemetry must be AttemptTelemetry")
        if self.failure is None and self.decision is None:
            raise ValueError("a successful invocation requires a decision")
        if self.failure is not None and self.decision is not None:
            raise ValueError("a failed invocation cannot contain a decision")
        if self.decision is not None and not isinstance(
            self.decision,
            (
                TaskHandoff,
                PlanOutput,
                DelegateDecision,
                WaitDecision,
                ScopeRequestDecision,
                ScopeResponseDecision,
                AcceptDecision,
                ReassignDecision,
                ReviewSelection,
            ),
        ):
            raise TypeError("decision must be a typed subscription decision")
        decision = self.decision
        if decision is not None and not isinstance(decision, PlanOutput):
            if decision.run_id != self.attempt.run_id:
                raise ValueError("decision run identity does not match attempt")
            if isinstance(decision, TaskHandoff) and (
                decision.task_id != self.attempt.task_id
                or decision.attempt_id != self.attempt.attempt_id
            ):
                raise ValueError("handoff identity does not match attempt")
            if isinstance(decision, (WaitDecision, ScopeRequestDecision)) and (
                decision.task_id != self.attempt.task_id
            ):
                raise ValueError("decision task identity does not match attempt")
            if isinstance(decision, DelegateDecision) and (
                decision.parent_task_id != self.attempt.task_id
            ):
                raise ValueError("delegation parent identity does not match attempt")


@runtime_checkable
class SubscriptionGateway(Protocol):
    async def execute(
        self, request: SubscriptionInvocationRequest
    ) -> SubscriptionInvocationResult: ...


class SubscriptionInterrupted(asyncio.CancelledError):
    """Cancellation carrying measured, attempt-bound settlement evidence."""

    def __init__(self, result: SubscriptionInvocationResult) -> None:
        super().__init__("subscription attempt interrupted")
        self.result = result


__all__ = [
    "SubscriptionDecision",
    "SubscriptionFailure",
    "SubscriptionGateway",
    "SubscriptionInterrupted",
    "SubscriptionInvocationRequest",
    "SubscriptionInvocationResult",
]
