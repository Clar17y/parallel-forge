"""Tests for extracted agent result and usage validation helpers."""

from __future__ import annotations

import hashlib
import json
from typing import cast
from uuid import UUID, uuid4

import pytest
from forge.application.services.agent_results import (
    safe_usage,
    safe_usage_attempts,
    usage_attempts_bytes,
    usage_bound,
    validate_agent_result,
)
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperInput,
    DeveloperOutput,
    PlannerInput,
    PlanOutput,
    PolicySummary,
    ReviewDecision,
    ReviewerInput,
    ReviewOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord
from pydantic import ValidationError


def _instruction() -> str:
    return "<!-- forge-instruction-version: 1 -->\nExecute assigned task."


def _instruction_digest(instruction: str) -> str:
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def _make_budget() -> AgentBudget:
    return AgentBudget(
        max_input_tokens=10_000,
        max_output_tokens=2_000,
        max_tool_calls=20,
        max_duration_seconds=60,
        max_cost_minor=500,
    )


def _make_plan_output() -> PlanOutput:
    return PlanOutput(
        summary="Safe implementation plan for feature XYZ",
        assumptions=("Repository is clean",),
        affected_components=("orchestrator",),
        steps=("Step 1: write tests", "Step 2: implement"),
        required_checks=("pytest -q",),
        risks=("Regression risk in edge case",),
        security_considerations=("No secrets exposed",),
        dependency_changes=(),
    )


def _make_developer_output() -> DeveloperOutput:
    return DeveloperOutput(
        summary="Implemented feature XYZ with test coverage",
        changed_paths=("src/forge/domain/feature.py",),
        tests_added_or_changed=("tests/domain/test_feature.py",),
        named_checks_run=("unit", "lint"),
        local_commit_sha="a" * 40,
        diff_digest="b" * 64,
        unresolved_concerns=(),
        plan_deviations=(),
    )


def _make_review_output() -> ReviewOutput:
    return ReviewOutput(
        decision=ReviewDecision.APPROVE,
        findings=(),
        tested_claims=("Tests pass",),
        missing_evidence=(),
        summary="Review passed with no remaining issues",
    )


def _make_planner_context() -> PlannerInput:
    return PlannerInput(
        original_task=UntrustedContent.from_text(
            "Plan the work",
            source_kind=UntrustedSourceKind.TASK,
            source_reference="task",
        ),
        base_commit="a" * 40,
        repository_tree=UntrustedContent.from_text(
            "(empty)",
            source_kind=UntrustedSourceKind.REPOSITORY_TREE,
            source_reference=".",
        ),
        policy_summary=PolicySummary(policy_id=uuid4(), policy_version=1),
    )


def _make_developer_context() -> DeveloperInput:
    return DeveloperInput(
        original_task=UntrustedContent.from_text(
            "Implement feature",
            source_kind=UntrustedSourceKind.TASK,
            source_reference="task",
        ),
        plan=_make_plan_output(),
        worktree_id="forge-worktree-1",
        base_commit="b" * 40,
    )


def _make_reviewer_context() -> ReviewerInput:
    return ReviewerInput(
        original_task=UntrustedContent.from_text(
            "Review changes",
            source_kind=UntrustedSourceKind.TASK,
            source_reference="task",
        ),
        plan=_make_plan_output(),
        current_diff=UntrustedContent.from_text(
            "diff --git a/f b/f",
            source_kind=UntrustedSourceKind.DIFF,
            source_reference="diff",
        ),
    )


def _make_request(
    *,
    role: AgentRole = AgentRole.DEVELOPER,
    execution_id: UUID | None = None,
    run_id: UUID | None = None,
    task_id: UUID | None = None,
    parent_execution_id: UUID | None = None,
    provider: str = "google",
    model: str = "gemini-2.5-flash",
    instruction_version: str = "1",
    allowed_tools: tuple[ToolName, ...] | None = None,
) -> AgentRequest:
    instr = _instruction()
    if allowed_tools is None:
        if role == AgentRole.PLANNER:
            allowed_tools = (ToolName.REPOSITORY_LIST_FILES, ToolName.REPOSITORY_READ_FILE)
        elif role == AgentRole.DEVELOPER:
            allowed_tools = (ToolName.REPOSITORY_WRITE_FILE, ToolName.GIT_COMMIT)
        else:
            allowed_tools = (ToolName.REPOSITORY_READ_FILE, ToolName.VALIDATION_RESULTS_READ)

    if role == AgentRole.PLANNER:
        context = _make_planner_context()
    elif role == AgentRole.DEVELOPER:
        context = _make_developer_context()
    else:
        context = _make_reviewer_context()

    return AgentRequest(
        execution_id=execution_id or uuid4(),
        run_id=run_id or uuid4(),
        task_id=task_id or uuid4(),
        role=role,
        context=context,
        parent_execution_id=parent_execution_id,
        provider=provider,
        model=model,
        instruction_version=instruction_version,
        system_instruction=instr,
        instruction_digest=_instruction_digest(instr),
        allowed_tools=allowed_tools,
        budget=_make_budget(),
    )


