"""Forge-facing, provider-neutral gateway over the raw Google ADK runtime."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, cast, runtime_checkable

from forge.agents.adk_runtime import (
    ADK_MAX_PAYLOAD_BYTES,
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkRuntimeProtocol,
    AdkTool,
)
from forge.agents.errors import AgentBudgetExceeded, AgentGatewayError, AgentOutputInvalid
from forge.agents.prompt_loader import PromptChanged, PromptLoader
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperOutput,
    ReviewOutput,
)
from forge.domain.plan import PlanOutput
from forge.domain.tool import ToolName
from forge.observability.usage import PricingCatalog, UsageRecord
from pydantic import BaseModel, ValidationError

_PG_INT32_MAX: Final[int] = 2_147_483_647
_MAX_TOOL_NAMES: Final[int] = 100
_MAX_VALIDATION_ERRORS: Final[int] = 100
_MAX_ERROR_LOCATION_SEGMENTS: Final[int] = 20
_SAFE_ERROR_CODE = re.compile(r"\A[a-z][a-z0-9_.-]{0,95}\Z", re.ASCII)
_SAFE_LOCATION_TEXT = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z", re.ASCII)
_PROVIDER_NAME = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,95}\Z", re.ASCII)
_CURRENCY = re.compile(r"\A[A-Z]{3}\Z", re.ASCII)
_VALIDATION_ERROR_MESSAGE: Final[str] = "output does not match the required role schema"


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundAdkTools:
    """The exact authorized tool names and closure-bound ADK tool objects."""

    names: tuple[ToolName, ...]
    tools: tuple[AdkTool, ...]

    def __post_init__(self) -> None:
        if type(self.names) is not tuple or type(self.tools) is not tuple:
            raise AgentGatewayError()
        if len(self.names) != len(self.tools) or len(self.names) > _MAX_TOOL_NAMES:
            raise AgentGatewayError()
        seen: set[ToolName] = set()
        for name in self.names:
            if type(name) is not ToolName or name in seen:
                raise AgentGatewayError()
            seen.add(name)
        for tool in self.tools:
            if not isinstance(tool, AdkTool):
                raise AgentGatewayError()

    def __repr__(self) -> str:
        return f"BoundAdkTools(names={self.names!r}, count={len(self.tools)})"


@runtime_checkable
class AdkToolProvider(Protocol):
    """Synchronous source of already-authorized, closure-bound ADK tools."""

    def tools_for(self, request: AgentRequest) -> BoundAdkTools: ...


@dataclass(frozen=True, slots=True)
class _RoleBinding:
    name: str
    schema: type[BaseModel]


_ROLE_BINDINGS: Final[dict[AgentRole, _RoleBinding]] = {
    AgentRole.PLANNER: _RoleBinding(name="planner", schema=PlanOutput),
    AgentRole.DEVELOPER: _RoleBinding(name="developer", schema=DeveloperOutput),
    AgentRole.REVIEWER: _RoleBinding(name="reviewer", schema=ReviewOutput),
}


@dataclass(frozen=True, slots=True)
class _BudgetLimits:
    input_tokens: int
    output_tokens: int
    tool_calls: int
    duration_ms: int
    cost_minor: int


@dataclass(slots=True)
class _AggregateUsage:
    """Checked aggregate counters retained only for one gateway execution."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_call_count: int = 0
    duration_ms: int = 0
    provider_cost_minor: int = 0
    provider_cost_known: bool = True
    attempt_count: int = 0
    provider_request_id: str | None = None

    def add(self, result: AdkInvocationResult) -> None:
        usage = result.usage
        self.input_tokens = _checked_add(self.input_tokens, usage.input_tokens)
        self.output_tokens = _checked_add(self.output_tokens, usage.output_tokens)
        self.cached_input_tokens = _checked_add(self.cached_input_tokens, usage.cached_input_tokens)
        self.tool_call_count = _checked_add(self.tool_call_count, usage.tool_call_count)
        self.duration_ms = _checked_add(self.duration_ms, result.duration_ms)
        if usage.cost_minor is None:
            self.provider_cost_known = False
        elif self.provider_cost_known:
            self.provider_cost_minor = _checked_add(self.provider_cost_minor, usage.cost_minor)

        self.attempt_count += 1
        if self.attempt_count == 1:
            self.provider_request_id = result.provider_request_id
        else:
            self.provider_request_id = None


