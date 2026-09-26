"""Locked revalidation of immutable subscription handoff evidence."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_handoff import (
    VerifiedSubscriptionHandoff,
    artifact_descriptor_digest,
    operation_evidence_digest,
)
from forge.application.ports.tool_recovery import terminal_call_digest, terminal_intent_digest
from forge.domain.subscription import ToolCallBinding, decode_subscription_record
from forge.domain.tool import ToolCallStatus, ToolName
from forge.persistence.models.execution import (
    Artifact,
    ArtifactLineage,
    ArtifactLineageParent,
    OperationIntent,
    ToolCall,
)
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionOperationBinding
from forge.persistence.repositories.artifacts import _descriptor_from_rows
from forge.persistence.repositories.operations import _intent_from_record
from forge.persistence.repositories.tool_calls import _record_from_row


class PostgresSubscriptionHandoffEvidence:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def verify(self, proof: VerifiedSubscriptionHandoff) -> bool:
        """Lock the source rows and reject any proof that no longer matches."""
        try:
            return await self._verify(proof)
        except TypeError, ValueError, KeyError:
            return False

    async def _verify(self, proof: VerifiedSubscriptionHandoff) -> bool:
        if not 1 <= len(proof.call_proofs) <= 128 or not 1 <= len(proof.artifact_proofs) <= 256:
            return False
        ids = [item.call_id for item in proof.call_proofs]
        digests = dict(proof.artifact_proofs)
        if (
            len(ids) != len(set(ids))
            or proof.snapshot_call_id not in ids
            or len(digests) != len(proof.artifact_proofs)
            or proof.manifest_digest not in digests
        ):
            return False
        run = (
            await self._session.execute(
                select(Run.id).where(Run.id == proof.run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return False
        rows = (
            (
                await self._session.execute(
                    select(ToolCall)
                    .where(ToolCall.id.in_(ids))
                    .order_by(ToolCall.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != len(ids):
            return False
        calls = {row.id: _record_from_row(row) for row in rows}
        bindings = (
            await self._session.scalars(
                select(SubscriptionOperationBinding)
                .where(
                    SubscriptionOperationBinding.durable_operation_id.in_(ids),
                    SubscriptionOperationBinding.attempt_id == proof.attempt_id,
                )
                .order_by(SubscriptionOperationBinding.id)
                .limit(129)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(bindings) != len(ids):
            return False
        receipts = {row.durable_operation_id: row for row in bindings}
        if len(receipts) != len(ids):
            return False
        intent_digests: dict[UUID, str] = {}
        for item in proof.call_proofs:
            call = calls.get(item.call_id)
            if (
                call is None
                or call.run_id != proof.run_id
                or call.subscription_task_id != proof.task_id
                or call.subscription_attempt_id != proof.attempt_id
                or call.policy_version != proof.policy_version
                or (
                    call.status is not ToolCallStatus.SUCCEEDED
                    and not (
                        call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK
                        and call.status is ToolCallStatus.FAILED
                    )
                )
                or not call.authorized
                or terminal_call_digest(call) != item.call_digest
            ):
                return False
            row = receipts.get(item.call_id)
            if (
                row is None
                or row.receipt_payload is None
                or row.receipt_payload.get("accepted")
                is not (call.status is ToolCallStatus.SUCCEEDED)
            ):
                return False
            binding = decode_subscription_record(row.payload)
            if (
                not isinstance(binding, ToolCallBinding)
                or binding.attempt_id != proof.attempt_id
                or binding.durable_operation_id != item.call_id
                or binding.tool_name is not call.tool_name
                or operation_evidence_digest(binding, row.receipt_payload) != item.receipt_digest
            ):
                return False
            if item.call_id == proof.snapshot_call_id:
                if (
                    call.tool_name is not ToolName.GIT_DIFF
                    or call.normalized_arguments != {"scope": "snapshot"}
                    or call.operation_intent_id is not None
                    or item.terminal is not None
                    or call.artifact_digests != (proof.manifest_digest,)
                ):
                    return False
            else:
                terminal = item.terminal
                if (
                    call.tool_name not in {ToolName.BUILD_RUN_NAMED_CHECK, ToolName.GIT_COMMIT}
                    or terminal is None
                    or terminal.effect_id != item.call_id
                    or terminal.call_digest != item.call_digest
                    or call.operation_intent_id != item.call_id
                    or item.call_id not in dict(terminal.intent_digests)
                ):
                    return False
                for identity, digest in terminal.intent_digests:
                    if identity in intent_digests and intent_digests[identity] != digest:
                        return False
                    intent_digests[identity] = digest
        if intent_digests:
            intents = (
                await self._session.scalars(
                    select(OperationIntent)
                    .where(OperationIntent.id.in_(intent_digests))
                    .order_by(OperationIntent.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
            if len(intents) != len(intent_digests) or any(
                row.run_id != proof.run_id
                or terminal_intent_digest(_intent_from_record(row)) != intent_digests[row.id]
                for row in intents
            ):
                return False
        return await verify_artifact_closure(
            self._session,
            proof.run_id,
            tuple(digest for call in calls.values() for digest in call.artifact_digests),
            proof.artifact_proofs,
        )


async def verify_artifact_closure(
    session: AsyncSession,
    run_id: UUID,
    roots: tuple[str, ...],
    artifact_proofs: tuple[tuple[str, str], ...],
) -> bool:
    """Recheck the exact, bounded run-scoped descriptor and parent closure."""
    digests = dict(artifact_proofs)
    if not 1 <= len(digests) <= 256 or len(digests) != len(artifact_proofs):
        return False
    artifacts = (
        await session.execute(
            select(Artifact, ArtifactLineage)
            .join(
                ArtifactLineage,
                ArtifactLineage.artifact_id == Artifact.id,
            )
            .where(Artifact.digest.in_(digests), ArtifactLineage.run_id == run_id)
            .order_by(Artifact.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(artifacts) != len(digests):
        return False
    contents = {content.id: content for content, _ in artifacts}
    edges = (
        await session.scalars(
            select(ArtifactLineageParent)
            .where(
                ArtifactLineageParent.artifact_id.in_(contents),
                ArtifactLineageParent.run_id == run_id,
            )
            .order_by(ArtifactLineageParent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    parents: dict[UUID, list[str]] = {identity: [] for identity in contents}
    for edge in edges:
        if edge.parent_artifact_id not in contents:
            return False
        parents[edge.artifact_id].append(contents[edge.parent_artifact_id].digest)
    parent_digests = {}
    for content, lineage in artifacts:
        descriptor = _descriptor_from_rows(content, lineage, tuple(sorted(parents[content.id])))
        if artifact_descriptor_digest(descriptor) != digests[content.digest]:
            return False
        parent_digests[content.digest] = descriptor.parent_digests
    # Require the complete referenced closure, without unrelated extra proofs.
    pending = list(roots)
    reached = set()
    while pending:
        digest = pending.pop()
        if digest not in parent_digests:
            return False
        if digest not in reached:
            reached.add(digest)
            pending.extend(parent_digests[digest])
    return reached == set(digests)