def _make_usage(
    request: AgentRequest,
    *,
    provider: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_input_tokens: int = 0,
    duration_ms: int = 300,
    tool_call_count: int = 2,
    pricing_version: str | None = "fake-v1",
    currency: str | None = "USD",
    estimated_cost_minor: int | None = 5,
    unknown_price_reason: str | None = None,
    provider_request_id: str | None = "req-1",
    run_id: UUID | None = None,
    agent_execution_id: UUID | None = None,
) -> UsageRecord:
    return UsageRecord(
        provider=provider or request.provider,
        model=model or request.model,
        prompt_version=prompt_version or request.instruction_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        duration_ms=duration_ms,
        tool_call_count=tool_call_count,
        provider_request_id=provider_request_id,
        pricing_version=pricing_version,
        estimated_cost_minor=estimated_cost_minor,
        currency=currency,
        unknown_price_reason=unknown_price_reason,
        run_id=run_id if run_id is not None else request.run_id,
        agent_execution_id=agent_execution_id
        if agent_execution_id is not None
        else request.execution_id,
    )


def _make_result(
    request: AgentRequest,
    *,
    finish_status: AgentFinishStatus = AgentFinishStatus.SUCCEEDED,
    role: AgentRole | None = None,
    parent_execution_id: UUID | None = None,
    usage: UsageRecord | None = None,
    usage_attempts: tuple[UsageRecord, ...] | None = None,
    output: object = None,
    execution_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    instruction_digest: str | None = None,
    tool_call_count: int | None = None,
    duration_ms: int | None = None,
) -> AgentResult:
    effective_role = role or request.role
    effective_provider = provider or request.provider
    effective_model = model or request.model
    effective_usage = usage or _make_usage(
        request, provider=effective_provider, model=effective_model
    )
    effective_attempts = usage_attempts if usage_attempts is not None else (effective_usage,)
    effective_parent = (
        parent_execution_id if parent_execution_id is not None else request.parent_execution_id
    )

    if output is None and finish_status == AgentFinishStatus.SUCCEEDED:
        if effective_role == AgentRole.PLANNER:
            output = _make_plan_output()
        elif effective_role == AgentRole.DEVELOPER:
            output = _make_developer_output()
        else:
            output = _make_review_output()

    return AgentResult(
        execution_id=execution_id or request.execution_id,
        role=effective_role,
        finish_status=finish_status,
        output=output if finish_status == AgentFinishStatus.SUCCEEDED else None,
        parent_execution_id=effective_parent,
        provider=effective_provider,
        model=effective_model,
        instruction_digest=instruction_digest or request.instruction_digest,
        usage=effective_usage,
        usage_attempts=effective_attempts,
        tool_call_count=tool_call_count
        if tool_call_count is not None
        else effective_usage.tool_call_count,
        duration_ms=duration_ms if duration_ms is not None else effective_usage.duration_ms,
    )


# ===========================================================================
# validate_agent_result tests
# ===========================================================================