def _checked_add(left: int, right: int) -> int:
    if type(left) is not int or type(right) is not int:
        raise AgentBudgetExceeded()
    total = left + right
    if total < 0 or total > _PG_INT32_MAX:
        raise AgentBudgetExceeded()
    return total


def _strict_metadata(value: object, *, maximum: int, uppercase: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise AgentGatewayError()
    if uppercase and value != value.upper():
        raise AgentGatewayError()
    return value


def _limits(request: AgentRequest) -> _BudgetLimits:
    budget = request.budget
    values = (
        budget.max_input_tokens,
        budget.max_output_tokens,
        budget.max_tool_calls,
        budget.max_duration_seconds,
        budget.max_cost_minor,
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise AgentBudgetExceeded()
    duration_ms = budget.max_duration_seconds * 1_000
    if duration_ms > _PG_INT32_MAX:
        raise AgentBudgetExceeded()
    if any(value > _PG_INT32_MAX for value in values[:3] + (budget.max_cost_minor,)):
        raise AgentBudgetExceeded()
    return _BudgetLimits(
        input_tokens=budget.max_input_tokens,
        output_tokens=budget.max_output_tokens,
        tool_calls=budget.max_tool_calls,
        duration_ms=duration_ms,
        cost_minor=budget.max_cost_minor,
    )


def _canonical_payload(value: object) -> tuple[object, str]:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = payload.encode("utf-8")
    except TypeError, ValueError, UnicodeError, OverflowError:
        raise AgentGatewayError() from None
    if len(encoded) > ADK_MAX_PAYLOAD_BYTES:
        raise AgentGatewayError()
    return value, payload


def _safe_error_code(value: object) -> str:
    if type(value) is str and _SAFE_ERROR_CODE.fullmatch(value) is not None:
        return value
    return "invalid_output"


def _safe_error_location(value: object) -> list[str | int]:
    if not isinstance(value, (tuple, list)):
        return ["output"]
    result: list[str | int] = []
    for item in value[:_MAX_ERROR_LOCATION_SEGMENTS]:
        if (type(item) is int and 0 <= item <= _PG_INT32_MAX) or (
            type(item) is str and _SAFE_LOCATION_TEXT.fullmatch(item) is not None
        ):
            result.append(item)
        else:
            result.append("field")
    return result or ["output"]


def _validation_errors(error: object) -> list[dict[str, object]]:
    raw_errors: list[Mapping[str, object]] = []
    if isinstance(error, ValidationError):
        try:
            for item in error.errors()[:_MAX_VALIDATION_ERRORS]:
                if isinstance(item, Mapping):
                    raw_errors.append(item)
        except Exception:  # noqa: BLE001 - custom validator details must stay contained
            raw_errors.clear()
    if not raw_errors:
        raw_errors.append({"type": "invalid_output", "loc": ("output",)})
    return [
        {
            "type": _safe_error_code(item.get("type")),
            "loc": _safe_error_location(item.get("loc")),
            "message": _VALIDATION_ERROR_MESSAGE,
        }
        for item in raw_errors
    ]


def _validate_role_output(
    schema: type[BaseModel], output_text: object
) -> tuple[BaseModel | None, list[dict[str, object]]]:
    if type(output_text) is not str:
        return None, _validation_errors(None)
    if not output_text.strip():
        return None, [
            {
                "type": "missing_output",
                "loc": ["output"],
                "message": _VALIDATION_ERROR_MESSAGE,
            }
        ]
    try:
        parsed = schema.model_validate_json(output_text, strict=True)
    except ValidationError as error:
        return None, _validation_errors(error)
    except TypeError, ValueError:
        return None, _validation_errors(None)
    except Exception:  # noqa: BLE001 - role validators are an untrusted extension seam
        return None, _validation_errors(None)
    if type(parsed) is not schema:
        return None, _validation_errors(None)
    return parsed, []


def _repair_payload(original: object, errors: list[dict[str, object]]) -> str:
    envelope = {"original_payload": original, "validation_errors": errors[:_MAX_VALIDATION_ERRORS]}
    _, payload = _canonical_payload(envelope)
    return payload


def _unpriced_usage(
    request: AgentRequest,
    aggregate: _AggregateUsage,
    *,
    provider_request_id: str | None,
) -> UsageRecord:
    try:
        return UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=aggregate.input_tokens,
            output_tokens=aggregate.output_tokens,
            cached_input_tokens=aggregate.cached_input_tokens,
            duration_ms=aggregate.duration_ms,
            tool_call_count=aggregate.tool_call_count,
            provider_request_id=provider_request_id,
            run_id=request.run_id,
            agent_execution_id=request.execution_id,
        )
    except TypeError, ValueError:
        raise AgentBudgetExceeded() from None


def _price_usage(
    catalog: PricingCatalog,
    usage: UsageRecord,
    *,
    currency: str,
) -> UsageRecord:
    try:
        priced = catalog.price(usage, currency=currency)
    except Exception:  # noqa: BLE001 - catalog implementations may fail arbitrarily
        raise AgentBudgetExceeded() from None
    estimate = priced.estimated_cost_minor
    if priced.unknown_price_reason is not None or estimate is None:
        raise AgentBudgetExceeded()
    if type(estimate) is not int or estimate < 0 or estimate > _PG_INT32_MAX:
        raise AgentBudgetExceeded()
    return priced


def _aggregate_effective_cost(aggregate: _AggregateUsage, priced: UsageRecord) -> int:
    estimate = priced.estimated_cost_minor
    if estimate is None:
        raise AgentBudgetExceeded()
    provider_cost = aggregate.provider_cost_minor if aggregate.provider_cost_known else 0
    effective = max(estimate, provider_cost)
    if effective > _PG_INT32_MAX:
        raise AgentBudgetExceeded()
    return effective


def _attempt_over_budget(
    result: AdkInvocationResult,
    limits: _BudgetLimits,
    effective_cost_minor: int,
) -> bool:
    usage = result.usage
    return (
        usage.input_tokens > limits.input_tokens
        or usage.output_tokens > limits.output_tokens
        or usage.tool_call_count > limits.tool_calls
        or result.duration_ms > limits.duration_ms
        or effective_cost_minor > limits.cost_minor
    )


def _status_for_finish(reason: AdkFinishReason) -> AgentFinishStatus:
    match reason:
        case AdkFinishReason.COMPLETED:
            return AgentFinishStatus.SUCCEEDED
        case AdkFinishReason.BUDGET_EXHAUSTED:
            return AgentFinishStatus.BUDGET_EXCEEDED
        case AdkFinishReason.TIMED_OUT:
            return AgentFinishStatus.TIMED_OUT
        case AdkFinishReason.CANCELLED:
            return AgentFinishStatus.CANCELLED
        case AdkFinishReason.FAILED:
            return AgentFinishStatus.FAILED


def _remaining(limit: int, used: int) -> int:
    if type(limit) is not int or type(used) is not int:
        raise AgentBudgetExceeded()
    if used > limit:
        return 0
    return limit - used


class GoogleAdkGateway:
    """Validate Forge requests, invoke ADK once or once for a bounded repair."""

    def __init__(
        self,
        runtime: AdkRuntimeProtocol,
        prompt_loader: PromptLoader,
        tool_provider: AdkToolProvider,
        pricing_catalog: PricingCatalog,
        *,
        supported_provider: str = "google",
        currency: str = "USD",
    ) -> None:
        if not isinstance(runtime, AdkRuntimeProtocol):
            raise AgentGatewayError()
        if not callable(getattr(prompt_loader, "verify_unchanged", None)):
            raise AgentGatewayError()
        if not isinstance(tool_provider, AdkToolProvider):
            raise AgentGatewayError()
        if not isinstance(pricing_catalog, PricingCatalog):
            raise AgentGatewayError()
        self._runtime = runtime
        self._prompt_loader = prompt_loader
        self._tool_provider = tool_provider
        self._pricing_catalog = pricing_catalog
        self._supported_provider = _strict_metadata(supported_provider, maximum=96)
        self._currency = _strict_metadata(currency, maximum=3, uppercase=True)
        if _PROVIDER_NAME.fullmatch(self._supported_provider) is None:
            raise AgentGatewayError()
        if _CURRENCY.fullmatch(self._currency) is None:
            raise AgentGatewayError()

    async def execute(self, request: AgentRequest) -> AgentResult:
        """Execute one exact role request, with at most one output-repair attempt."""
        if type(request) is not AgentRequest:
            raise AgentGatewayError()
        if request.provider != self._supported_provider:
            raise AgentGatewayError()
        binding = _ROLE_BINDINGS.get(request.role)
        if binding is None:
            raise AgentGatewayError()
        limits = _limits(request)
        original_payload_object, original_payload = self._serialize_context(request)
        self._ensure_catalog_support(request)

        self._verify_prompt(request)
        bound_tools = self._resolve_tools(request)
        first_invocation = self._invocation(
            request,
            binding,
            bound_tools,
            original_payload,
            limits,
        )
        self._verify_prompt(request)
        first_result = await self._invoke(first_invocation)
        first_priced = self._price_attempt(request, first_result)

        aggregate = _AggregateUsage()
        aggregate.add(first_result)
        first_effective_cost = self._effective_cost(first_result, first_priced)
        if _attempt_over_budget(first_result, limits, first_effective_cost):
            return self._result(
                request,
                aggregate,
                first_priced,
                AgentFinishStatus.BUDGET_EXCEEDED,
                None,
            )

        if first_result.finish_reason is not AdkFinishReason.COMPLETED:
            return self._result(
                request,
                aggregate,
                first_priced,
                _status_for_finish(first_result.finish_reason),
                None,
            )

        first_output, first_errors = _validate_role_output(binding.schema, first_result.output_text)
        if first_output is not None:
            return self._result(
                request,
                aggregate,
                first_priced,
                AgentFinishStatus.SUCCEEDED,
                cast(PlanOutput | DeveloperOutput | ReviewOutput, first_output),
            )

        remaining = _BudgetLimits(
            input_tokens=_remaining(limits.input_tokens, aggregate.input_tokens),
            output_tokens=_remaining(limits.output_tokens, aggregate.output_tokens),
            tool_calls=_remaining(limits.tool_calls, aggregate.tool_call_count),
            duration_ms=_remaining(limits.duration_ms, aggregate.duration_ms),
            cost_minor=_remaining(limits.cost_minor, first_effective_cost),
        )
        repair_payload = _repair_payload(original_payload_object, first_errors)
        repair_invocation = self._invocation(
            request,
            binding,
            bound_tools,
            repair_payload,
            remaining,
        )
        self._verify_prompt(request)
        second_result = await self._invoke(repair_invocation)
        second_priced = self._price_attempt(request, second_result)
        aggregate.add(second_result)
        second_effective_cost = self._effective_cost(second_result, second_priced)
        aggregate_priced = self._price_aggregate(request, aggregate)
        aggregate_effective_cost = _aggregate_effective_cost(aggregate, aggregate_priced)

        if (
            _attempt_over_budget(second_result, remaining, second_effective_cost)
            or aggregate_effective_cost > limits.cost_minor
        ):
            return self._result(
                request,
                aggregate,
                aggregate_priced,
                AgentFinishStatus.BUDGET_EXCEEDED,
                None,
            )
        if second_result.finish_reason is not AdkFinishReason.COMPLETED:
            return self._result(
                request,
                aggregate,
                aggregate_priced,
                _status_for_finish(second_result.finish_reason),
                None,
            )

        second_output, _ = _validate_role_output(binding.schema, second_result.output_text)
        if second_output is None:
            raise AgentOutputInvalid()
        return self._result(
            request,
            aggregate,
            aggregate_priced,
            AgentFinishStatus.SUCCEEDED,
            cast(PlanOutput | DeveloperOutput | ReviewOutput, second_output),
        )

    def _serialize_context(self, request: AgentRequest) -> tuple[object, str]:
        try:
            payload_object = request.context.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - custom Pydantic serializers stay contained
            raise AgentGatewayError() from None
        if not isinstance(payload_object, Mapping):
            raise AgentGatewayError()
        return _canonical_payload(payload_object)

    def _verify_prompt(self, request: AgentRequest) -> None:
        try:
            self._prompt_loader.verify_unchanged(request)
        except PromptChanged:
            raise
        except OSError, RuntimeError, TypeError, ValueError:
            raise AgentGatewayError() from None

    def _resolve_tools(self, request: AgentRequest) -> BoundAdkTools:
        try:
            bound = self._tool_provider.tools_for(request)
        except Exception:  # noqa: BLE001 - injected provider is a trust boundary
            raise AgentGatewayError() from None
        if type(bound) is not BoundAdkTools or bound.names != request.allowed_tools:
            raise AgentGatewayError()
        return bound

    @staticmethod
    def _invocation(
        request: AgentRequest,
        binding: _RoleBinding,
        bound_tools: BoundAdkTools,
        payload: str,
        limits: _BudgetLimits,
    ) -> AdkInvocation:
        try:
            return AdkInvocation(
                agent_name=binding.name,
                model=request.model,
                instruction=request.system_instruction,
                output_schema=binding.schema,
                tools=bound_tools.tools,
                user_id=str(request.run_id),
                session_id=str(request.execution_id),
                user_payload_json=payload,
                max_input_tokens=limits.input_tokens,
                max_output_tokens=limits.output_tokens,
                max_tool_calls=limits.tool_calls,
                max_duration_ms=limits.duration_ms,
                max_cost_minor=limits.cost_minor,
            )
        except TypeError, ValueError:
            raise AgentBudgetExceeded() from None

    async def _invoke(self, invocation: AdkInvocation) -> AdkInvocationResult:
        try:
            result = await self._runtime.invoke(invocation)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - injected runtime is a trust boundary
            raise AgentGatewayError() from None
        if type(result) is not AdkInvocationResult:
            raise AgentGatewayError()
        return result

    def _price_attempt(self, request: AgentRequest, result: AdkInvocationResult) -> UsageRecord:
        usage = _AggregateUsage()
        usage.add(result)
        unpriced = _unpriced_usage(
            request,
            usage,
            provider_request_id=result.provider_request_id,
        )
        return _price_usage(self._pricing_catalog, unpriced, currency=self._currency)

    def _ensure_catalog_support(self, request: AgentRequest) -> None:
        zero = _AggregateUsage()
        try:
            reason = self._pricing_catalog.unknown_reason(
                _unpriced_usage(request, zero, provider_request_id=None),
                currency=self._currency,
            )
        except Exception:  # noqa: BLE001 - catalog implementations may fail arbitrarily
            raise AgentBudgetExceeded() from None
        if reason in {"unknown_currency", "unknown_model_price"}:
            raise AgentBudgetExceeded()

    @staticmethod
    def _effective_cost(result: AdkInvocationResult, priced: UsageRecord) -> int:
        estimate = priced.estimated_cost_minor
        if estimate is None:
            raise AgentBudgetExceeded()
        provider_cost = result.usage.cost_minor
        effective = estimate if provider_cost is None else max(estimate, provider_cost)
        if effective > _PG_INT32_MAX:
            raise AgentBudgetExceeded()
        return effective

    def _price_aggregate(self, request: AgentRequest, aggregate: _AggregateUsage) -> UsageRecord:
        return _price_usage(
            self._pricing_catalog,
            _unpriced_usage(
                request,
                aggregate,
                provider_request_id=(
                    aggregate.provider_request_id if aggregate.attempt_count == 1 else None
                ),
            ),
            currency=self._currency,
        )

    @staticmethod
    def _result(
        request: AgentRequest,
        aggregate: _AggregateUsage,
        priced: UsageRecord,
        status: AgentFinishStatus,
        output: PlanOutput | DeveloperOutput | ReviewOutput | None,
    ) -> AgentResult:
        if status is not AgentFinishStatus.SUCCEEDED:
            output = None
        try:
            return AgentResult(
                execution_id=request.execution_id,
                role=request.role,
                finish_status=status,
                output=output,
                parent_execution_id=request.parent_execution_id,
                provider=request.provider,
                model=request.model,
                instruction_digest=request.instruction_digest,
                usage=priced,
                tool_call_count=aggregate.tool_call_count,
                duration_ms=aggregate.duration_ms,
            )
        except TypeError, ValueError:
            raise AgentGatewayError() from None


__all__ = ["AdkToolProvider", "BoundAdkTools", "GoogleAdkGateway"]
