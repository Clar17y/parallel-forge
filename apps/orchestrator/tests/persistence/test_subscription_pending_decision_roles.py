"""A stopped decision retains the source-role restrictions of live dispatch."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.domain.plan import PlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptDecision,
    AttemptTelemetry,
    BoundScopeResponseDecision,
    HandoffStatus,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
    encode_subscription_record,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.repositories.subscription_resumption import _pending_decision_is_admissible

from apps.orchestrator.tests.agents.test_subscription_protocol import _request


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize(
    "kind", ["plan", "wait", "accept", "scope_response", "handoff", "scope_request"]
)
def test_pending_decisions_keep_dispatcher_role_boundaries(primary, kind):
    request = _request(
        purpose=SpecialistPurpose.PRIMARY if primary else SpecialistPurpose.ROUTINE_IMPLEMENTATION
    )
    task = replace(request.task, parent_task_id=None if primary else uuid4())
    fields = {"run_id": task.run_id, "task_id": task.task_id}
    if kind == "plan":
        decision = PlanOutput(
            summary="Update the assigned component",
            assumptions=(),
            affected_components=("src",),
            steps=("Implement and test",),
            required_checks=("unit",),
            risks=("Regression",),
            security_considerations=(),
            dependency_changes=(),
        )
        encoded = {"type": "PlanOutput", "value": decision.model_dump(mode="json")}
    else:
        if kind == "wait":
            decision = WaitDecision(
                **fields, waiting_on_task_ids=(uuid4(),), reason="Wait for child"
            )
        elif kind == "accept":
            decision = AcceptDecision(
                **fields,
                candidate_commit=None,
                candidate_tree_digest="a" * 64,
                evidence_receipt_ids=("receipt",),
                rationale="Verified candidate",
            )
        elif kind == "scope_response":
            decision = BoundScopeResponseDecision(
                **fields,
                request_attempt_id=uuid4(),
                denied_paths=("src",),
                reason="Denied",
            )
        elif kind == "handoff":
            decision = TaskHandoff(
                **fields,
                attempt_id=request.attempt.attempt_id,
                status=HandoffStatus.COMPLETED,
                summary="Completed assigned work",
                candidate_tree_digest="a" * 64,
                evidence_receipt_ids=("receipt",),
            )
        else:
            decision = ScopeRequestDecision(**fields, requested_paths=("src",), reason="Need scope")
        encoded = encode_subscription_record(decision)
    proof = SubscriptionLaunchTerminalProof(
        launch_id="stopped",
        pid=123,
        process_identity="test-client",
        outcome="completed",
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    source = SimpleNamespace(
        contract=task,
        result=SimpleNamespace(
            result_payload={
                "effective_failure": None,
                "failure": None,
                "failure_detail": None,
                "proposal_context": {"envelope": encode_subscription_record(request.envelope)},
                "decision": encoded,
                "attempt": encode_subscription_record(request.attempt),
                "telemetry": encode_subscription_record(AttemptTelemetry()),
                "launch_proof": proof.model_dump(mode="json"),
            }
        ),
    )
    run = SimpleNamespace(
        suspended_state=RunState.PLANNING if kind == "plan" else RunState.IMPLEMENTING
    )
    expected = primary == (kind not in {"handoff", "scope_request"})
    assert _pending_decision_is_admissible(source, run) is expected
