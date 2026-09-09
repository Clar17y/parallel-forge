"""Tests for deterministic scripted fake agent gateway behavior, scenarios, and budgets."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from forge.agents.fake_gateway import (
    FakeAgentGateway,
    FakeAgentScenario,
    FakeAgentStep,
    FakeRequestInvalid,
    FakeScriptExhausted,
    FakeScriptInvalid,
)
from forge.application.ports.agents import AgentGateway
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    DeveloperInput,
    DeveloperOutput,
    PlannerInput,
    PolicySummary,
    ReviewDecision,
    ReviewerInput,
    ReviewOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.plan import PlanOutput
from forge.domain.policy import RunnerMode
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.tool import ToolName

# ===========================================================================
# Reusable Builders & Fixtures
# ===========================================================================


def _build_plan_output() -> PlanOutput:
    return PlanOutput(
        summary="Plan summary",
        assumptions=("Assumption 1",),
        affected_components=("orchestrator",),
        steps=("Step 1", "Step 2"),
        required_checks=("pytest -q",),
        risks=("Risk 1",),
        security_considerations=("Sec 1",),
        dependency_changes=(),
    )


def _build_developer_output() -> DeveloperOutput:
    return DeveloperOutput(
        summary="Developer summary",
        changed_paths=("src/forge/domain/agent.py",),
        tests_added_or_changed=("tests/domain/test_agent.py",),
        named_checks_run=("unit",),
        local_commit_sha="a" * 40,
        diff_digest="b" * 64,
        unresolved_concerns=(),
        plan_deviations=(),
    )


def _build_review_output(
    decision: ReviewDecision = ReviewDecision.APPROVE,
    findings: tuple[ReviewFinding, ...] = (),
    missing_evidence: tuple[str, ...] = (),
) -> ReviewOutput:
    return ReviewOutput(
        decision=decision,
        findings=findings,
        tested_claims=("All checks pass",),
        missing_evidence=missing_evidence,
        summary="Review summary",
    )


def _build_request(
    role: AgentRole = AgentRole.PLANNER,
    *,
    budget: AgentBudget | None = None,
    provider: str = "fake-provider",
    model: str = "fake-model",
    instruction_version: str = "1",
    parent_execution_id: Any = None,
    context: PlannerInput | DeveloperInput | ReviewerInput | None = None,
) -> AgentRequest:
    sys_instruction = "System instruction"
    digest = hashlib.sha256(sys_instruction.encode("utf-8")).hexdigest()
    if context is None:
        if role == AgentRole.PLANNER:
            context = PlannerInput(
                original_task=UntrustedContent.from_text(
                    "Plan task",
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference="task-ref",
                ),
                base_commit="a" * 40,
                repository_tree=UntrustedContent.from_text(
                    "tree",
                    source_kind=UntrustedSourceKind.REPOSITORY_TREE,
                    source_reference="tree-ref",
                ),
                relevant_instructions=(),
                policy_summary=PolicySummary(
                    policy_id=uuid4(),
                    policy_version=1,
                    runner_mode=RunnerMode.DOCKER,
                    trusted_project=False,
                    required_checks=(),
                    allowed_merge_methods=("squash",),
                    publication_blocking_severities=(FindingSeverity.BLOCKER,),
                    merge_blocking_severities=(FindingSeverity.BLOCKER,),
                ),
            )
        elif role == AgentRole.DEVELOPER:
            context = DeveloperInput(
                original_task=UntrustedContent.from_text(
                    "Develop task",
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference="task-ref",
                ),
                plan=_build_plan_output(),
                worktree_id="forge-wt-01",
                base_commit="a" * 40,
                remediation_findings=(),
                relevant_instructions=(),
            )
        else:
            context = ReviewerInput(
                original_task=UntrustedContent.from_text(
                    "Review task",
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference="task-ref",
                ),
                plan=_build_plan_output(),
                current_diff=UntrustedContent.from_text(
                    "diff",
                    source_kind=UntrustedSourceKind.DIFF,
                    source_reference="diff-ref",
                ),
                check_evidence=(),
                relevant_instructions=(),
            )

    tools = (
        (ToolName.REPOSITORY_LIST_FILES,)
        if role != AgentRole.DEVELOPER
        else (ToolName.REPOSITORY_WRITE_FILE,)
    )

    return AgentRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        role=role,
        context=context,
        parent_execution_id=parent_execution_id,
        provider=provider,
        model=model,
        instruction_version=instruction_version,
        system_instruction=sys_instruction,
        instruction_digest=digest,
        allowed_tools=tools,
        budget=budget or AgentBudget(),
    )


# ===========================================================================
# Interface & Initialization Tests
# ===========================================================================


class TestFakeGatewayInterface:
    """Test FakeAgentGateway protocol conformance and constructor validation."""

    def test_satisfies_agent_gateway_protocol(self) -> None:
        gateway = FakeAgentGateway(
            {AgentRole.PLANNER: [FakeAgentStep.success(_build_plan_output())]}
        )
        assert isinstance(gateway, AgentGateway)

    def test_rejects_empty_scripts(self) -> None:
        with pytest.raises(ValueError, match="scripts mapping must not be empty"):
            FakeAgentGateway({})

    def test_rejects_non_mapping_scripts(self) -> None:
        with pytest.raises(TypeError, match="scripts must be a mapping"):
            FakeAgentGateway([FakeAgentStep.success(_build_plan_output())])  # type: ignore[arg-type]

    def test_rejects_non_role_key(self) -> None:
        with pytest.raises(TypeError, match="script role keys must be AgentRole members"):
            FakeAgentGateway({"planner": [FakeAgentStep.success(_build_plan_output())]})  # type: ignore[dict-item]

    def test_rejects_empty_steps_sequence_for_role(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            FakeAgentGateway({AgentRole.PLANNER: ()})

    def test_rejects_incompatible_step_output_for_role(self) -> None:
        # Planner configured with DeveloperOutput
        with pytest.raises(FakeScriptInvalid):
            FakeAgentGateway(
                {AgentRole.PLANNER: [FakeAgentStep.success(_build_developer_output())]}
            )

        # Developer configured with ReviewOutput
        with pytest.raises(FakeScriptInvalid):
            FakeAgentGateway({AgentRole.DEVELOPER: [FakeAgentStep.success(_build_review_output())]})

        # Reviewer configured with PlanOutput
        with pytest.raises(FakeScriptInvalid):
            FakeAgentGateway({AgentRole.REVIEWER: [FakeAgentStep.success(_build_plan_output())]})


# ===========================================================================
# Deterministic Ordering & Request Recording Tests
# ===========================================================================


class TestDeterministicExecutionAndRecording:
    """Test deterministic step consumption, request recording, and exhaustion."""

    @pytest.mark.asyncio
    async def test_deterministic_per_role_script_ordering(self) -> None:
        plan_1 = _build_plan_output()
        plan_2 = PlanOutput(
            summary="Second plan",
            assumptions=("Assumption 2",),
            affected_components=("worker",),
            steps=("Step A",),
            required_checks=("check 1",),
            risks=("Risk 2",),
            security_considerations=("Sec 2",),
            dependency_changes=(),
        )

        gateway = FakeAgentGateway(
            {
                AgentRole.PLANNER: [
                    FakeAgentStep.invalid_schema(input_tokens=100),
                    FakeAgentStep.success(plan_1, input_tokens=200),
                    FakeAgentStep.success(plan_2, input_tokens=300),
                ]
            }
        )

        assert gateway.invocation_count(AgentRole.PLANNER) == 0

        # Invocations 1: invalid_schema
        req1 = _build_request(AgentRole.PLANNER)
        res1 = await gateway.execute(req1)
        assert res1.finish_status == AgentFinishStatus.INVALID_OUTPUT
        assert res1.output is None
        assert res1.usage.input_tokens == 100
        assert res1.usage.provider_request_id == "planner-1"
        assert gateway.invocation_count(AgentRole.PLANNER) == 1

        # Invocations 2: success plan_1
        req2 = _build_request(AgentRole.PLANNER)
        res2 = await gateway.execute(req2)
        assert res2.finish_status == AgentFinishStatus.SUCCEEDED
        assert res2.output == plan_1
        assert res2.usage.input_tokens == 200
        assert res2.usage.provider_request_id == "planner-2"
        assert gateway.invocation_count(AgentRole.PLANNER) == 2

        # Invocations 3: success plan_2
        req3 = _build_request(AgentRole.PLANNER)
        res3 = await gateway.execute(req3)
        assert res3.finish_status == AgentFinishStatus.SUCCEEDED
        assert res3.output == plan_2
        assert res3.usage.input_tokens == 300
        assert res3.usage.provider_request_id == "planner-3"
        assert gateway.invocation_count(AgentRole.PLANNER) == 3

    @pytest.mark.asyncio
    async def test_requests_recorded_in_exact_admission_order(self) -> None:
        gateway = FakeAgentGateway(
            {
                AgentRole.PLANNER: [FakeAgentStep.success(_build_plan_output())],
                AgentRole.DEVELOPER: [FakeAgentStep.success(_build_developer_output())],
            }
        )

        req_p = _build_request(AgentRole.PLANNER)
        req_d = _build_request(AgentRole.DEVELOPER)

        await gateway.execute(req_p)
        await gateway.execute(req_d)

        assert len(gateway.requests) == 2
        assert gateway.requests[0] == req_p
        assert gateway.requests[1] == req_d

    @pytest.mark.asyncio
    async def test_script_exhaustion_raises(self) -> None:
        gateway = FakeAgentGateway(
            {AgentRole.PLANNER: [FakeAgentStep.success(_build_plan_output())]}
        )

        req = _build_request(AgentRole.PLANNER)
        await gateway.execute(req)

        # Second invocation exceeds scripted steps
        with pytest.raises(FakeScriptExhausted):
            await gateway.execute(req)

    @pytest.mark.asyncio
    async def test_unconfigured_role_raises_exhausted(self) -> None:
        gateway = FakeAgentGateway(
            {AgentRole.PLANNER: [FakeAgentStep.success(_build_plan_output())]}
        )
        req_dev = _build_request(AgentRole.DEVELOPER)
        with pytest.raises(FakeScriptExhausted):
            await gateway.execute(req_dev)

    @pytest.mark.asyncio
    async def test_execute_rejects_untrimmed_request_strings(self) -> None:
        gateway = FakeAgentGateway(
            {AgentRole.PLANNER: [FakeAgentStep.success(_build_plan_output())]}
        )
        req = _build_request(AgentRole.PLANNER, provider=" fake-provider ")
        with pytest.raises(FakeRequestInvalid):
            await gateway.execute(req)


# ===========================================================================
# Scenario Simulation Tests
# ===========================================================================


class TestFakeScenarios:
    """Test every FakeAgentScenario returns its corresponding finish status."""

    @pytest.mark.parametrize(
        ("scenario", "expected_status"),
        [
            (FakeAgentScenario.INVALID_SCHEMA, AgentFinishStatus.INVALID_OUTPUT),
            (FakeAgentScenario.VALIDATION_FAILURE, AgentFinishStatus.INVALID_OUTPUT),
            (FakeAgentScenario.TIMEOUT, AgentFinishStatus.TIMED_OUT),
            (FakeAgentScenario.TOOL_DENIED, AgentFinishStatus.TOOL_DENIED),
            (FakeAgentScenario.FAILED, AgentFinishStatus.FAILED),
            (FakeAgentScenario.CANCELLED, AgentFinishStatus.CANCELLED),
        ],
    )
    @pytest.mark.asyncio
    async def test_error_and_failure_scenarios(
        self, scenario: FakeAgentScenario, expected_status: AgentFinishStatus
    ) -> None:
        step = FakeAgentStep(
            scenario=scenario,
            output=None,
            input_tokens=120,
            output_tokens=30,
            tool_calls=1,
            duration_ms=500,
            cost_minor=2,
        )
        gateway = FakeAgentGateway({AgentRole.PLANNER: [step]})
        req = _build_request(AgentRole.PLANNER)
        result = await gateway.execute(req)

        assert result.finish_status == expected_status
        assert result.output is None
        assert result.tool_call_count == 1
        assert result.duration_ms == 500
        assert result.usage.input_tokens == 120
        assert result.usage.output_tokens == 30
        assert result.usage.estimated_cost_minor == 2


# ===========================================================================
# Budget Enforcement & Boundaries Tests
# ===========================================================================


class TestBudgetBoundaries:
    """Test exact boundary conditions for every budget dimension."""

    @pytest.mark.asyncio
    async def test_input_token_boundary(self) -> None:
        budget = AgentBudget(max_input_tokens=1000)

        # Equal to limit: allowed
        step_ok = FakeAgentStep.success(_build_plan_output(), input_tokens=1000)
        # Exceeds limit by 1: budget_exceeded
        step_exceeded = FakeAgentStep.success(_build_plan_output(), input_tokens=1001)

        gateway = FakeAgentGateway({AgentRole.PLANNER: [step_ok, step_exceeded]})

        res_ok = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_ok.finish_status == AgentFinishStatus.SUCCEEDED
        assert res_ok.output is not None

        res_exceeded = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_exceeded.finish_status == AgentFinishStatus.BUDGET_EXCEEDED
        assert res_exceeded.output is None

    @pytest.mark.asyncio
    async def test_output_token_boundary(self) -> None:
        budget = AgentBudget(max_output_tokens=500)

        step_ok = FakeAgentStep.success(_build_plan_output(), output_tokens=500)
        step_exceeded = FakeAgentStep.success(_build_plan_output(), output_tokens=501)

        gateway = FakeAgentGateway({AgentRole.PLANNER: [step_ok, step_exceeded]})

        res_ok = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_ok.finish_status == AgentFinishStatus.SUCCEEDED

        res_exceeded = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_exceeded.finish_status == AgentFinishStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_tool_calls_boundary(self) -> None:
        budget = AgentBudget(max_tool_calls=10)

        step_ok = FakeAgentStep.success(_build_plan_output(), tool_calls=10)
        step_exceeded = FakeAgentStep.success(_build_plan_output(), tool_calls=11)

        gateway = FakeAgentGateway({AgentRole.PLANNER: [step_ok, step_exceeded]})

        res_ok = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_ok.finish_status == AgentFinishStatus.SUCCEEDED

        res_exceeded = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_exceeded.finish_status == AgentFinishStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_duration_boundary(self) -> None:
        budget = AgentBudget(max_duration_seconds=5)  # 5000 ms

        step_ok = FakeAgentStep.success(_build_plan_output(), duration_ms=5000)
        step_exceeded = FakeAgentStep.success(_build_plan_output(), duration_ms=5001)

        gateway = FakeAgentGateway({AgentRole.PLANNER: [step_ok, step_exceeded]})

        res_ok = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_ok.finish_status == AgentFinishStatus.SUCCEEDED

        res_exceeded = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_exceeded.finish_status == AgentFinishStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_cost_minor_boundary(self) -> None:
        budget = AgentBudget(max_cost_minor=200)

        step_ok = FakeAgentStep.success(_build_plan_output(), cost_minor=200)
        step_exceeded = FakeAgentStep.success(_build_plan_output(), cost_minor=201)

        gateway = FakeAgentGateway({AgentRole.PLANNER: [step_ok, step_exceeded]})

        res_ok = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_ok.finish_status == AgentFinishStatus.SUCCEEDED

        res_exceeded = await gateway.execute(_build_request(AgentRole.PLANNER, budget=budget))
        assert res_exceeded.finish_status == AgentFinishStatus.BUDGET_EXCEEDED


# ===========================================================================
# Multi-Turn Review Findings & Remediation Workflow
# ===========================================================================


class TestReviewAndRemediationWorkflow:
    """Simulate a complete multi-step developer implementation, review findings, and remediation."""

    @pytest.mark.asyncio
    async def test_developer_and_reviewer_remediation_cycle(self) -> None:
        finding = ReviewFinding(
            finding_id="f-1",
            severity=FindingSeverity.MAJOR,
            path="src/forge/domain/agent.py",
            start_line=25,
            summary="Missing validation",
            evidence="No null check",
            proposed_resolution="Add null check",
            resolved_at=None,
        )

        dev_step_1 = FakeAgentStep.success(_build_developer_output())
        rev_step_1 = FakeAgentStep.success(
            _build_review_output(
                decision=ReviewDecision.REQUEST_CHANGES,
                findings=(finding,),
            )
        )

        dev_step_2 = FakeAgentStep.success(
            DeveloperOutput(
                summary="Remediated finding f-1 with null check",
                changed_paths=("src/forge/domain/agent.py",),
                tests_added_or_changed=("tests/domain/test_agent.py",),
                named_checks_run=("unit", "lint"),
                local_commit_sha="2" * 40,
                diff_digest="3" * 64,
                unresolved_concerns=(),
                plan_deviations=(),
            )
        )

        resolved_finding = ReviewFinding(
            finding_id="f-1",
            severity=FindingSeverity.MAJOR,
            path="src/forge/domain/agent.py",
            start_line=25,
            summary="Missing validation",
            evidence="No null check",
            proposed_resolution="Add null check",
            resolved_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        )

        rev_step_2 = FakeAgentStep.success(
            _build_review_output(
                decision=ReviewDecision.APPROVE,
                findings=(resolved_finding,),
            )
        )

        gateway = FakeAgentGateway(
            {
                AgentRole.DEVELOPER: [dev_step_1, dev_step_2],
                AgentRole.REVIEWER: [rev_step_1, rev_step_2],
            }
        )

        # 1. Developer implements initial change
        req_dev_1 = _build_request(AgentRole.DEVELOPER)
        res_dev_1 = await gateway.execute(req_dev_1)
        assert res_dev_1.finish_status == AgentFinishStatus.SUCCEEDED
        assert isinstance(res_dev_1.output, DeveloperOutput)

        # 2. Reviewer inspects diff and requests changes
        req_rev_1 = _build_request(AgentRole.REVIEWER)
        res_rev_1 = await gateway.execute(req_rev_1)
        assert res_rev_1.finish_status == AgentFinishStatus.SUCCEEDED
        assert isinstance(res_rev_1.output, ReviewOutput)
        assert res_rev_1.output.decision == ReviewDecision.REQUEST_CHANGES

        # 3. Developer remediates findings
        dev_ctx_2 = DeveloperInput(
            original_task=req_dev_1.context.original_task,
            plan=_build_plan_output(),
            worktree_id="forge-wt-01",
            base_commit="a" * 40,
            remediation_findings=res_rev_1.output.findings,
            relevant_instructions=(),
        )
        req_dev_2 = _build_request(AgentRole.DEVELOPER, context=dev_ctx_2)
        res_dev_2 = await gateway.execute(req_dev_2)
        assert res_dev_2.finish_status == AgentFinishStatus.SUCCEEDED
        assert "Remediated finding f-1" in res_dev_2.output.summary  # type: ignore[union-attr]

        # 4. Reviewer approves
        req_rev_2 = _build_request(AgentRole.REVIEWER)
        res_rev_2 = await gateway.execute(req_rev_2)
        assert res_rev_2.finish_status == AgentFinishStatus.SUCCEEDED
        assert res_rev_2.output.decision == ReviewDecision.APPROVE  # type: ignore[union-attr]

        # Total 4 requests recorded
        assert len(gateway.requests) == 4
        assert gateway.invocation_count(AgentRole.DEVELOPER) == 2
        assert gateway.invocation_count(AgentRole.REVIEWER) == 2


# ===========================================================================
# Concurrency & Admission Tests
# ===========================================================================


class TestGatewayConcurrency:
    """Test thread/coroutine safety under concurrent invocations."""

    @pytest.mark.asyncio
    async def test_concurrent_executions_are_safely_admitted(self) -> None:
        steps = [FakeAgentStep.success(_build_plan_output(), input_tokens=i) for i in range(1, 21)]
        gateway = FakeAgentGateway({AgentRole.PLANNER: steps})

        requests = [_build_request(AgentRole.PLANNER) for _ in range(20)]

        results = await asyncio.gather(*[gateway.execute(r) for r in requests])

        assert len(results) == 20
        assert gateway.invocation_count(AgentRole.PLANNER) == 20
        assert len(gateway.requests) == 20
        # All requests admitted
        assert {r.execution_id for r in gateway.requests} == {req.execution_id for req in requests}
