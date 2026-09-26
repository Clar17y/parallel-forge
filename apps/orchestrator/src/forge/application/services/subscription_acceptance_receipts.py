"""Verify cited receipt bytes, then retain their bindings under current authority."""

from collections.abc import Callable
from uuid import UUID

from forge.application.adapters.named_check_receipts import decode_command_result
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_acceptance import PreparedSubscriptionAcceptance
from forge.application.ports.subscription_acceptance_receipts import (
    AcceptanceReceiptClaimError,
    AcceptanceReceiptProof,
    AcceptanceReceiptSource,
    ReceiptClaimErrorKind,
    VerifiedAcceptanceReceipts,
)
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_handoff import (
    HandoffCallProof,
    artifact_descriptor_digest,
)
from forge.application.ports.tool_recovery import TerminalEffectVerifier, terminal_call_digest
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.subscription_handoff import _decode_snapshot
from forge.application.services.subscription_receipt_artifacts import read_receipt_artifacts
from forge.domain.operation import canonical_digest
from forge.domain.tool import ToolName


class SubscriptionAcceptanceReceiptVerification:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        artifact_store: ArtifactStore,
        terminal_verifier: TerminalEffectVerifier,
    ) -> None:
        self._factory, self._store, self._terminal = work_factory, artifact_store, terminal_verifier

    async def reject_claims(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            result = await work.subscription_decisions.reject_acceptance_receipt_claims(attempt_id)
            await work.commit()
            return result

    async def verify(self, attempt_id: UUID) -> VerifiedAcceptanceReceipts | None:
        async with self._factory() as work:
            prepared = await work.subscription_decisions.prepare_acceptance(attempt_id)
            if not prepared.accepted:
                await work.commit()
                return None
            proposal = await work.subscription_decisions.acceptance_proposal(attempt_id)
            sources = await work.subscription_decisions.acceptance_receipt_sources(proposal)
            await work.commit()
        proof = await self._verify(proposal, sources)
        if proof is None:
            return None
        async with self._factory() as work:
            await work.subscription_decisions.record_acceptance_receipts(proposal, proof)
            await work.commit()
        return proof

    async def _verify(
        self, proposal: PreparedSubscriptionAcceptance, sources: tuple[AcceptanceReceiptSource, ...]
    ) -> VerifiedAcceptanceReceipts | None:
        try:
            return await self._verify_bytes(proposal, sources)
        except AcceptanceReceiptClaimError:
            raise
        except KeyError, OSError, RuntimeError, TypeError, ValueError, RecursionError:
            # Storage failures or damaged external evidence never consume repairs.
            return None

    async def _verify_bytes(
        self, proposal: PreparedSubscriptionAcceptance, sources: tuple[AcceptanceReceiptSource, ...]
    ) -> VerifiedAcceptanceReceipts | None:
        artifacts = await read_receipt_artifacts(
            self._factory,
            self._store,
            proposal.decision.run_id,
            tuple(digest for source in sources for digest in source.call.artifact_digests),
        )
        if artifacts is None:
            return None
        descriptors, blobs = artifacts
        proofs = []
        for source in sources:
            call = source.call
            assert (
                call.subscription_task_id is not None and call.subscription_attempt_id is not None
            )
            metadata = call.result_metadata or {}
            snapshot = None
            command_name: str | None = None
            result_digest: str | None = None
            commit_sha: str | None = None
            terminal = None
            if call.tool_name is ToolName.GIT_DIFF and call.normalized_arguments == {
                "scope": "snapshot"
            }:
                if call.operation_intent_id is not None or len(call.artifact_digests) != 1:
                    return None
                digest = call.artifact_digests[0]
                descriptor = descriptors[digest]
                if (
                    descriptor.producer_type != "subscription_working_tree_snapshot"
                    or descriptor.producer_id != call.id
                    or descriptor.parent_digests
                    or descriptor.schema_version != 1
                    or descriptor.media_type != "application/json"
                    or descriptor.byte_count > 4 * 1024 * 1024
                    or metadata.get("manifest_digest") != digest
                ):
                    return None
                snapshot = CandidateInspection.from_snapshot(
                    _decode_snapshot(
                        blobs[digest],
                        call,
                        proposal.worktree.identity.worktree_name,
                        proposal.worktree.base_sha,
                        proposal.policy.version,
                    )
                )
                if metadata.get("candidate_tree_digest") != snapshot.tree_digest:
                    return None
                matches = snapshot == proposal.review.candidate
            elif call.tool_name in {ToolName.BUILD_RUN_NAMED_CHECK, ToolName.GIT_COMMIT}:
                terminal = await self._terminal.verify_terminal_effect(call.id)
                if (
                    terminal is None
                    or terminal.effect_id != call.id
                    or terminal.call_digest != terminal_call_digest(call)
                    or call.operation_intent_id != call.id
                    or call.id not in dict(terminal.intent_digests)
                ):
                    return None
                if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                    result_value = metadata.get("command_result_digest")
                    # The named-check receipt links its command result as a
                    # parent artifact. Require verified reachable bytes, then
                    # recheck the result's exact producer and terminal proof.
                    if not isinstance(result_value, str) or result_value not in blobs:
                        return None
                    result_digest = result_value
                    result = decode_command_result(blobs[result_digest])
                    descriptor = descriptors[result_digest]
                    if (
                        descriptor.producer_type != "command_result"
                        or descriptor.producer_id != call.id
                        or descriptor.schema_version != 1
                        or descriptor.media_type
                        not in {"application/json", "application/vnd.forge.command-result+json"}
                        or result.evidence_digest != result_digest
                        or result.command_name != call.normalized_arguments.get("command_name")
                        or result.policy_version != proposal.policy.version
                        or result.exit_code != 0
                        or result.timed_out
                    ):
                        return None
                    command_name = result.command_name
                    matches = (
                        metadata.get("candidate_tree_digest_before")
                        == proposal.review.candidate.tree_digest
                        and metadata.get("candidate_tree_digest_after")
                        == proposal.review.candidate.tree_digest
                    )
                else:
                    commit_value = metadata.get("new_sha")
                    if not isinstance(commit_value, str):
                        return None
                    commit_sha = commit_value
                    # A matching HEAD does not prove the selected working tree:
                    # it may contain later uncommitted changes. Retain the commit
                    # identity without upgrading it to a full candidate binding.
                    matches = False
            else:
                raise AcceptanceReceiptClaimError(ReceiptClaimErrorKind.UNSUPPORTED_TOOL, call.id)
            proofs.append(
                AcceptanceReceiptProof(
                    task_id=call.subscription_task_id,
                    attempt_id=call.subscription_attempt_id,
                    producer_digest=source.producer_digest,
                    tool_name=call.tool_name,
                    call=HandoffCallProof(
                        call.id, terminal_call_digest(call), source.receipt_digest, terminal
                    ),
                    snapshot=snapshot,
                    command_name=command_name,
                    command_result_digest=result_digest,
                    commit_sha=commit_sha,
                    matches_candidate=matches,
                )
            )
        return VerifiedAcceptanceReceipts(
            proposal.result_digest,
            canonical_digest(proposal.review.payload()),
            tuple(proofs),
            tuple(
                (digest, artifact_descriptor_digest(descriptor))
                for digest, descriptor in sorted(descriptors.items())
            ),
        )
