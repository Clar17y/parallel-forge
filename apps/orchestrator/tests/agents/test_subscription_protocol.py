from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProtocolError, decode_final, freeze_context
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.run import RunState
from forge.domain.subscription import (
    AttemptIdentity,
    AuthMode,
    BillingMode,
    BrokerAuthorizationBinding,
    ExecutionEnvelope,
    HandoffStatus,
    LogicalTaskContract,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.domain.tool import ToolName


@pytest.mark.parametrize("kind", ["delegate", "wait", "handoff", "accept"])
def test_planning_primary_rejects_non_plan_decisions(kind: str) -> None:
    request = replace(_request(purpose=SpecialistPurpose.PRIMARY), run_state=RunState.PLANNING)
    with pytest.raises(ProtocolError, match="run phase"):
        decode_final({"kind": kind}, request)


def test_accept_preserves_known_child_target_and_defaults_to_primary() -> None:
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    child = replace(
        request.task,
        task_id=uuid4(),
        parent_task_id=request.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
    )
    request = replace(request, known_tasks=(child,))
    payload = {
        "kind": "accept",
        "candidate_commit": None,
        "candidate_tree_digest": "a" * 64,
        "evidence_receipt_ids": [str(uuid4())],
        "rationale": "Evidence-backed acceptance",
    }
    assert decode_final(payload, request).decision.task_id == request.task.task_id
    assert (
        decode_final(dict(payload, task_id=str(child.task_id)), request).decision.task_id
        == child.task_id
    )
    with pytest.raises(ProtocolError, match="unknown decision target"):
        decode_final(dict(payload, task_id=str(uuid4())), request)


def test_scope_response_requires_exact_request_attempt() -> None:
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    child = replace(
        request.task,
        task_id=uuid4(),
        parent_task_id=request.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
    )
    request = replace(request, known_tasks=(child,))
    payload = {
        "kind": "scope_response",
        "task_id": str(child.task_id),
        "granted_paths": ["src/shared"],
        "reason": "Required interface",
    }
    with pytest.raises(ProtocolError):
        decode_final(payload, request)
    identity = uuid4()
    decision = decode_final(dict(payload, request_attempt_id=str(identity)), request).decision
    assert decision.request_attempt_id == identity


def test_bound_scope_response_roundtrip_preserves_legacy_encoding() -> None:
    from forge.domain.subscription import (
        BoundScopeResponseDecision,
        ScopeResponseDecision,
        decode_subscription_record,
        encode_subscription_record,
    )

    legacy = ScopeResponseDecision(
        run_id=uuid4(), task_id=uuid4(), denied_paths=("src",), reason="Denied"
    )
    encoded = encode_subscription_record(legacy)
    assert "request_attempt_id" not in encoded["record"]["fields"]
    assert encode_subscription_record(decode_subscription_record(encoded)) == encoded
    bound = BoundScopeResponseDecision(
        run_id=legacy.run_id,
        task_id=legacy.task_id,
        request_attempt_id=uuid4(),
        denied_paths=("src",),
        reason="Denied",
    )
    assert decode_subscription_record(encode_subscription_record(bound)) == bound
    with pytest.raises(ValueError, match="grant and deny"):
        replace(bound, granted_paths=("SRC",))


def test_reassignment_requires_observed_version_and_stopped_attempt() -> None:
    from forge.domain.subscription import BoundReassignDecision
    from pydantic import TypeAdapter

    request = _request(purpose=SpecialistPurpose.PRIMARY)
    child = replace(
        request.task,
        task_id=uuid4(),
        parent_task_id=request.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
    )
    source = uuid4()
    request = replace(
        request,
        known_tasks=(child,),
        untrusted_context={
            "task_outcomes": [{"task_id": str(child.task_id), "version": 7}],
        },
    )
    payload = {
        "kind": "reassign",
        "task_id": str(child.task_id),
        "source_attempt_id": str(source),
        "expected_task_version": 7,
        "new_route": TypeAdapter(RouteSpec).dump_python(child.route.effective, mode="json"),
        "reason": "Approved reassignment",
        "preserve_partial_work": True,
    }
    decision = decode_final(payload, request).decision
    assert isinstance(decision, BoundReassignDecision) and decision.source_attempt_id == source
    for field in ("source_attempt_id", "expected_task_version"):
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ProtocolError):
            decode_final(incomplete, request)
    with pytest.raises(ProtocolError, match="observed task version"):
        decode_final(dict(payload, expected_task_version=8), request)
    with pytest.raises(ProtocolError, match="preserve partial"):
        decode_final(dict(payload, preserve_partial_work=False), request)


