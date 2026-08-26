"""Deterministic scripted fake agent gateway for service tests and local demos."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperOutput,
    ReviewOutput,
)
from forge.domain.plan import PlanOutput
from forge.observability.usage import UsageRecord

_NONNEGATIVE_INT32_FLOOR: Final[int] = 0
_PG_INT32_MAX: Final[int] = 2_147_483_647
_MAX_STEPS_PER_ROLE: Final[int] = 1_000
_MAX_RECORDED_REQUESTS: Final[int] = _MAX_STEPS_PER_ROLE * len(AgentRole)
_PRICING_VERSION: Final[str] = "fake-v1"
_CURRENCY: Final[str] = "USD"


def _validate_nonnegative_int32(value: object, name: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < _NONNEGATIVE_INT32_FLOOR or value > _PG_INT32_MAX:
        raise ValueError(f"{name} must be between {_NONNEGATIVE_INT32_FLOOR} and {_PG_INT32_MAX}")
    return value


class FakeAgentScenario(StrEnum):
    """Closed scenarios supported by scripted fake agent steps."""

    SUCCESS = "success"
    INVALID_SCHEMA = "invalid_schema"
    VALIDATION_FAILURE = "validation_failure"
    TIMEOUT = "timeout"
    TOOL_DENIED = "tool_denied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FakeScriptExhausted(RuntimeError):
    """A role script is missing or has no remaining steps."""

    def __init__(self) -> None:
        super().__init__("fake agent script is exhausted")


class FakeScriptInvalid(ValueError):
    """Fake gateway script configuration or step output is invalid for the role."""

    def __init__(self) -> None:
        super().__init__("fake agent script configuration is invalid")


@dataclass(frozen=True, slots=True)
class FakeAgentStep:
    """One immutable scripted execution step for a fake agent role."""

    scenario: FakeAgentScenario
    output: PlanOutput | DeveloperOutput | ReviewOutput | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_calls: int = 0
    duration_ms: int = 0
    cost_minor: int = 0

    def __post_init__(self) -> None:
        if type(self.scenario) is not FakeAgentScenario:
            raise TypeError("scenario must be a FakeAgentScenario")
        _validate_nonnegative_int32(self.input_tokens, "input_tokens")
        _validate_nonnegative_int32(self.output_tokens, "output_tokens")
        _validate_nonnegative_int32(self.cached_input_tokens, "cached_input_tokens")
        _validate_nonnegative_int32(self.tool_calls, "tool_calls")
        _validate_nonnegative_int32(self.duration_ms, "duration_ms")
        _validate_nonnegative_int32(self.cost_minor, "cost_minor")

        if self.scenario == FakeAgentScenario.SUCCESS:
            if type(self.output) not in {PlanOutput, DeveloperOutput, ReviewOutput}:
                raise TypeError(
                    "success step requires an exact PlanOutput, DeveloperOutput, or ReviewOutput"
                )
        else:
            if self.output is not None:
                raise ValueError(f"{self.scenario.value} step must not specify structured output")

    def __repr__(self) -> str:
        return (
            f"FakeAgentStep(scenario={self.scenario.value!r}, "
            f"input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, "
            f"cached_input_tokens={self.cached_input_tokens}, "
            f"tool_calls={self.tool_calls}, "
            f"duration_ms={self.duration_ms}, "
            f"cost_minor={self.cost_minor}, "
            f"has_output={self.output is not None})"
        )

    @classmethod
    def success(
        cls,
        output: PlanOutput | DeveloperOutput | ReviewOutput,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a successful scripted step with exact structured output."""
        return cls(
            scenario=FakeAgentScenario.SUCCESS,
            output=output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def invalid_schema(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct an invalid_schema scripted step."""
        return cls(
            scenario=FakeAgentScenario.INVALID_SCHEMA,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def validation_failure(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a validation_failure scripted step."""
        return cls(
            scenario=FakeAgentScenario.VALIDATION_FAILURE,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def timeout(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a timeout scripted step."""
        return cls(
            scenario=FakeAgentScenario.TIMEOUT,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def tool_denied(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a tool_denied scripted step."""
        return cls(
            scenario=FakeAgentScenario.TOOL_DENIED,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def failed(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a failed scripted step."""
        return cls(
            scenario=FakeAgentScenario.FAILED,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        tool_calls: int = 0,
        duration_ms: int = 0,
        cost_minor: int = 0,
    ) -> FakeAgentStep:
        """Construct a cancelled scripted step."""
        return cls(
            scenario=FakeAgentScenario.CANCELLED,
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            cost_minor=cost_minor,
        )


def _validate_role_step_compatibility(role: AgentRole, step: FakeAgentStep) -> None:
    if step.scenario == FakeAgentScenario.SUCCESS:
        if role == AgentRole.PLANNER and type(step.output) is not PlanOutput:
            raise FakeScriptInvalid
        if role == AgentRole.DEVELOPER and type(step.output) is not DeveloperOutput:
            raise FakeScriptInvalid
        if role == AgentRole.REVIEWER and type(step.output) is not ReviewOutput:
            raise FakeScriptInvalid


class FakeAgentGateway:
    """Deterministic, provider-free scripted fake implementing AgentGateway."""

    def __init__(self, scripts: Mapping[AgentRole, Sequence[FakeAgentStep]]) -> None:
        if not isinstance(scripts, Mapping):
            raise TypeError(
                "scripts must be a mapping from AgentRole to sequences of FakeAgentStep"
            )
        if len(scripts) == 0:
            raise ValueError("scripts mapping must not be empty")

        copied_scripts: dict[AgentRole, tuple[FakeAgentStep, ...]] = {}
        for role, steps in scripts.items():
            if type(role) is not AgentRole:
                raise TypeError("script role keys must be AgentRole members")
            if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
                raise TypeError("script steps must be a sequence of FakeAgentStep instances")
            if len(steps) == 0:
                raise ValueError(f"script sequence for role {role.value} must not be empty")
            if len(steps) > _MAX_STEPS_PER_ROLE:
                raise ValueError(
                    f"script sequence for role {role.value} exceeds maximum of {_MAX_STEPS_PER_ROLE} steps"
                )

            frozen_steps: list[FakeAgentStep] = []
            for step in steps:
                if type(step) is not FakeAgentStep:
                    raise TypeError("script steps must be FakeAgentStep instances")
                _validate_role_step_compatibility(role, step)
                frozen_steps.append(step)
            copied_scripts[role] = tuple(frozen_steps)

        self._scripts: Mapping[AgentRole, tuple[FakeAgentStep, ...]] = MappingProxyType(
            copied_scripts
        )
        self._requests: list[AgentRequest] = []
        self._invocation_counts: dict[AgentRole, int] = {}
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def requests(self) -> tuple[AgentRequest, ...]:
        """Return an immutable snapshot tuple of all admitted requests in admission order."""
        return tuple(self._requests)

    def invocation_count(self, role: AgentRole) -> int:
        """Return the number of consumed steps for one role."""
        if type(role) is not AgentRole:
            raise TypeError("role must be an AgentRole")
        return self._invocation_counts.get(role, 0)

    def __repr__(self) -> str:
        roles = tuple(r.value for r in self._scripts)
        return f"FakeAgentGateway(configured_roles={roles!r})"

    async def execute(self, request: AgentRequest) -> AgentResult:
        """Execute one validated agent request against the scripted steps."""
        if type(request) is not AgentRequest:
            raise TypeError("request must be an AgentRequest")
        if any(
            value != value.strip()
            for value in (request.provider, request.model, request.instruction_version)
        ):
            raise FakeScriptInvalid

        async with self._admission_lock():
            role = request.role
            script = self._scripts.get(role)
            count = self._invocation_counts.get(role, 0)
            if len(self._requests) >= _MAX_RECORDED_REQUESTS:
                raise FakeScriptExhausted
            self._requests.append(request)
            if script is None or count >= len(script):
                raise FakeScriptExhausted

            step = script[count]
            _validate_role_step_compatibility(role, step)

            self._invocation_counts[role] = count + 1
            invocation_number = count + 1

        provider_request_id = f"{role.value}-{invocation_number}"
        usage = UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            cached_input_tokens=step.cached_input_tokens,
            duration_ms=step.duration_ms,
            tool_call_count=step.tool_calls,
            provider_request_id=provider_request_id,
            pricing_version=_PRICING_VERSION,
            estimated_cost_minor=step.cost_minor,
            currency=_CURRENCY,
            unknown_price_reason=None,
        )

        budget = request.budget
        budget_exceeded = (
            step.input_tokens > budget.max_input_tokens
            or step.output_tokens > budget.max_output_tokens
            or step.tool_calls > budget.max_tool_calls
            or step.duration_ms > budget.max_duration_seconds * 1000
            or step.cost_minor > budget.max_cost_minor
        )

        if budget_exceeded:
            return AgentResult(
                execution_id=request.execution_id,
                role=role,
                finish_status=AgentFinishStatus.BUDGET_EXCEEDED,
                output=None,
                parent_execution_id=request.parent_execution_id,
                provider=request.provider,
                model=request.model,
                instruction_digest=request.instruction_digest,
                usage=usage,
                tool_call_count=step.tool_calls,
                duration_ms=step.duration_ms,
            )

        match step.scenario:
            case FakeAgentScenario.SUCCESS:
                finish_status = AgentFinishStatus.SUCCEEDED
                output = step.output
            case FakeAgentScenario.INVALID_SCHEMA | FakeAgentScenario.VALIDATION_FAILURE:
                finish_status = AgentFinishStatus.INVALID_OUTPUT
                output = None
            case FakeAgentScenario.TIMEOUT:
                finish_status = AgentFinishStatus.TIMED_OUT
                output = None
            case FakeAgentScenario.TOOL_DENIED:
                finish_status = AgentFinishStatus.TOOL_DENIED
                output = None
            case FakeAgentScenario.FAILED:
                finish_status = AgentFinishStatus.FAILED
                output = None
            case FakeAgentScenario.CANCELLED:
                finish_status = AgentFinishStatus.CANCELLED
                output = None

        return AgentResult(
            execution_id=request.execution_id,
            role=role,
            finish_status=finish_status,
            output=output,
            parent_execution_id=request.parent_execution_id,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            usage=usage,
            tool_call_count=step.tool_calls,
            duration_ms=step.duration_ms,
        )

    def _admission_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._lock
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
            self._lock_loop = loop
        elif self._lock_loop is not loop:
            if lock.locked():
                raise FakeScriptInvalid
            lock = asyncio.Lock()
            self._lock = lock
            self._lock_loop = loop
        return lock


__all__ = [
    "FakeAgentGateway",
    "FakeAgentScenario",
    "FakeAgentStep",
    "FakeScriptExhausted",
    "FakeScriptInvalid",
]