class TestValidateAgentResult:
    def test_developer_bound_valid_result_succeeds(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request)

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.SUCCEEDED
        assert usage == result.usage
        assert attempts == (result.usage,)
        assert reason == ""

    def test_developer_bound_valid_result_with_parent_succeeds(self) -> None:
        parent_id = uuid4()
        request = _make_request(role=AgentRole.DEVELOPER, parent_execution_id=parent_id)
        result = _make_result(request, parent_execution_id=parent_id)

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.SUCCEEDED
        assert usage == result.usage
        assert attempts == (result.usage,)
        assert reason == ""

    def test_developer_non_succeeded_status_preserved(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request, finish_status=AgentFinishStatus.BUDGET_EXCEEDED)

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.BUDGET_EXCEEDED
        assert usage == result.usage
        assert attempts == (result.usage,)
        assert reason == "budget_exceeded"

    def test_developer_rejected_when_expected_role_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request)

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.PLANNER
        )

        assert status is AgentFinishStatus.FAILED
        assert usage == result.usage
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_result_role_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request, role=AgentRole.PLANNER, output=_make_plan_output())

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert usage == result.usage
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_parent_execution_id_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER, parent_execution_id=uuid4())
        result = _make_result(request, parent_execution_id=uuid4())

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert usage == result.usage
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_execution_id_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request, execution_id=uuid4())

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert usage == result.usage
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_provider_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER, provider="google")
        usage = _make_usage(request, provider="openai")
        result = _make_result(request, provider="openai", usage=usage)

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_model_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER, model="gemini-2.5-flash")
        usage = _make_usage(request, model="gemini-2.5-pro")
        result = _make_result(request, model="gemini-2.5-pro", usage=usage)

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_instruction_digest_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        other_digest = hashlib.sha256(b"other instruction").hexdigest()
        result = _make_result(request, instruction_digest=other_digest)

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_developer_rejected_when_duration_or_tool_calls_mismatch(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)
        result = _make_result(request)
        object.__setattr__(result, "duration_ms", 9999)

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_non_agent_result_rejected_with_safe_usage(self) -> None:
        request = _make_request(role=AgentRole.DEVELOPER)

        status, usage, attempts, reason = validate_agent_result(
            "not-a-result", request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"
        assert usage.provider == request.provider
        assert usage.model == request.model
        assert usage.input_tokens == 0

    def test_fresh_reviewer_parent_disallowed(self) -> None:
        """Reviewer requests and results must be fresh and have no parent_execution_id."""
        parent_id = uuid4()

        with pytest.raises(
            ValueError, match="reviewer executions must be fresh and have no parent_execution_id"
        ):
            _make_request(role=AgentRole.REVIEWER, parent_execution_id=parent_id)

        valid_request = _make_request(role=AgentRole.REVIEWER)
        with pytest.raises(
            ValidationError, match="reviewer results must be fresh and have no parent_execution_id"
        ):
            AgentResult(
                execution_id=valid_request.execution_id,
                role=AgentRole.REVIEWER,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=_make_review_output(),
                parent_execution_id=parent_id,
                provider=valid_request.provider,
                model=valid_request.model,
                instruction_digest=valid_request.instruction_digest,
                usage=_make_usage(valid_request),
                tool_call_count=2,
                duration_ms=300,
            )

        fresh_result = _make_result(valid_request, parent_execution_id=None)
        status, usage, attempts, reason = validate_agent_result(
            fresh_result, valid_request, expected_role=AgentRole.REVIEWER
        )
        assert status is AgentFinishStatus.SUCCEEDED
        assert usage == fresh_result.usage
        assert attempts == (fresh_result.usage,)
        assert reason == ""

    def test_planner_result_valid(self) -> None:
        request = _make_request(role=AgentRole.PLANNER)
        result = _make_result(request)

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.PLANNER
        )

        assert status is AgentFinishStatus.SUCCEEDED
        assert usage == result.usage
        assert attempts == (result.usage,)
        assert reason == ""

    def test_planner_result_with_parent_disallowed(self) -> None:
        request = _make_request(role=AgentRole.PLANNER)
        result = _make_result(request, parent_execution_id=uuid4())

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.PLANNER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"

    def test_usage_attempts_totals_contract(self) -> None:
        """Usage attempts whose sums equal the aggregate are accepted."""
        request = _make_request(role=AgentRole.DEVELOPER)
        attempt1 = _make_usage(
            request,
            input_tokens=40,
            output_tokens=20,
            duration_ms=100,
            tool_call_count=1,
            estimated_cost_minor=2,
        )
        attempt2 = _make_usage(
            request,
            input_tokens=60,
            output_tokens=30,
            duration_ms=200,
            tool_call_count=1,
            estimated_cost_minor=3,
        )
        aggregate = _make_usage(
            request,
            input_tokens=100,
            output_tokens=50,
            duration_ms=300,
            tool_call_count=2,
            estimated_cost_minor=5,
        )
        result = _make_result(
            request,
            usage=aggregate,
            usage_attempts=(attempt1, attempt2),
            tool_call_count=2,
            duration_ms=300,
        )

        status, usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.SUCCEEDED
        assert usage == aggregate
        assert attempts == (attempt1, attempt2)
        assert reason == ""

    def test_usage_attempts_sum_mismatch_rejected(self) -> None:
        """Usage attempts whose sums do not equal the aggregate fail validation."""
        request = _make_request(role=AgentRole.DEVELOPER)
        attempt1 = _make_usage(
            request,
            input_tokens=40,
            output_tokens=20,
            duration_ms=100,
            tool_call_count=1,
        )
        result = _make_result(request)
        # Bypassing Pydantic to test validator defense against corrupted attempts
        object.__setattr__(result, "usage_attempts", (attempt1,))

        status, _usage, attempts, reason = validate_agent_result(
            result, request, expected_role=AgentRole.DEVELOPER
        )

        assert status is AgentFinishStatus.FAILED
        assert attempts == ()
        assert reason == "result_identity_mismatch"


# ===========================================================================
# safe_usage tests
# ===========================================================================