def test_bound_reassignment_preserves_historical_record_encoding() -> None:
    from forge.domain.subscription import (
        BoundReassignDecision,
        ReassignDecision,
        decode_subscription_record,
        encode_subscription_record,
    )

    legacy = ReassignDecision(
        run_id=uuid4(),
        task_id=uuid4(),
        new_route=_request().task.route.effective,
        reason="Legacy",
    )
    encoded = encode_subscription_record(legacy)
    assert encode_subscription_record(decode_subscription_record(encoded)) == encoded
    bound = BoundReassignDecision(
        run_id=legacy.run_id,
        task_id=legacy.task_id,
        new_route=legacy.new_route,
        reason="Bound",
        source_attempt_id=uuid4(),
        expected_task_version=3,
    )
    assert decode_subscription_record(encode_subscription_record(bound)) == bound
    for version in (-1, True, 1.0):
        with pytest.raises(ValueError):
            replace(bound, expected_task_version=version)


@pytest.mark.parametrize("state", [state for state in RunState if state is not RunState.PLANNING])
def test_non_planning_primary_rejects_plan_decision(state: RunState) -> None:
    request = replace(_request(purpose=SpecialistPurpose.PRIMARY), run_state=state)
    with pytest.raises(ProtocolError, match="run phase"):
        decode_final({"kind": "plan", "plan": {}}, request)


def test_planning_primary_accepts_valid_plan() -> None:
    from forge.domain.plan import ScopedPlanOutput

    request = replace(_request(purpose=SpecialistPurpose.PRIMARY), run_state=RunState.PLANNING)
    plan = ScopedPlanOutput(
        owned_paths=("src",),
        summary="Update the requested component",
        assumptions=(),
        affected_components=("src",),
        steps=("Implement and test",),
        required_checks=("unit",),
        risks=("Behavior regression",),
        security_considerations=(),
        dependency_changes=(),
    )
    assert (
        decode_final({"kind": "plan", "plan": plan.model_dump(mode="json")}, request).decision
        == plan
    )


def test_new_planning_decision_requires_explicit_scope() -> None:
    from forge.domain.plan import PlanOutput

    plan = PlanOutput(
        summary="Change",
        assumptions=(),
        affected_components=("src",),
        steps=("Implement",),
        required_checks=("unit",),
        risks=("Regression",),
        security_considerations=(),
        dependency_changes=(),
    )
    request = replace(_request(purpose=SpecialistPurpose.PRIMARY), run_state=RunState.PLANNING)
    with pytest.raises(ProtocolError):
        decode_final({"kind": "plan", "plan": plan.model_dump(mode="json")}, request)


def test_request_rejects_untyped_phase() -> None:
    with pytest.raises(TypeError, match="RunState"):
        replace(_request(), run_state="PLANNING")


def test_phase_specific_schema_uses_trusted_request_state() -> None:
    from forge.agents.subscription_protocol import output_schema

    request = replace(
        _request(purpose=SpecialistPurpose.PRIMARY),
        run_state=RunState.PLANNING,
        untrusted_context={"run_state": "IMPLEMENTING"},
    )
    choices = output_schema(request)["properties"]["decision"]["anyOf"]
    assert [choice["properties"]["kind"]["const"] for choice in choices] == ["plan"]
    assert "owned_paths" in choices[0]["properties"]["plan"]["required"]
    request = replace(request, run_state=RunState.IMPLEMENTING)
    choices = output_schema(request)["properties"]["decision"]["anyOf"]
    kinds = {choice["properties"]["kind"]["const"] for choice in choices}
    assert "plan" not in kinds
    assert "delegate" in kinds


