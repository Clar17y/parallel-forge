"""Reusable agent result and usage validation helpers across roles."""

from __future__ import annotations

import json

from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    validate_usage_durable_metadata,
    validated_usage_attempts,
)
from forge.observability.usage import UsageRecord


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def safe_usage(usage: UsageRecord | None, request: AgentRequest) -> UsageRecord:
    """Return bounded, schema-safe usage with fallback for missing or unpriced records."""
    if usage is not None and usage_bound(usage, request):
        try:
            validate_usage_durable_metadata(usage)
        except TypeError, ValueError:
            usage = None
    if usage is not None and usage_bound(usage, request):
        if usage.pricing_version is not None and usage.currency is not None:
            return usage
        return UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            duration_ms=usage.duration_ms,
            tool_call_count=usage.tool_call_count,
            provider_request_id=usage.provider_request_id,
            pricing_version="unavailable-v1",
            currency="USD",
            unknown_price_reason="gateway_usage_unavailable",
        )
    return UsageRecord(
        provider=request.provider,
        model=request.model,
        prompt_version=request.instruction_version,
        pricing_version="unavailable-v1",
        currency="USD",
        unknown_price_reason="gateway_usage_unavailable",
    )


def safe_usage_attempts(
    aggregate: UsageRecord | None,
    attempts: tuple[UsageRecord, ...],
    request: AgentRequest,
) -> tuple[UsageRecord, ...]:
    """Validate attempt records against aggregate usage and request identity."""
    if aggregate is None or not usage_bound(aggregate, request):
        return ()
    try:
        validated = validated_usage_attempts(aggregate, attempts)
    except TypeError, ValueError:
        return ()
    return validated if all(usage_bound(attempt, request) for attempt in validated) else ()


def usage_attempts_bytes(attempts: tuple[UsageRecord, ...]) -> bytes:
    """Encode usage attempt records into canonical bytes for durable storage."""
    return _canonical_json_bytes(
        {
            "schema_version": 1,
            "attempts": [
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "prompt_version": attempt.prompt_version,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "cached_input_tokens": attempt.cached_input_tokens,
                    "duration_ms": attempt.duration_ms,
                    "tool_call_count": attempt.tool_call_count,
                    "provider_request_id": attempt.provider_request_id,
                    "pricing_version": attempt.pricing_version,
                    "estimated_cost_minor": attempt.estimated_cost_minor,
                    "currency": attempt.currency,
                    "unknown_price_reason": attempt.unknown_price_reason,
                    "run_id": str(attempt.run_id) if attempt.run_id is not None else None,
                    "agent_execution_id": (
                        str(attempt.agent_execution_id)
                        if attempt.agent_execution_id is not None
                        else None
                    ),
                }
                for attempt in attempts
            ],
        }
    )


def usage_bound(usage: UsageRecord, request: AgentRequest) -> bool:
    """Check that usage record identity attributes match the agent request."""
    return (
        type(usage) is UsageRecord
        and usage.provider == request.provider
        and usage.model == request.model
        and usage.prompt_version == request.instruction_version
        and (usage.run_id is None or usage.run_id == request.run_id)
        and (usage.agent_execution_id is None or usage.agent_execution_id == request.execution_id)
    )


def validate_agent_result(
    result: object,
    request: AgentRequest,
    *,
    expected_role: AgentRole,
) -> tuple[AgentFinishStatus, UsageRecord, tuple[UsageRecord, ...], str]:
    """Validate provider identity and usage before trusting status or output."""
    if type(result) is not AgentResult:
        return AgentFinishStatus.FAILED, safe_usage(None, request), (), "result_identity_mismatch"
    usage = safe_usage(result.usage, request)
    if (
        result.execution_id != request.execution_id
        or result.role != expected_role
        or request.role != expected_role
        or result.parent_execution_id != request.parent_execution_id
        or (
            expected_role in (AgentRole.PLANNER, AgentRole.REVIEWER)
            and result.parent_execution_id is not None
        )
        or result.provider != request.provider
        or result.model != request.model
        or result.instruction_digest != request.instruction_digest
        or not usage_bound(result.usage, request)
        or result.tool_call_count != result.usage.tool_call_count
        or result.duration_ms != result.usage.duration_ms
    ):
        return AgentFinishStatus.FAILED, usage, (), "result_identity_mismatch"
    attempts = safe_usage_attempts(result.usage, result.usage_attempts, request)
    if attempts != result.usage_attempts:
        return AgentFinishStatus.FAILED, usage, (), "result_identity_mismatch"
    if result.finish_status is AgentFinishStatus.SUCCEEDED:
        return AgentFinishStatus.SUCCEEDED, usage, attempts, ""
    return result.finish_status, usage, attempts, result.finish_status.value


__all__ = [
    "safe_usage",
    "safe_usage_attempts",
    "usage_attempts_bytes",
    "usage_bound",
    "validate_agent_result",
]