class TestSafeUsage:
    def test_preserves_valid_measured_usage_with_pricing(self) -> None:
        request = _make_request()
        valid = _make_usage(
            request,
            input_tokens=500,
            output_tokens=250,
            duration_ms=1500,
            tool_call_count=3,
            pricing_version="v1",
            currency="USD",
            estimated_cost_minor=12,
        )

        safe = safe_usage(valid, request)

        assert safe == valid
        assert safe.input_tokens == 500
        assert safe.output_tokens == 250
        assert safe.estimated_cost_minor == 12

    def test_preserves_measured_usage_when_pricing_unavailable(self) -> None:
        """Do not fabricate zero usage in place of measured valid usage."""
        request = _make_request()
        unpriced = _make_usage(
            request,
            input_tokens=450,
            output_tokens=150,
            cached_input_tokens=50,
            duration_ms=800,
            tool_call_count=2,
            pricing_version=None,
            currency=None,
            estimated_cost_minor=None,
        )

        safe = safe_usage(unpriced, request)

        assert safe.input_tokens == 450
        assert safe.output_tokens == 150
        assert safe.cached_input_tokens == 50
        assert safe.duration_ms == 800
        assert safe.tool_call_count == 2
        assert safe.pricing_version == "unavailable-v1"
        assert safe.currency == "USD"
        assert safe.unknown_price_reason == "gateway_usage_unavailable"

    def test_none_usage_returns_zeroed_safe_record(self) -> None:
        request = _make_request()
        safe = safe_usage(None, request)

        assert safe.provider == request.provider
        assert safe.model == request.model
        assert safe.prompt_version == request.instruction_version
        assert safe.input_tokens == 0
        assert safe.output_tokens == 0
        assert safe.pricing_version == "unavailable-v1"
        assert safe.currency == "USD"
        assert safe.unknown_price_reason == "gateway_usage_unavailable"

    def test_unbound_usage_returns_zeroed_safe_record(self) -> None:
        request = _make_request(provider="google")
        other_usage = _make_usage(request, provider="openai")

        safe = safe_usage(other_usage, request)

        assert safe.provider == request.provider
        assert safe.input_tokens == 0
        assert safe.pricing_version == "unavailable-v1"

    def test_unsafe_durable_metadata_falls_back(self) -> None:
        request = _make_request()
        unbound = _make_usage(request, run_id=uuid4())
        safe = safe_usage(unbound, request)
        assert safe.input_tokens == 0


# ===========================================================================
# safe_usage_attempts tests
# ===========================================================================


class TestSafeUsageAttempts:
    def test_none_aggregate_returns_empty(self) -> None:
        request = _make_request()
        attempt = _make_usage(request)
        assert safe_usage_attempts(None, (attempt,), request) == ()

    def test_unbound_aggregate_returns_empty(self) -> None:
        request = _make_request(provider="google")
        aggregate = _make_usage(request, provider="openai")
        attempt = _make_usage(request)
        assert safe_usage_attempts(aggregate, (attempt,), request) == ()

    def test_valid_attempts_returned(self) -> None:
        request = _make_request()
        u = _make_usage(request)
        assert safe_usage_attempts(u, (u,), request) == (u,)

    def test_unbound_attempt_returns_empty(self) -> None:
        request = _make_request(provider="google")
        aggregate = _make_usage(request)
        unbound_attempt = _make_usage(request, provider="openai")
        assert safe_usage_attempts(aggregate, (unbound_attempt,), request) == ()


# ===========================================================================
# usage_bound tests
# ===========================================================================


class TestUsageBound:
    def test_matching_record_is_bound(self) -> None:
        request = _make_request()
        usage = _make_usage(request)
        assert usage_bound(usage, request) is True

    def test_mismatched_provider_is_not_bound(self) -> None:
        request = _make_request(provider="google")
        usage = _make_usage(request, provider="anthropic")
        assert usage_bound(usage, request) is False

    def test_mismatched_model_is_not_bound(self) -> None:
        request = _make_request(model="gemini-2.5-flash")
        usage = _make_usage(request, model="gemini-2.5-pro")
        assert usage_bound(usage, request) is False

    def test_mismatched_run_id_is_not_bound(self) -> None:
        request = _make_request()
        usage = _make_usage(request, run_id=uuid4())
        assert usage_bound(usage, request) is False

    def test_non_usage_record_is_not_bound(self) -> None:
        request = _make_request()
        assert usage_bound(cast(UsageRecord, object()), request) is False


# ===========================================================================
# usage_attempts_bytes tests
# ===========================================================================


class TestUsageAttemptsBytes:
    def test_canonical_json_exact_bytes(self) -> None:
        request = _make_request()
        u = _make_usage(request, input_tokens=10, output_tokens=5, duration_ms=100)
        raw_bytes = usage_attempts_bytes((u,))

        parsed = json.loads(raw_bytes.decode("utf-8"))
        assert parsed["schema_version"] == 1
        assert len(parsed["attempts"]) == 1
        assert parsed["attempts"][0]["input_tokens"] == 10
        assert parsed["attempts"][0]["output_tokens"] == 5
        assert parsed["attempts"][0]["provider"] == request.provider