def _route(model: str) -> RouteSpec:
    return RouteSpec(
        provider="openai",
        client="codex_app_server",
        model=model,
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


def _request(
    *,
    tools: frozenset[ToolName] = frozenset(),
    purpose: SpecialistPurpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION,
) -> SubscriptionInvocationRequest:
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    primary_route = RouteBinding(
        requested=_route("gpt-5.6"), effective=_route("gpt-5.6"), is_primary=True
    )
    worker_route = RouteBinding(requested=_route("gpt-5.6-luna"), effective=_route("gpt-5.6-luna"))
    selected = primary_route if purpose is SpecialistPurpose.PRIMARY else worker_route
    task = LogicalTaskContract(
        run_id=run_id,
        task_id=task_id,
        purpose=purpose,
        route=selected,
        budget=TaskBudget(max_duration_seconds=60, max_tool_calls=4, max_named_checks=2),
        owned_paths=("src",) if purpose is SpecialistPurpose.PRIMARY else (),
    )
    attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=purpose,
        policy_version=7,
        permitted_tools=tools,
        broker_token="opaque",
    )
    envelope = ExecutionEnvelope(
        run_id=run_id,
        profile_id=uuid4(),
        profile_version=3,
        safety_policy_version=7,
        routes=(
            (SpecialistPurpose.PRIMARY, primary_route),
            (SpecialistPurpose.ROUTINE_IMPLEMENTATION, worker_route),
        ),
    )
    return SubscriptionInvocationRequest(
        task=task,
        attempt=attempt,
        authorization=auth,
        envelope=envelope,
        prompt_version="v1",
        trusted_system_prompt="Return structured JSON only.",
        untrusted_context={"issue": {"title": "untrusted"}},
    )


def test_request_deep_freezes_context_and_validates_frozen_envelope() -> None:
    request = _request()
    assert isinstance(request.untrusted_context, MappingProxyType)
    assert isinstance(request.untrusted_context["issue"], MappingProxyType)
    with pytest.raises(TypeError):
        request.untrusted_context["new"] = "value"  # type: ignore[index]
    with pytest.raises(ValueError, match="envelope|route"):
        replace(request, envelope=replace(request.envelope, run_id=uuid4()))
    with pytest.raises(ValueError, match="attempt identity"):
        replace(
            request,
            attempt=AttemptIdentity(
                run_id=uuid4(), task_id=request.task.task_id, attempt_id=uuid4()
            ),
        )


@pytest.mark.parametrize(
    "context",
    [
        {"bad": float("nan")},
        {"bad": object()},
        {"bad": {1: "non-string key"}},
        {"too_long": "x" * 1_048_577},
    ],
)
def test_context_rejects_malformed_or_unbounded_values(context: dict[str, object]) -> None:
    with pytest.raises((ProtocolError, TypeError, ValueError)):
        freeze_context(context)


def test_context_rejects_excessive_nesting() -> None:
    nested: object = "over"
    for _ in range(26):
        nested = {"child": nested}
    with pytest.raises((ProtocolError, TypeError, ValueError)):
        freeze_context({"too_deep": nested})


def test_completed_handoff_decodes_all_candidate_and_check_evidence() -> None:
    request = _request()
    result = decode_final(
        {
            "kind": "handoff",
            "run_id": str(request.attempt.run_id),
            "task_id": str(request.attempt.task_id),
            "attempt_id": str(request.attempt.attempt_id),
            "status": "completed",
            "summary": "implemented and checked",
            "candidate_commit": "b" * 40,
            "candidate_tree_digest": "a" * 64,
            "changed_paths": ["src/a.py"],
            "check_results": [
                {
                    "command_name": "unit",
                    "exit_code": 0,
                    "passed": True,
                    "output_digest": "c" * 64,
                    "duration_ms": 12,
                    "receipt_id": "receipt-check",
                }
            ],
            "evidence_receipt_ids": ["receipt-check"],
            "residual_concerns": ["live capability proof remains"],
        },
        request,
    )
    handoff = result.decision
    assert result.attempt == request.attempt
    assert handoff is not None and handoff.status is HandoffStatus.COMPLETED
    assert handoff.changed_paths == ("src/a.py",)
    assert handoff.check_results[0].command_name == "unit"
    assert handoff.evidence_receipt_ids == ("receipt-check",)


def test_completed_handoff_requires_candidate_and_evidence() -> None:
    with pytest.raises(ProtocolError, match="invalid structured decision"):
        decode_final(
            {"kind": "handoff", "status": "completed", "summary": "missing proof"}, _request()
        )


