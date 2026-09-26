"""Failed command evidence stays historical; only final checks prove the candidate."""

import hashlib
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from forge.application.adapters.named_check_receipts import encode_command_result
from forge.application.ports.runner import CommandResult
from forge.application.ports.tool_recovery import VerifiedTerminalEffect, terminal_call_digest
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from forge.domain.operation import canonical_digest
from forge.domain.policy import RunnerMode, StepKind
from forge.domain.subscription import CheckResultEvidence, ToolCallBinding
from forge.domain.tool import ToolCallStatus, ToolName
from test_subscription_handoff import case


def history_case(*, truncated=False, timed_out=False):
    state, verifier, handoff, kwargs = case()
    snapshot = next(iter(state.calls.values()))
    claims = []
    for index in range(2):
        identity = uuid4()
        passed = index == 1
        started = snapshot.started_at - timedelta(seconds=5 - index * 2)
        result = CommandResult(
            command_name="unit",
            kind=StepKind.TEST,
            command_digest="a" * 64,
            policy_version=1,
            exit_code=int(not passed),
            timed_out=timed_out and not passed,
            started_at=started,
            duration_ms=25,
            stdout_digest="b" * 64,
            stderr_digest="c" * 64,
            runner_mode=RunnerMode.TRUSTED_HOST,
            image_digest=None,
            network_enabled=False,
            stdout_original_byte_count=0,
            stderr_original_byte_count=0,
            stdout_truncated=truncated and not passed,
            stderr_truncated=False,
            unsandboxed=True,
        )
        data = encode_command_result(result)
        digest = hashlib.sha256(data).hexdigest()
        state.data[digest] = data
        state.descriptors[digest] = ArtifactDescriptor(
            digest=digest,
            media_type="application/json",
            byte_count=len(data),
            storage_path=canonical_storage_pointer(digest),
            run_id=handoff.run_id,
            producer_type="command_result",
            producer_id=identity,
        )
        candidate = handoff.candidate_tree_digest if passed else "d" * 64
        call = replace(
            snapshot,
            id=identity,
            tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
            normalized_arguments={"command_name": "unit"},
            artifact_digests=(digest,),
            operation_intent_id=identity,
            status=ToolCallStatus.SUCCEEDED if passed else ToolCallStatus.FAILED,
            started_at=started,
            completed_at=started + timedelta(milliseconds=50),
            result_metadata={
                "command_result_digest": digest,
                "candidate_tree_digest_before": candidate,
                "candidate_tree_digest_after": candidate,
            },
        )
        state.calls[identity] = call
        state.receipts[identity] = (
            ToolCallBinding(
                attempt_id=handoff.attempt_id,
                provider_call_key=str(index),
                durable_operation_id=identity,
                tool_name=call.tool_name,
                arguments_digest=canonical_digest(call.normalized_arguments),
            ),
            {
                "accepted": passed,
                "result": {
                    "tool_name": call.tool_name.value,
                    "status": call.status.value,
                    "artifact_digests": [digest],
                    "operation_intent_id": str(identity),
                },
            },
        )
        claims.append(
            CheckResultEvidence(
                command_name="unit",
                exit_code=int(not passed),
                passed=passed,
                output_digest=digest,
                duration_ms=25,
                receipt_id=str(identity),
            )
        )

    async def terminal(identity):
        call = state.calls[identity]
        return VerifiedTerminalEffect(
            effect_id=identity,
            call_digest=terminal_call_digest(call),
            intent_digests=((identity, "e" * 64),),
        )

    verifier._terminal.verify_terminal_effect.side_effect = terminal
    kwargs["task"] = replace(kwargs["task"], named_checks=("unit",))
    handoff = replace(
        handoff,
        check_results=tuple(claims),
        evidence_receipt_ids=(*handoff.evidence_receipt_ids, *(item.receipt_id for item in claims)),
    )
    return state, verifier, handoff, kwargs


async def test_failed_check_is_verified_without_claiming_it_passed_current_tree():
    _, verifier, handoff, kwargs = history_case()
    proof = await verifier.verify(handoff, **kwargs)
    assert proof is not None and proof.checks_match_snapshot
    assert len(proof.call_proofs) == 3


@pytest.mark.parametrize(
    "change",
    [
        "accepted_failure",
        "reversed_calls",
        "after_snapshot",
        "duration",
        "exit_code",
        "historical_drift",
        "cancelled",
        "wrong_tool",
        "missing_claim",
        "missing_terminal",
        "corrupt_artifact",
        "truncated",
        "timed_out",
    ],
)
async def test_history_never_relaxes_terminal_lineage_or_chronology(change):
    state, verifier, handoff, kwargs = history_case(
        truncated=change == "truncated",
        timed_out=change == "timed_out",
    )
    failed, passed = (UUID(item.receipt_id) for item in handoff.check_results)
    call = state.calls[failed]
    if change == "accepted_failure":
        state.receipts[failed][1]["accepted"] = True
    elif change == "reversed_calls":
        state.calls[failed] = replace(
            call, completed_at=state.calls[passed].started_at + timedelta(seconds=1)
        )
    elif change == "after_snapshot":
        snapshot = state.calls[UUID(handoff.evidence_receipt_ids[0])]
        state.calls[passed] = replace(
            state.calls[passed], completed_at=snapshot.started_at + timedelta(seconds=1)
        )
    elif change in {"duration", "exit_code"}:
        claim = replace(
            handoff.check_results[0],
            **({"duration_ms": 1} if change == "duration" else {"exit_code": 2}),
        )
        handoff = replace(handoff, check_results=(claim, handoff.check_results[1]))
    elif change == "historical_drift":
        state.calls[failed] = replace(
            call, result_metadata={**call.result_metadata, "candidate_tree_digest_after": "f" * 64}
        )
    elif change == "cancelled":
        state.calls[failed] = replace(call, status=ToolCallStatus.CANCELLED)
    elif change == "wrong_tool":
        state.calls[failed] = replace(call, tool_name=ToolName.GIT_COMMIT)
    elif change == "missing_claim":
        handoff = replace(handoff, check_results=handoff.check_results[1:])
    elif change == "missing_terminal":
        verifier._terminal.verify_terminal_effect.side_effect = None
        verifier._terminal.verify_terminal_effect.return_value = None
    elif change == "corrupt_artifact":
        state.data[handoff.check_results[0].output_digest] = b"changed"
    assert await verifier.verify(handoff, **kwargs) is None


async def test_latest_check_must_still_match_the_final_candidate():
    state, verifier, handoff, kwargs = history_case()
    identity = UUID(handoff.check_results[-1].receipt_id)
    call = state.calls[identity]
    state.calls[identity] = replace(
        call,
        result_metadata={
            **call.result_metadata,
            "candidate_tree_digest_before": "f" * 64,
            "candidate_tree_digest_after": "f" * 64,
        },
    )
    proof = await verifier.verify(handoff, **kwargs)
    assert proof is not None and not proof.checks_match_snapshot
