"""Frozen producers and broker receipts cited by a current acceptance intent."""

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_acceptance_receipts import (
    AcceptanceReceiptClaimError,
    AcceptanceReceiptSource,
    ReceiptClaimErrorKind,
    VerifiedAcceptanceReceipts,
)
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_handoff import operation_evidence_digest
from forge.application.ports.tool_recovery import terminal_call_digest, terminal_intent_digest
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AttemptIdentity,
    ExecutionEnvelope,
    LogicalTaskContract,
    SpecialistPurpose,
    ToolCallBinding,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.tool import ToolCallStatus, ToolName
from forge.persistence.models.execution import OperationIntent, ToolCall
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.operations import _intent_from_record
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_acceptance import acceptance_proposal
from forge.persistence.repositories.subscription_handoff_evidence import verify_artifact_closure
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.tool_calls import _record_from_row


async def acceptance_receipt_sources(
    session: AsyncSession, proposal: PreparedSubscriptionAcceptance
) -> tuple[AcceptanceReceiptSource, ...]:
    current = await acceptance_proposal(session, proposal.attempt_id)
    if replace(current, inspection=None) != replace(proposal, inspection=None):
        raise SubscriptionDecisionError("acceptance source changed before receipt verification")
    return await _receipt_sources(session, current)


async def _receipt_sources(
    session: AsyncSession, proposal: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance
) -> tuple[AcceptanceReceiptSource, ...]:
    # The caller holds the run lock. Current admission separately proves a closed,
    # quiescent candidate; historical replay grants no current effect authority.
    run_id = proposal.decision.run_id
    envelope = await PostgresSubscriptionRepository(session).envelope_for_run(run_id)
    sources = []
    for value in proposal.decision.evidence_receipt_ids:
        identity = UUID(value)
        row = await session.scalar(
            select(ToolCall)
            .where(ToolCall.id == identity, ToolCall.run_id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.run_id != run_id:
            raise AcceptanceReceiptClaimError(ReceiptClaimErrorKind.ABSENT_OR_FOREIGN, identity)
        call = _record_from_row(row)
        if call.subscription_attempt_id is None or call.subscription_task_id is None:
            raise AcceptanceReceiptClaimError(
                ReceiptClaimErrorKind.NON_SUBSCRIPTION_PRODUCER, identity
            )
        attempt = await session.get(
            SubscriptionAttempt,
            call.subscription_attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        result = await session.get(
            SubscriptionAttemptResult,
            call.subscription_attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        task_row = await session.get(
            SubscriptionTask,
            call.subscription_task_id,
            with_for_update=True,
            populate_existing=True,
        )
        if attempt is None or result is None or task_row is None:
            raise SubscriptionDecisionError("acceptance receipt producer evidence is absent")
        try:
            payload = result.result_payload
            context = payload["proposal_context"]
            if not isinstance(context, dict):
                raise TypeError
            task = decode_subscription_record(context["task"])
            frozen_envelope = decode_subscription_record(context["envelope"])
            producer_payload = payload["attempt"]
            if not isinstance(producer_payload, Mapping):
                raise TypeError
            producer = decode_subscription_record(producer_payload)
            launch = SubscriptionLaunchTerminalProof.model_validate(payload.get("launch_proof"))
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 4
                or canonical_digest(payload) != result.result_digest
                or not isinstance(task, LogicalTaskContract)
                or (task.run_id, task.task_id) != (run_id, call.subscription_task_id)
                or (attempt.run_id, attempt.task_row_id) != (run_id, task.task_id)
                or attempt.status != "terminal"
                or task_row.run_id != run_id
                or task_row.parent_task_id != task.parent_task_id
                or (
                    task.task_id != proposal.decision.task_id
                    if task.purpose is SpecialistPurpose.PRIMARY
                    else task.parent_task_id != proposal.decision.task_id
                )
                or not isinstance(frozen_envelope, ExecutionEnvelope)
                or frozen_envelope != envelope
                or not frozen_envelope.permits_route(task.purpose, task.route)
                or not isinstance(producer, AttemptIdentity)
                or (producer.run_id, producer.task_id, producer.attempt_id, producer.attempt_number)
                != (run_id, task.task_id, attempt.id, attempt.attempt_number)
                or canonical_digest(context["task"]) != attempt.task_digest
                or canonical_digest(context["envelope"]) != attempt.envelope_digest
                or context["route"] != attempt.route_payload
                or context["route"] != encode_subscription_record(task.route)
                or context["budget"] != encode_subscription_record(task.budget)
                or context["candidate_epoch"] != attempt.candidate_epoch
                or context["task_version"] != attempt.task_version
                or type(attempt.candidate_epoch) is not int
                or attempt.candidate_epoch > proposal.review.candidate_epoch
                or payload["telemetry"] != attempt.telemetry_payload
            ):
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise SubscriptionDecisionError("acceptance receipt producer proof differs") from None
        consumption = await session.get(
            SubscriptionAttemptConsumption,
            attempt.id,
            with_for_update=True,
            populate_existing=True,
        )
        launches = (
            await session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == attempt.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if (
            consumption is None
            or consumption.telemetry_payload != attempt.telemetry_payload
            or not launches_confirmed(
                launches, launch, require_decision=False, worker_identity=attempt.lease_owner
            )
        ):
            raise SubscriptionDecisionError("acceptance receipt producer is not proved stopped")
        rows = (
            await session.scalars(
                select(SubscriptionOperationBinding)
                .where(SubscriptionOperationBinding.durable_operation_id == identity)
                .limit(2)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(rows) != 1 or rows[0].receipt_payload is None:
            raise AcceptanceReceiptClaimError(ReceiptClaimErrorKind.AMBIGUOUS_CALLBACK, identity)
        binding = decode_subscription_record(rows[0].payload)
        receipt = rows[0].receipt_payload
        outcome = receipt.get("result")
        metadata = outcome.get("metadata") if isinstance(outcome, Mapping) else None
        required_metadata = {
            ToolName.GIT_DIFF: ("manifest_digest", "candidate_tree_digest"),
            ToolName.BUILD_RUN_NAMED_CHECK: ("command_result_digest",),
            ToolName.GIT_COMMIT: ("new_sha",),
        }.get(call.tool_name, ())
        if (
            not isinstance(binding, ToolCallBinding)
            or rows[0].attempt_id != attempt.id
            or rows[0].provider_call_key != binding.provider_call_key
            or binding.attempt_id != attempt.id
            or binding.durable_operation_id != identity
            or binding.tool_name is not call.tool_name
            # Commit audit arguments retain only message_digest. Terminal
            # verification separately checks the original request and message.
            or binding.arguments_digest
            != (
                call.request_digest
                if call.tool_name is ToolName.GIT_COMMIT
                else canonical_digest(call.normalized_arguments)
            )
            or call.subscription_purpose != task.purpose.value
            or call.policy_version != proposal.policy.version
            or call.resource_id != proposal.worktree.identity.worktree_name
            or not call.authorized
            or call.status is not ToolCallStatus.SUCCEEDED
            or call.completed_at is None
            or call.request_digest is None
            or call.invocation_schema_version != 1
            or receipt.get("accepted") is not True
            or not isinstance(outcome, Mapping)
            or outcome.get("tool_name") != call.tool_name.value
            or outcome.get("status") != "succeeded"
            or tuple(outcome.get("artifact_digests", ())) != call.artifact_digests
            # Persisted calls also contain repository-added lineage fields that
            # do not appear in the callback's tool-result metadata.
            or not isinstance(metadata, Mapping)
            or any(key not in metadata for key in required_metadata)
            or any(
                (call.result_metadata or {}).get(key) != value for key, value in metadata.items()
            )
            or outcome.get("operation_intent_id")
            != (str(call.operation_intent_id) if call.operation_intent_id else None)
        ):
            raise AcceptanceReceiptClaimError(
                ReceiptClaimErrorKind.CALLBACK_BINDING_DIFFERS, identity
            )
        if call.tool_name not in {
            ToolName.GIT_DIFF,
            ToolName.BUILD_RUN_NAMED_CHECK,
            ToolName.GIT_COMMIT,
        } or (
            call.tool_name is ToolName.GIT_DIFF
            and call.normalized_arguments != {"scope": "snapshot"}
        ):
            raise AcceptanceReceiptClaimError(ReceiptClaimErrorKind.UNSUPPORTED_TOOL, identity)
        sources.append(
            AcceptanceReceiptSource(
                call,
                task,
                canonical_digest(
                    {
                        "result_digest": result.result_digest,
                        "task_digest": attempt.task_digest,
                        "envelope_digest": attempt.envelope_digest,
                        "lease_owner": attempt.lease_owner,
                        "lease_generation": attempt.lease_generation,
                        "candidate_epoch": attempt.candidate_epoch,
                        "task_version": attempt.task_version,
                    }
                ),
                operation_evidence_digest(binding, receipt),
            )
        )
    return tuple(sources)


async def record_acceptance_receipts(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    proof: VerifiedAcceptanceReceipts,
) -> None:
    sources = await acceptance_receipt_sources(session, proposal)
    await verify_acceptance_receipt_evidence(session, proposal, proof, sources)
    result = await session.get(SubscriptionAttemptResult, proposal.attempt_id)
    assert result is not None and result.application_payload is not None
    if "receipt_verification" in result.application_payload:
        if result.application_payload["receipt_verification"] != proof.payload():
            raise SubscriptionDecisionError("acceptance receipt verification replay differs")
        return
    result.application_payload = {
        **result.application_payload,
        "receipt_verification": proof.payload(),
    }
    result.application_digest = canonical_digest(result.application_payload)
    await session.flush()


async def verify_acceptance_receipt_evidence(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    proof: VerifiedAcceptanceReceipts,
    sources: tuple[AcceptanceReceiptSource, ...],
) -> None:
    """Recheck retained source proof under the caller's run lock."""
    if (
        proof.result_digest != proposal.result_digest
        or proof.review_digest != canonical_digest(proposal.review.payload())
        or tuple(str(item.call.call_id) for item in proof.receipts)
        != proposal.decision.evidence_receipt_ids
    ):
        raise SubscriptionDecisionError("acceptance receipt proof source differs")
    intents: dict[UUID, str] = {}
    for source, item in zip(sources, proof.receipts, strict=True):
        call = source.call
        if (
            item.call.call_id != call.id
            or item.task_id != call.subscription_task_id
            or item.attempt_id != call.subscription_attempt_id
            or item.producer_digest != source.producer_digest
            or item.call.call_digest != terminal_call_digest(call)
            or item.call.receipt_digest != source.receipt_digest
            or item.tool_name is not call.tool_name
        ):
            raise SubscriptionDecisionError("acceptance receipt changed during verification")
        metadata = call.result_metadata or {}
        if call.tool_name is ToolName.GIT_DIFF:
            if (
                item.snapshot is None
                or item.call.terminal is not None
                or call.operation_intent_id is not None
                or call.normalized_arguments != {"scope": "snapshot"}
                or call.artifact_digests != (metadata.get("manifest_digest"),)
                or item.snapshot.base_sha != proposal.worktree.base_sha
                or item.snapshot.tree_digest != metadata.get("candidate_tree_digest")
                or item.matches_candidate != (item.snapshot == proposal.review.candidate)
            ):
                raise SubscriptionDecisionError("acceptance snapshot proof differs")
        else:
            terminal = item.call.terminal
            if (
                terminal is None
                or terminal.effect_id != call.id
                or terminal.call_digest != item.call.call_digest
                or call.operation_intent_id != call.id
                or call.id not in dict(terminal.intent_digests)
            ):
                raise SubscriptionDecisionError("acceptance terminal proof differs")
            for identity, digest in terminal.intent_digests:
                if identity in intents and intents[identity] != digest:
                    raise SubscriptionDecisionError("acceptance terminal intent conflicts")
                intents[identity] = digest
            if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                matches = (
                    metadata.get("candidate_tree_digest_before")
                    == proposal.review.candidate.tree_digest
                    and metadata.get("candidate_tree_digest_after")
                    == proposal.review.candidate.tree_digest
                )
                if (
                    item.command_name != call.normalized_arguments.get("command_name")
                    or item.command_result_digest != metadata.get("command_result_digest")
                    # Command results are parents of named-check receipts.
                    # The exact artifact closure is rechecked under lock below.
                    or item.command_result_digest not in dict(proof.artifact_proofs)
                    or item.matches_candidate != matches
                ):
                    raise SubscriptionDecisionError("acceptance named check proof differs")
            elif call.tool_name is ToolName.GIT_COMMIT:
                if item.commit_sha != metadata.get("new_sha") or item.matches_candidate:
                    raise SubscriptionDecisionError("acceptance commit proof differs")
            else:
                raise SubscriptionDecisionError("acceptance receipt proof tool differs")
    if intents:
        rows = (
            await session.scalars(
                select(OperationIntent)
                .where(OperationIntent.id.in_(intents))
                .order_by(OperationIntent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(rows) != len(intents) or any(
            row.run_id != proposal.decision.run_id
            or terminal_intent_digest(_intent_from_record(row)) != intents[row.id]
            for row in rows
        ):
            raise SubscriptionDecisionError("acceptance terminal evidence changed")
    if not await verify_artifact_closure(
        session,
        proposal.decision.run_id,
        tuple(digest for source in sources for digest in source.call.artifact_digests),
        proof.artifact_proofs,
    ):
        raise SubscriptionDecisionError("acceptance artifact evidence changed")