@pytest.mark.parametrize("kind", ["accept", "delegate", "plan"])
def test_worker_cannot_emit_primary_only_decisions(kind: str) -> None:
    payload: dict[str, object] = {"kind": kind}
    if kind == "accept":
        payload.update(
            candidate_tree_digest="a" * 64, evidence_receipt_ids=["r"], rationale="looks good"
        )
    elif kind == "delegate":
        payload.update(rationale="split", children=[])
    else:
        payload["plan"] = {}
    with pytest.raises(ProtocolError, match="primary"):
        decode_final(payload, _request())


def test_primary_child_uses_frozen_envelope_route_instead_of_primary_route() -> None:
    primary = _request(purpose=SpecialistPurpose.PRIMARY)
    child_id = uuid4()
    result = decode_final(
        {
            "kind": "delegate",
            "rationale": "split bounded work",
            "children": [
                {
                    "task_id": str(child_id),
                    "purpose": "routine_implementation",
                    "owned_paths": ["src/a.py"],
                    "budget": {
                        "max_duration_seconds": 30,
                        "max_tool_calls": 2,
                        "max_named_checks": 1,
                        "max_provider_attempts": 1,
                        "max_repairs": 1,
                    },
                    "max_repairs": 1,
                }
            ],
        },
        primary,
    )
    child = result.decision.child_tasks[0]  # type: ignore[union-attr]
    assert child.route == primary.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION)
    assert child.route != primary.task.route
    assert child.budget.max_duration_seconds == 30


@pytest.mark.parametrize("field", ["run_id", "task_id", "attempt_id"])
def test_result_rejects_foreign_handoff_lineage(field: str) -> None:
    from forge.application.ports.subscription_gateway import SubscriptionInvocationResult

    request = _request()
    result = decode_final({"kind": "handoff", "status": "blocked"}, request)
    foreign = replace(result.decision, **{field: uuid4()})
    with pytest.raises(ValueError, match="identity"):
        SubscriptionInvocationResult(attempt=request.attempt, decision=foreign)


def test_unknown_broker_status_is_not_reported_as_success() -> None:
    from forge.agents.subscription_protocol import ProviderToolCall, tool_result_frame

    call = ProviderToolCall(
        call_key="call",
        thread_id="thread",
        turn_id="turn",
        name="repository.read_file",
        arguments={},
    )
    assert tool_result_frame(call, {}, 9)["result"]["success"] is False


@pytest.mark.parametrize(
    "purpose", [SpecialistPurpose.PRIMARY, SpecialistPurpose.ROUTINE_IMPLEMENTATION]
)
def test_output_schema_has_closed_required_objects_for_strict_provider(purpose) -> None:
    from forge.agents.subscription_protocol import output_schema

    schema = output_schema(_request(purpose=purpose))
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"decision"}
    assert "anyOf" in schema["properties"]["decision"]

    def check(value):
        if isinstance(value, dict):
            assert "oneOf" not in value and "default" not in value
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(schema)


def test_record_decoder_rejects_unknown_provider_key():
    with pytest.raises(ProtocolError):
        decode_final(
            {"kind": "handoff", "status": "blocked", "unknown_provider_key": True}, _request()
        )


@pytest.mark.parametrize("target", ["unknown", "self", "nonchild"])
def test_wait_decoder_rejects_targets_outside_direct_child_context(target):
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    child = replace(
        request.task,
        task_id=uuid4(),
        parent_task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
    )
    request = replace(request, known_tasks=(child,))
    target_id = (
        request.task.task_id
        if target == "self"
        else child.task_id
        if target == "nonchild"
        else uuid4()
    )
    with pytest.raises(ProtocolError):
        decode_final(
            {"kind": "wait", "waiting_on_task_ids": [str(target_id)], "reason": "Wait"}, request
        )


def test_wait_decoder_accepts_known_direct_child():
    request = _request(purpose=SpecialistPurpose.PRIMARY)
    child = replace(
        request.task,
        task_id=uuid4(),
        parent_task_id=request.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
    )
    request = replace(request, known_tasks=(child,))
    result = decode_final(
        {"kind": "wait", "waiting_on_task_ids": [str(child.task_id)], "reason": "Wait"}, request
    )
    assert result.decision.waiting_on_task_ids == (child.task_id,)
