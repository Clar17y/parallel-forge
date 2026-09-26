"""Locked proof rechecks use real broker receipts and artifact lineage rows."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_handoff import (
    HandoffCallProof,
    VerifiedSubscriptionHandoff,
    artifact_descriptor_digest,
    operation_evidence_digest,
)
from forge.application.ports.tool_recovery import terminal_call_digest
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.domain.subscription import decode_subscription_record, encode_subscription_record
from forge.domain.tool import ToolName
from forge.persistence.models.execution import Artifact, ArtifactLineage, OperationIntent, ToolCall
from forge.persistence.models.subscription import SubscriptionOperationBinding
from forge.persistence.repositories.subscription_handoff_evidence import (
    PostgresSubscriptionHandoffEvidence,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_snapshot_tool import _snapshot_case


async def snapshot_proof(session_factory, tmp_path, *, commit=False):
    broker, _, attempt, store, _, authority = await _snapshot_case(session_factory, tmp_path)
    terminal = None
    if commit:
        permitted = frozenset({ToolName.GIT_DIFF, ToolName.GIT_COMMIT})
        authority = replace(authority, permitted_tools=permitted)
        broker = SubscriptionToolBroker(
            lambda: PostgresUnitOfWork(session_factory),
            lease=broker._lease,
            authority=authority,
            effect=ControlledSubscriptionEffect(
                broker._effect.service, replace(broker._effect.context, permitted_tools=permitted)
            ),
        )
        committed = await broker.invoke(
            token="commit-token",
            provider_call_key="handoff-commit",
            tool_name=ToolName.GIT_COMMIT,
            arguments={"message": "test: handoff evidence"},
        )
        assert committed.accepted and committed.result["status"] == "succeeded"
        terminal = await ToolRecoveryService(
            lambda: PostgresUnitOfWork(session_factory),
            store,
        ).verify_terminal_effect(committed.operation_id)
        assert terminal is not None
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="handoff-proof",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(receipt.operation_id)
        binding, stored = await work.subscription.operation_evidence(
            call.id,
            run_id=authority.run_id,
            task_id=authority.task_id,
            attempt_id=attempt,
        )
        descriptor = await work.artifacts.get_by_digest(
            call.artifact_digests[0], run_id=authority.run_id
        )
    proof = VerifiedSubscriptionHandoff(
        run_id=authority.run_id,
        task_id=authority.task_id,
        attempt_id=attempt,
        snapshot_call_id=call.id,
        manifest_digest=descriptor.digest,
        candidate_tree_digest=receipt.result["metadata"]["candidate_tree_digest"],
        policy_version=call.policy_version,
        task_digest="a" * 64,
        handoff_digest="b" * 64,
        call_proofs=(
            HandoffCallProof(
                call.id, terminal_call_digest(call), operation_evidence_digest(binding, stored)
            ),
        ),
        artifact_proofs=((descriptor.digest, artifact_descriptor_digest(descriptor)),),
        checks_match_snapshot=True,
        output_digest="c" * 64,
    )
    if terminal is not None:
        async with PostgresUnitOfWork(session_factory) as work:
            call = await work.tool_calls.get(terminal.effect_id)
            binding, stored = await work.subscription.operation_evidence(
                call.id,
                run_id=authority.run_id,
                task_id=authority.task_id,
                attempt_id=attempt,
            )
            descriptors = dict(proof.artifact_proofs)
            pending = list(call.artifact_digests)
            while pending:
                digest = pending.pop()
                if digest in descriptors:
                    continue
                descriptor = await work.artifacts.get_by_digest(digest, run_id=authority.run_id)
                descriptors[digest] = artifact_descriptor_digest(descriptor)
                pending.extend(descriptor.parent_digests)
        proof = replace(
            proof,
            call_proofs=(
                *proof.call_proofs,
                HandoffCallProof(
                    call.id,
                    terminal_call_digest(call),
                    operation_evidence_digest(binding, stored),
                    terminal,
                ),
            ),
            artifact_proofs=tuple(sorted(descriptors.items())),
        )
    return proof


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "receipt",
        "binding",
        "call",
        "descriptor",
        "lineage",
        "missing_artifact",
        "duplicate_call",
        "duplicate_artifact",
        "foreign_task",
        "missing_snapshot",
    ],
)
async def test_locked_handoff_rechecks_complete_snapshot_evidence(
    session_factory, tmp_path, mutation
):
    proof = await snapshot_proof(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        if mutation in {"receipt", "binding"}:
            row = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == proof.snapshot_call_id
                )
            )
            if mutation == "receipt":
                row.receipt_payload = {**row.receipt_payload, "accepted": False}
            else:
                row.payload = encode_subscription_record(
                    replace(
                        decode_subscription_record(row.payload),
                        provider_call_key="changed-key",
                    )
                )
        elif mutation == "call":
            row = await work.session.get(ToolCall, proof.snapshot_call_id)
            row.authorized = False
        elif mutation == "descriptor":
            row = await work.session.scalar(
                select(Artifact).where(Artifact.digest == proof.manifest_digest)
            )
            row.media_type = "application/changed"
        elif mutation == "lineage":
            row = await work.session.scalar(
                select(ArtifactLineage).where(
                    ArtifactLineage.run_id == proof.run_id,
                    ArtifactLineage.producer_id == proof.snapshot_call_id,
                )
            )
            row.producer_kind = "changed-producer"
        await work.commit()
    if mutation == "missing_artifact":
        proof = replace(proof, artifact_proofs=())
    elif mutation == "duplicate_call":
        proof = replace(proof, call_proofs=proof.call_proofs * 2)
    elif mutation == "duplicate_artifact":
        proof = replace(proof, artifact_proofs=proof.artifact_proofs * 2)
    elif mutation == "foreign_task":
        proof = replace(proof, task_id=uuid4())
    elif mutation == "missing_snapshot":
        proof = replace(proof, snapshot_call_id=uuid4())
    async with PostgresUnitOfWork(session_factory) as work:
        assert await PostgresSubscriptionHandoffEvidence(work.session).verify(proof) is (
            mutation == "none"
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation", ["none", "intent", "publication", "missing_terminal", "missing_parent"]
)
async def test_locked_handoff_rechecks_commit_and_publication(session_factory, tmp_path, mutation):
    proof = await snapshot_proof(session_factory, tmp_path, commit=True)
    commit = proof.call_proofs[1]
    assert len(commit.terminal.intent_digests) == 2
    if mutation in {"intent", "publication"}:
        identity = commit.terminal.intent_digests[0 if mutation == "intent" else 1][0]
        async with PostgresUnitOfWork(session_factory) as work:
            row = await work.session.get(OperationIntent, identity)
            row.outcome_payload = {**row.outcome_payload, "unexpected": "changed"}
            await work.commit()
    elif mutation == "missing_terminal":
        proof = replace(proof, call_proofs=(proof.call_proofs[0], replace(commit, terminal=None)))
    elif mutation == "missing_parent":
        # Drop a non-manifest artifact; the full parent closure is mandatory.
        digest = next(
            digest for digest, _ in proof.artifact_proofs if digest != proof.manifest_digest
        )
        proof = replace(
            proof,
            artifact_proofs=tuple(item for item in proof.artifact_proofs if item[0] != digest),
        )
    async with PostgresUnitOfWork(session_factory) as work:
        assert await PostgresSubscriptionHandoffEvidence(work.session).verify(proof) is (
            mutation == "none"
        )
