"""Agent attempt-usage contract tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.agents.errors import AgentOutputInvalid
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentResult
from forge.observability.usage import UsageRecord


def _usage(
    *, run_id=None, execution_id=None, input_tokens=3, request_id="request-1"
) -> UsageRecord:
    return UsageRecord(
        provider="google",
        model="gemini",
        prompt_version="1",
        input_tokens=input_tokens,
        output_tokens=2,
        duration_ms=5,
        tool_call_count=1,
        provider_request_id=request_id,
        pricing_version="v1",
        estimated_cost_minor=1,
        currency="USD",
        run_id=run_id,
        agent_execution_id=execution_id,
    )


def test_result_detaches_two_exact_attempts_and_allows_rounding_difference() -> None:
    run_id, execution_id = uuid4(), uuid4()
    first = _usage(run_id=run_id, execution_id=execution_id, request_id="first")
    second = _usage(run_id=run_id, execution_id=execution_id, input_tokens=4, request_id="second")
    aggregate = UsageRecord(
        provider="google",
        model="gemini",
        prompt_version="1",
        input_tokens=7,
        output_tokens=4,
        duration_ms=10,
        tool_call_count=2,
        pricing_version="v1",
        estimated_cost_minor=3,
        currency="USD",
        run_id=run_id,
        agent_execution_id=execution_id,
    )

    result = AgentResult(
        execution_id=execution_id,
        role=AgentRole.PLANNER,
        finish_status=AgentFinishStatus.FAILED,
        output=None,
        provider="google",
        model="gemini",
        instruction_digest="a" * 64,
        usage=aggregate,
        usage_attempts=(first, second),
        tool_call_count=2,
        duration_ms=10,
    )

    assert result.usage_attempts == (first, second)
    assert result.usage_attempts[0] is not first


def test_attempts_reject_identity_or_measured_total_mismatch() -> None:
    run_id, execution_id = uuid4(), uuid4()
    aggregate = _usage(run_id=run_id, execution_id=execution_id)
    wrong_identity = _usage(run_id=uuid4(), execution_id=execution_id)
    with pytest.raises(ValueError, match="identity"):
        AgentOutputInvalid(usage=aggregate, usage_attempts=(wrong_identity,))

    with pytest.raises(ValueError, match="sum"):
        AgentOutputInvalid(
            usage=aggregate,
            usage_attempts=(_usage(run_id=run_id, execution_id=execution_id, input_tokens=4),),
        )


def test_failed_attempt_metadata_rejects_credential_pattern() -> None:
    usage = _usage(request_id="ghp_" + "A" * 36)
    with pytest.raises(ValueError):
        AgentOutputInvalid(usage=usage, usage_attempts=(usage,))


def test_failed_aggregate_metadata_rejects_credential_pattern_without_attempts() -> None:
    with pytest.raises(ValueError):
        AgentOutputInvalid(usage=_usage(request_id="ghp_" + "A" * 36))
