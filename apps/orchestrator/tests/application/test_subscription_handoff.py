"""Completed handoffs require actual bound evidence, not copied provider hashes."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.tools import ToolCallRecord
from forge.application.ports.worktrees import GitSnapshotFile, GitWorkingTreeSnapshot
from forge.application.services.subscription_handoff import SubscriptionHandoffVerifier
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    HandoffStatus,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    ToolCallBinding,
)
from forge.domain.tool import ToolCallStatus, ToolName


class Work:
    def __init__(self, state):
        self.state = state
        self.tool_calls = SimpleNamespace(
            get=AsyncMock(side_effect=lambda identity: state.calls[identity])
        )
        self.subscription = SimpleNamespace(
            operation_evidence=AsyncMock(
                side_effect=lambda identity, **kwargs: state.receipts.get(identity)
            )
        )
        self.artifacts = SimpleNamespace(
            get_by_digest=AsyncMock(side_effect=lambda digest, **kwargs: state.descriptors[digest])
        )

    async def __aenter__(self):
        self.state.active += 1
        return self

    async def __aexit__(self, *_):
        self.state.active -= 1

    async def rollback(self):
        pass


def case():
    run, task_id, attempt, call_id = (uuid4() for _ in range(4))
    route = RouteSpec(provider="test", client="controlled", model="test")
    task = LogicalTaskContract(
        run_id=run,
        task_id=task_id,
        parent_task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(),
        owned_paths=("apps",),
    )
    snapshot = GitWorkingTreeSnapshot(
        head_sha="a" * 40,
        base_sha="a" * 40,
        files=(
            GitSnapshotFile(
                path="apps/file.py", mode="100644", content_digest="b" * 64, byte_count=1
            ),
        ),
        changed_paths=("apps/file.py", "sibling/file.py"),
    )
    manifest = dict(
        snapshot.manifest(),
        run_id=str(run),
        task_id=str(task_id),
        attempt_id=str(attempt),
        tool_call_id=str(call_id),
        worktree_id="worktree",
        policy_version=1,
    )
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(data).hexdigest()
    descriptor = ArtifactDescriptor(
        digest=digest,
        media_type="application/json",
        byte_count=len(data),
        storage_path=canonical_storage_pointer(digest),
        run_id=run,
        producer_type="subscription_working_tree_snapshot",
        producer_id=call_id,
    )
    call = ToolCallRecord(
        id=call_id,
        run_id=run,
        agent_execution_id=None,
        subscription_task_id=task_id,
        subscription_attempt_id=attempt,
        subscription_purpose=task.purpose.value,
        tool_name=ToolName.GIT_DIFF,
        normalized_arguments={"scope": "snapshot"},
        authorized=True,
        status=ToolCallStatus.SUCCEEDED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        policy_version=1,
        resource_id="resource",
        request_digest=canonical_digest({"scope": "snapshot"}),
        invocation_schema_version=1,
        result_metadata_schema_version=1,
        artifact_digests=(digest,),
        result_metadata={
            "candidate_tree_digest": snapshot.candidate_tree_digest,
            "manifest_digest": digest,
        },
    )
    binding = ToolCallBinding(
        attempt_id=attempt,
        provider_call_key="snapshot",
        durable_operation_id=call_id,
        tool_name=call.tool_name,
        arguments_digest=canonical_digest(call.normalized_arguments),
    )
    receipt = {
        "accepted": True,
        "result": {
            "tool_name": call.tool_name.value,
            "status": "succeeded",
            "artifact_digests": [digest],
            "metadata": dict(call.result_metadata),
            "operation_intent_id": None,
        },
    }
    state = SimpleNamespace(
        active=0,
        calls={call_id: call},
        receipts={call_id: (binding, receipt)},
        descriptors={digest: descriptor},
        data={digest: data},
    )

    async def verify(digest):
        assert state.active == 0
        return digest in state.data

    async def read(digest, **kwargs):
        assert state.active == 0
        return state.data[digest]

    store = SimpleNamespace(verify=verify, open_bytes=read)
    verifier = SubscriptionHandoffVerifier(
        lambda: Work(state),
        store,
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    handoff = TaskHandoff(
        run_id=run,
        task_id=task_id,
        attempt_id=attempt,
        status=HandoffStatus.COMPLETED,
        candidate_tree_digest=snapshot.candidate_tree_digest,
        changed_paths=("apps/file.py",),
        evidence_receipt_ids=(str(call_id),),
    )
    kwargs = {
        "task": task,
        "policy_version": 1,
        "worktree_id": "worktree",
        "base_sha": "a" * 40,
        "resource_id": "resource",
    }
    return state, verifier, handoff, kwargs


async def test_snapshot_handoff_binds_actual_evidence_and_permits_disjoint_sibling_changes():
    state, verifier, handoff, kwargs = case()
    proof = await verifier.verify(handoff, **kwargs)
    assert proof is not None
    assert proof.candidate_tree_digest == handoff.candidate_tree_digest
    assert proof.snapshot_call_id == next(iter(state.calls))
    assert proof.checks_match_snapshot
    assert state.active == 0


@pytest.mark.parametrize("change", [None, "different_head", "plain_handoff", "implementation"])
async def test_typed_review_can_identify_observed_head_without_claiming_a_commit_effect(change):
    from dataclasses import fields

    from forge.domain.agent import ReviewDecision, ReviewOutput
    from forge.domain.subscription import ReviewedTaskHandoff

    state, verifier, original, kwargs = case()
    purpose = (
        SpecialistPurpose.ROUTINE_IMPLEMENTATION
        if change == "implementation"
        else SpecialistPurpose.INDEPENDENT_REVIEW
    )
    kwargs["task"] = replace(kwargs["task"], purpose=purpose, owned_paths=())
    identity, call = next(iter(state.calls.items()))
    state.calls[identity] = replace(call, subscription_purpose=purpose.value)
    original = replace(
        original,
        candidate_commit="b" * 40 if change == "different_head" else "a" * 40,
        changed_paths=(),
    )
    handoff = (
        original
        if change == "plain_handoff"
        else ReviewedTaskHandoff(
            **{field.name: getattr(original, field.name) for field in fields(original)},
            review_output=ReviewOutput(
                decision=ReviewDecision.APPROVE,
                summary="Observed candidate",
                tested_claims=("Inspected snapshot",),
                missing_evidence=(),
            ),
        )
    )
    proof = await verifier.verify(handoff, **kwargs)
    assert (proof is not None) == (change is None)
    verifier._terminal.verify_terminal_effect.assert_not_awaited()


@pytest.mark.parametrize("change", ["same", "output", "storage"])
async def test_assessment_distinguishes_proven_output_change_from_unavailable_evidence(change):
    state, verifier, handoff, kwargs = case()
    manifest = json.loads(next(iter(state.data.values())))
    current = GitWorkingTreeSnapshot(
        head_sha=manifest["head_sha"],
        base_sha=manifest["base_sha"],
        files=tuple(GitSnapshotFile(**item) for item in manifest["files"]),
        changed_paths=tuple(manifest["changed_paths"]),
    )
    if change == "output":
        current = replace(current, files=(replace(current.files[0], content_digest="e" * 64),))
    elif change == "storage":

        async def unavailable(*args, **kwargs):
            raise OSError("storage offline")

        verifier._store.open_bytes = unavailable
    assessment = await verifier.assess(handoff, current_snapshot=current, **kwargs)
    if change == "storage":
        assert assessment is None
    elif change == "output":
        assert assessment.reason(handoff).value == "outputs_changed"
        assert assessment.verified.current_tree_digest is None
        assert assessment.current_tree_digest == current.candidate_tree_digest
    else:
        assert assessment.current_tree_digest == current.candidate_tree_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "missing_receipt",
        "rejected_receipt",
        "foreign_call",
        "foreign_descriptor",
        "wrong_policy",
        "wrong_worktree",
        "wrong_base",
        "wrong_resource",
        "copied_digest",
        "unowned_changes",
        "omitted_changes",
        "duplicate_receipt",
        "forged_manifest",
        "extra_schema",
        "missing_check",
        "unproved_commit",
        "failed_call",
        "wrong_binding",
    ],
)
async def test_handoff_rejects_unproved_or_foreign_evidence(mutation):
    state, verifier, handoff, kwargs = case()
    identity, call = next(iter(state.calls.items()))
    digest, descriptor = next(iter(state.descriptors.items()))
    binding, receipt = state.receipts[identity]
    if mutation == "bytes":
        state.data[digest] = b"tampered"
    elif mutation == "missing_receipt":
        state.receipts.clear()
    elif mutation == "rejected_receipt":
        receipt["accepted"] = False
    elif mutation == "foreign_call":
        state.calls[identity] = replace(call, subscription_attempt_id=uuid4())
    elif mutation == "foreign_descriptor":
        state.descriptors[digest] = replace(descriptor, producer_id=uuid4())
    elif mutation == "wrong_policy":
        kwargs["policy_version"] = 2
    elif mutation == "wrong_worktree":
        kwargs["worktree_id"] = "foreign"
    elif mutation == "wrong_base":
        kwargs["base_sha"] = "c" * 40
    elif mutation == "wrong_resource":
        kwargs["resource_id"] = "foreign"
    elif mutation == "copied_digest":
        handoff = replace(handoff, candidate_tree_digest="c" * 64)
    elif mutation == "unowned_changes":
        handoff = replace(handoff, changed_paths=("sibling/file.py",))
    elif mutation == "omitted_changes":
        handoff = replace(handoff, changed_paths=())
    elif mutation == "duplicate_receipt":
        handoff = replace(handoff, evidence_receipt_ids=handoff.evidence_receipt_ids * 2)
    elif mutation in {"forged_manifest", "extra_schema"}:
        value = json.loads(state.data[digest])
        if mutation == "forged_manifest":
            value["attempt_id"] = str(uuid4())
        else:
            value["injected"] = True
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        changed = hashlib.sha256(data).hexdigest()
        state.data = {changed: data}
        state.descriptors = {
            changed: replace(
                descriptor,
                digest=changed,
                storage_path=canonical_storage_pointer(changed),
                byte_count=len(data),
                original_byte_count=len(data),
            )
        }
        state.calls[identity] = replace(
            call,
            artifact_digests=(changed,),
            result_metadata=dict(call.result_metadata, manifest_digest=changed),
        )
        receipt["result"]["artifact_digests"] = [changed]
    elif mutation == "missing_check":
        kwargs["task"] = replace(kwargs["task"], named_checks=("unit",))
    elif mutation == "unproved_commit":
        handoff = replace(handoff, candidate_commit="a" * 40)
    elif mutation == "failed_call":
        state.calls[identity] = replace(call, status=ToolCallStatus.FAILED)
    elif mutation == "wrong_binding":
        state.receipts[identity] = (replace(binding, arguments_digest="c" * 64), receipt)
    assert await verifier.verify(handoff, **kwargs) is None
    assert state.active == 0


@pytest.mark.parametrize("candidate", [None, "matching", "changed", "before_changed"])
async def test_named_check_claim_is_bound_to_verified_command_result(candidate):
    from forge.application.adapters.named_check_receipts import encode_command_result
    from forge.application.ports.runner import CommandResult
    from forge.application.ports.tool_recovery import VerifiedTerminalEffect, terminal_call_digest
    from forge.domain.policy import RunnerMode, StepKind
    from forge.domain.subscription import CheckResultEvidence

    state, verifier, handoff, kwargs = case()
    snapshot_call = next(iter(state.calls.values()))
    identity, intent = uuid4(), uuid4()
    result = CommandResult(
        command_name="unit",
        kind=StepKind.TEST,
        command_digest="a" * 64,
        policy_version=1,
        exit_code=0,
        timed_out=False,
        started_at=datetime.now(UTC),
        duration_ms=25,
        stdout_digest="b" * 64,
        stderr_digest="c" * 64,
        runner_mode=RunnerMode.TRUSTED_HOST,
        image_digest=None,
        network_enabled=False,
        stdout_original_byte_count=0,
        stderr_original_byte_count=0,
        stdout_truncated=False,
        stderr_truncated=False,
        unsandboxed=True,
    )
    data = encode_command_result(result)
    digest = hashlib.sha256(data).hexdigest()
    descriptor = ArtifactDescriptor(
        digest=digest,
        media_type="application/json",
        byte_count=len(data),
        storage_path=canonical_storage_pointer(digest),
        run_id=handoff.run_id,
        producer_type="command_result",
        producer_id=identity,
    )
    call = replace(
        snapshot_call,
        id=identity,
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        normalized_arguments={"command_name": "unit"},
        artifact_digests=(digest,),
        operation_intent_id=intent,
        result_metadata={"command_result_digest": digest},
        duration_ms=100,
    )
    if candidate is not None:
        call = replace(
            call,
            result_metadata={
                **call.result_metadata,
                "candidate_tree_digest_before": "f" * 64
                if candidate == "before_changed"
                else handoff.candidate_tree_digest,
                "candidate_tree_digest_after": handoff.candidate_tree_digest
                if candidate in {"matching", "before_changed"}
                else "f" * 64,
            },
        )
    binding = ToolCallBinding(
        attempt_id=handoff.attempt_id,
        provider_call_key="unit",
        durable_operation_id=identity,
        tool_name=call.tool_name,
        arguments_digest=canonical_digest(call.normalized_arguments),
    )
    state.calls[identity] = call
    state.descriptors[digest] = descriptor
    state.data[digest] = data
    state.receipts[identity] = (
        binding,
        {
            "accepted": True,
            "result": {
                "tool_name": call.tool_name.value,
                "status": "succeeded",
                "artifact_digests": [digest],
                "operation_intent_id": str(intent),
            },
        },
    )
    terminal = VerifiedTerminalEffect(
        effect_id=identity,
        call_digest=terminal_call_digest(call),
        intent_digests=((intent, "d" * 64),),
    )
    verifier._terminal.verify_terminal_effect.return_value = terminal
    claim = CheckResultEvidence(
        command_name="unit",
        exit_code=0,
        passed=True,
        output_digest=digest,
        duration_ms=25,
        receipt_id=str(identity),
    )
    handoff = replace(
        handoff,
        check_results=(claim,),
        evidence_receipt_ids=handoff.evidence_receipt_ids + (str(identity),),
    )
    kwargs["task"] = replace(kwargs["task"], named_checks=("unit",))
    proof = await verifier.verify(handoff, **kwargs)
    assert proof is not None
    assert proof.checks_match_snapshot is (candidate == "matching")
    manifest = json.loads(state.data[snapshot_call.artifact_digests[0]])
    current = GitWorkingTreeSnapshot(
        head_sha=manifest["head_sha"],
        base_sha=manifest["base_sha"],
        files=tuple(GitSnapshotFile(**item) for item in manifest["files"]),
        changed_paths=tuple(manifest["changed_paths"]),
    )
    assessment = await verifier.assess(handoff, current_snapshot=current, **kwargs)
    if candidate == "matching":
        assert assessment.checks_match_snapshot
    else:
        assert assessment.reason(handoff).value == "checks_not_bound"
    # Provider duration must be the actual command duration, not tool service overhead.
    assert (
        await verifier.verify(
            replace(handoff, check_results=(replace(claim, duration_ms=100),)), **kwargs
        )
        is None
    )
    assert (
        await verifier.verify(
            replace(handoff, check_results=(replace(claim, output_digest="f" * 64),)), **kwargs
        )
        is None
    )
    verifier._terminal.verify_terminal_effect.return_value = replace(terminal, call_digest="f" * 64)
    assert await verifier.verify(handoff, **kwargs) is None


@pytest.mark.parametrize(
    "change", ["same", "sibling", "content", "mode", "deleted", "created", "changed_paths", "base"]
)
async def test_current_handoff_observation_preserves_owned_outputs(change):
    state, verifier, handoff, kwargs = case()
    manifest = json.loads(next(iter(state.data.values())))
    observed = GitWorkingTreeSnapshot(
        head_sha=manifest["head_sha"],
        base_sha=manifest["base_sha"],
        files=tuple(GitSnapshotFile(**item) for item in manifest["files"]),
        changed_paths=tuple(manifest["changed_paths"]),
    )
    current = observed
    if change == "sibling":
        current = replace(
            observed,
            files=observed.files
            + (
                GitSnapshotFile(
                    path="sibling/new.py", mode="100644", content_digest="e" * 64, byte_count=2
                ),
            ),
        )
    elif change == "content":
        current = replace(observed, files=(replace(observed.files[0], content_digest="e" * 64),))
    elif change == "mode":
        current = replace(observed, files=(replace(observed.files[0], mode="100755"),))
    elif change == "deleted":
        current = replace(observed, files=())
    elif change == "created":
        current = replace(
            observed,
            files=observed.files
            + (
                GitSnapshotFile(
                    path="apps/new.py", mode="100644", content_digest="e" * 64, byte_count=2
                ),
            ),
        )
    elif change == "changed_paths":
        current = replace(observed, changed_paths=("sibling/file.py",))
    elif change == "base":
        current = replace(observed, base_sha="e" * 40)
    proof = await verifier.verify(handoff, current_snapshot=current, **kwargs)
    if change in {"same", "sibling"}:
        assert proof is not None
        assert proof.current_tree_digest == current.candidate_tree_digest
        assert len(proof.output_digest) == 64
    else:
        assert proof is None


async def test_task_without_owned_paths_requires_whole_tree_match():
    from forge.application.services.subscription_handoff import handoff_output_digest

    _, _, _, kwargs = case()
    task = replace(kwargs["task"], owned_paths=())
    original = GitWorkingTreeSnapshot(
        head_sha="a" * 40,
        base_sha="a" * 40,
        files=(
            GitSnapshotFile(
                path="sibling/file.py", mode="100644", content_digest="b" * 64, byte_count=1
            ),
        ),
        changed_paths=("sibling/file.py",),
    )
    changed = replace(original, files=(replace(original.files[0], content_digest="c" * 64),))
    assert handoff_output_digest(original, task) != handoff_output_digest(changed, task)


async def test_scope_prefix_does_not_include_neighbor_directory():
    from forge.application.services.subscription_handoff import handoff_output_digest

    _, _, _, kwargs = case()
    original = GitWorkingTreeSnapshot(
        head_sha="a" * 40,
        base_sha="a" * 40,
        files=(
            GitSnapshotFile(
                path="apps-extra/file.py", mode="100644", content_digest="b" * 64, byte_count=1
            ),
        ),
        changed_paths=("apps-extra/file.py",),
    )
    changed = replace(original, files=(replace(original.files[0], content_digest="c" * 64),))
    assert handoff_output_digest(original, kwargs["task"]) == handoff_output_digest(
        changed, kwargs["task"]
    )


async def test_historical_handoff_does_not_invent_current_observation():
    _, verifier, handoff, kwargs = case()
    proof = await verifier.verify(handoff, **kwargs)
    assert proof is not None and proof.current_tree_digest is None
    assert len(proof.output_digest) == 64
