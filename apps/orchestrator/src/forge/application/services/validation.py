"""Assemble controller check outcomes into immutable delivery evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from forge.application.adapters.named_check_receipts import encode_command_result
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceKind,
    EvidenceSetDescriptor,
    ValidationEvidenceDraft,
    ValidationProjectionMember,
)
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.runner import CommandTerminalResult
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.evidence import (
    EvidenceStatus,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    encode_evidence_manifest,
)
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest


class ValidationError(RuntimeError):
    """Validation lacks complete, current controller evidence."""


class ValidationService:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._store = artifact_store

    async def publish(
        self,
        work: UnitOfWork,
        *,
        run_id: UUID,
        step_id: UUID,
        evidence_set_id: UUID,
        policy: ProjectPolicy,
        head_sha: str,
        results: Sequence[tuple[UUID, CommandTerminalResult]],
        prior_review_evidence_set_id: UUID | None = None,
    ) -> EvidenceSetDescriptor:
        """Project already reconciled receipts; caller owns authority and commit.

        The delivery controller fences its command and verifies the current HEAD
        before calling this method. Runner invocation and receipt reconciliation
        precede publication. A completed controller can contain failed checks;
        downstream decisions must inspect each member's status.
        """
        run = await work.runs.get_for_update(run_id)
        step = await work.controller_steps.get(run_id, step_id)
        commands = policy.required_checks
        if (
            run.state is not RunState.VALIDATING
            or run.project_id != policy.id
            or run.policy_version != policy.version
            or step is None
            or step.kind != "validate"
            or step.status is not ExecutionStatus.RUNNING
            or tuple(terminal.result.command_name for _, terminal in results)
            != tuple(command.name for command in commands)
            or len({result_id for result_id, _ in results}) != len(results)
        ):
            raise ValidationError("validation publication is not current or complete")

        members = []
        projections = []
        parents: set[str] = set()
        for command, (result_id, terminal) in zip(commands, results, strict=True):
            result = terminal.result
            if (
                result.kind is not command.kind
                or result.command_digest != command_spec_digest(command)
                or result.policy_version != policy.version
                or result.runner_mode is not policy.runner_mode
                or result.network_enabled != command.network_enabled
                or result.unsandboxed is not (policy.runner_mode is RunnerMode.TRUSTED_HOST)
            ):
                raise ValidationError("validation result does not match policy")
            try:
                artifact = await work.artifacts.get_by_digest(result.evidence_digest, run_id=run_id)
                data = await self._store.open_bytes(result.evidence_digest)
                expected_parents = tuple(sorted({result.stdout_digest, result.stderr_digest}))
                if (
                    artifact.producer_type != "command_result"
                    or artifact.producer_id != result_id
                    or artifact.media_type != "application/vnd.forge.command-result+json"
                    or artifact.schema_version != 1
                    or artifact.truncated
                    or artifact.byte_count != len(data)
                    or artifact.parent_digests != expected_parents
                    or data != encode_command_result(result)
                    or hashlib.sha256(data).hexdigest() != artifact.digest
                ):
                    raise ValidationError("validation result artifact is not bound")
                for digest in expected_parents:
                    output = await work.artifacts.get_by_digest(digest, run_id=run_id)
                    if (
                        output.producer_type != "command_output"
                        or output.producer_id != run_id
                        or not await self._store.verify(digest)
                    ):
                        raise ValidationError("validation output artifact is not bound")
            except ValidationError:
                raise
            except Exception:  # noqa: BLE001 - artifact failures must not expose storage details
                raise ValidationError("validation result artifact is unavailable") from None

            status = (
                EvidenceStatus.CANCELLED
                if terminal.caller_cancelled
                else EvidenceStatus.FAILED
                if result.timed_out or result.exit_code != 0
                else EvidenceStatus.PASSED
            )
            if status is EvidenceStatus.FAILED and result.exit_code == 0:
                raise ValidationError("timed out validation cannot claim a successful exit")
            member = ValidationEvidenceMember(
                result_id=result_id,
                check_name=command.name,
                command_name=command.name,
                command_version=policy.version,
                command_digest=result.command_digest,
                command_result_digest=result.evidence_digest,
                stdout_digest=result.stdout_digest,
                stderr_digest=result.stderr_digest,
                status=status,
                exit_code=result.exit_code,
                started_at=result.started_at,
                completed_at=result.started_at + timedelta(milliseconds=result.duration_ms),
            )
            members.append(member)
            if artifact.artifact_id is None:
                raise ValidationError("validation result has no persisted identity")
            projections.append(ValidationProjectionMember(member, artifact.artifact_id))
            parents.update((result.evidence_digest, *expected_parents))

        if prior_review_evidence_set_id is not None:
            prior = await work.evidence.get_by_id(prior_review_evidence_set_id, run_id=run_id)
            if prior.kind is not EvidenceKind.REVIEW or prior.policy_version != policy.version:
                raise ValidationError("validation prior review is invalid")
            parents.add(prior.manifest_digest)
        manifest = ValidationEvidenceManifest(
            evidence_set_id=evidence_set_id,
            run_id=run_id,
            step_id=step_id,
            policy_version=policy.version,
            head_sha=head_sha,
            members=tuple(members),
            prior_review_evidence_set_id=prior_review_evidence_set_id,
        )
        wire = encode_evidence_manifest(manifest)
        stored = await self._store.put_bytes(
            wire,
            media_type="application/vnd.forge.evidence-manifest+json",
        )
        if stored.digest != hashlib.sha256(wire).hexdigest() or stored.byte_count != len(wire):
            raise ValidationError("validation manifest storage differs")
        artifact = await work.artifacts.record(
            stored,
            run_id=run_id,
            producer_type="evidence_set",
            producer_id=evidence_set_id,
            parent_digests=tuple(sorted(parents)),
        )
        evidence = await work.evidence.record_set(
            ValidationEvidenceDraft(manifest, tuple(projections)),
            CanonicalEvidenceArtifact(artifact, manifest, wire),
        )
        await work.controller_steps.finalize(
            run_id,
            step_id,
            ExecutionStatus.SUCCEEDED,
            output_artifact_id=artifact.artifact_id,
            outcome="validation results recorded",
        )
        return evidence
