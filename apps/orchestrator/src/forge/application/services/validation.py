"""Assemble controller check outcomes into immutable delivery evidence."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from forge.application.adapters.controller_check import (
    ControllerCheckOperationAdapter,
    controller_check_request,
)
from forge.application.adapters.named_check import NamedCheckCancellation
from forge.application.adapters.named_check_receipts import (
    decode_command_result,
    encode_command_result,
)
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import (
    CanonicalEvidenceArtifact,
    EvidenceKind,
    EvidenceSetDescriptor,
    ValidationEvidenceDraft,
    ValidationProjectionMember,
)
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.runner import CommandTerminalResult, WorktreeRunnerFactoryPort
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.recovery import OperationExecutor
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    ValidationEvidenceMember,
    decode_evidence_manifest,
    encode_evidence_manifest,
)
from forge.domain.operation import OperationIntent, OperationOutcome, OperationStatus
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.validation import command_spec_digest


class ValidationError(RuntimeError):
    """Validation lacks complete, current controller evidence."""


async def _fence_command(command: CommandEnvelope, work: UnitOfWork) -> None:
    fenced = await work.commands.assert_current_lease(command)
    if (
        fenced.command_type != command.command_type
        or fenced.idempotency_key != command.idempotency_key
        or fenced.payload != command.payload
        or fenced.payload_schema_version != command.payload_schema_version
        or fenced.expected_run_version != command.expected_run_version
        or fenced.actor_id != command.actor_id
    ):
        raise CommandRecoveryRequired("validation delivery does not match its lease")


def validation_command_binding(command: CommandEnvelope) -> tuple[int, UUID | None]:
    """Decode the closed validation payload shared by execution and decisions."""
    attempt = command.payload.get("semantic_attempt")
    prior = command.payload.get("prior_review_evidence_set_id")
    if (
        command.command_type != "validate"
        or command.payload_schema_version != 1
        or type(attempt) is not int
        or attempt < 1
        or command.idempotency_key != f"{command.run_id}:validate:{attempt}"
        or set(command.payload) - {"semantic_attempt", "prior_review_evidence_set_id"}
    ):
        raise CommandRecoveryRequired("validation command authority is invalid")
    prior_id = None
    if "prior_review_evidence_set_id" in command.payload:
        try:
            prior_id = UUID(prior) if isinstance(prior, str) else None
        except ValueError:
            raise CommandRecoveryRequired("validation prior review identifier is invalid") from None
        if prior_id is None or not prior_id.int or str(prior_id) != prior:
            raise CommandRecoveryRequired("validation prior review identifier is invalid")
    return attempt, prior_id


class ValidationService:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        uow_factory: Callable[[], UnitOfWork] | None = None,
        operation_executor: OperationExecutor | None = None,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort] | None = None,
        runner_factory: WorktreeRunnerFactoryPort | None = None,
        environment_resolver: Callable[
            [RunSnapshot, ProjectPolicy, ManagedWorktree], Awaitable[Mapping[str, str]]
        ]
        | None = None,
        approved_plans: ApprovedPlanLoader | None = None,
    ) -> None:
        self._store = artifact_store
        self._uow_factory = uow_factory
        self._executor = operation_executor
        self._git_factory = git_factory
        self._runner_factory = runner_factory
        self._environment_resolver = environment_resolver
        self._approved_plans = approved_plans or ApprovedPlanLoader(artifact_store)

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> EvidenceSetDescriptor:
        """Admit one exact validation delivery before any check runner is used."""

        if (
            self._git_factory is None
            or self._runner_factory is None
            or self._environment_resolver is None
            or self._uow_factory is None
            or self._executor is None
        ):
            raise ValidationError("validation execution is not configured")
        await _fence_command(command, work)
        attempt, prior_review_id = validation_command_binding(command)
        approved = await self._approved_plans.load(work, command.run_id)
        if command.actor_id != approved.approval_actor_id:
            raise CommandRecoveryRequired("validation command actor is not approved")
        run = approved.run
        if prior_review_id is not None:
            prior_review = await work.evidence.get_by_id(prior_review_id, run_id=run.id)
            wire = await self._store.open_bytes(prior_review.manifest_digest)
            manifest = decode_evidence_manifest(wire)
            if (
                prior_review.kind is not EvidenceKind.REVIEW
                or prior_review.policy_version != approved.policy.version
                or prior_review.manifest_digest != hashlib.sha256(wire).hexdigest()
                or prior_review.manifest_byte_count != len(wire)
                or not isinstance(manifest, ReviewEvidenceManifest)
                or manifest.evidence_set_id != prior_review_id
                or manifest.run_id != run.id
                or manifest.policy_version != prior_review.policy_version
                or manifest.head_sha != prior_review.head_sha
                or manifest.producer_execution_id != prior_review.producer_execution_id
                or manifest.validation_evidence_set_id != prior_review.validation_evidence_set_id
            ):
                raise CommandRecoveryRequired("validation prior review evidence differs")
        if run.state is not RunState.VALIDATING or command.expected_run_version != run.version:
            raise CommandRecoveryRequired("validation run is not current")
        if run.worktree_path is None or run.branch_name is None or run.base_sha is None:
            raise CommandRecoveryRequired("validation run has no worktree binding")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, approved.policy.database.enabled
        )
        worktree = ManagedWorktree(
            identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha
        )
        git = self._git_factory(approved.policy)
        head_sha = git.head_sha(worktree)
        if git.inspect_worktree(identity, run.base_sha) != worktree or not git.is_ancestor(
            worktree
        ):
            raise CommandRecoveryRequired("validation worktree head is not approved")
        step_id = uuid5(NAMESPACE_URL, f"forge:validate:{command.id}")
        evidence_id = uuid5(step_id, "validation-evidence")
        existing_step = await work.controller_steps.get(run.id, step_id)
        if existing_step is None and attempt != await work.controller_steps.next_attempt(
            run.id, "validate"
        ):
            raise CommandRecoveryRequired("validation semantic attempt is not next")
        step = await work.controller_steps.admit(run.id, step_id, "validate", attempt)
        binding = {
            "command_id": str(command.id),
            "step_id": str(step_id),
            "head_sha": head_sha,
            "policy_version": approved.policy.version,
            "evidence_set_id": str(evidence_id),
            "checks": tuple(command_spec_digest(spec) for spec in approved.policy.required_checks),
        }
        if prior_review_id is not None:
            binding["prior_review_evidence_set_id"] = str(prior_review_id)
        if step.is_new:
            await work.events.append(
                RunEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type="run.validation_started",
                    payload=binding,
                    actor_class="worker",
                )
            )
        else:
            events = [
                event
                for event in await work.events.list_after(run.id, 0)
                if event.event_type == "run.validation_started"
                and event.payload.get("step_id") == str(step_id)
            ]
            if len(events) != 1 or events[0].payload != binding:
                raise CommandRecoveryRequired("validation binding differs from admission")
            if step.status not in {ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED}:
                raise CommandRecoveryRequired("validation terminal replay requires verification")
        await work.commit()
        environment = (
            {}
            if step.status is ExecutionStatus.SUCCEEDED
            else await self._environment_resolver(run, approved.policy, worktree)
        )
        results = []
        for spec in approved.policy.required_checks:
            await _fence_command(command, work)
            current = await self._approved_plans.load(work, run.id)
            if (
                current.run != run
                or current.approval_id != approved.approval_id
                or git.head_sha(worktree) != head_sha
            ):
                raise CommandRecoveryRequired("validation authority changed before check")
            values = {
                key: value for key, value in environment.items() if key in spec.environment_keys
            }
            result_id = uuid5(step_id, spec.name)
            prior = await work.operations.get_by_idempotency_key(
                f"{run.id}:controller-check:{step_id}:{spec.name}"
            )
            if prior is not None:
                owner = None
                if prior.status is not OperationStatus.SUCCEEDED:
                    if prior.status is OperationStatus.FAILED:
                        raise CommandRecoveryRequired("validation check is terminally failed")
                    owner = f"forge-validate-recovery-{uuid4().hex}"
                    claim = await work.operations.claim_for_recovery(
                        prior.id, owner_id=owner, lease_seconds=30
                    )
                    if not claim.acquired:
                        raise CommandRecoveryRequired("validation check still has an active owner")
                    prior = claim.intent
                    await work.commit()
                recovery = ControllerCheckOperationAdapter.for_recovery(
                    run_id=run.id,
                    step_id=step_id,
                    result_id=result_id,
                    worktree=worktree,
                    policy=approved.policy,
                    command_name=spec.name,
                    head_sha=head_sha,
                    artifacts=work.artifacts,
                    artifact_store=self._store,
                )
                try:
                    recovered = await recovery.reconcile(prior)
                except Exception:  # noqa: BLE001 - uncertain persisted proof requires operator recovery
                    raise CommandRecoveryRequired("validation receipt is unavailable") from None
                if (
                    recovered.status is not OperationStatus.SUCCEEDED
                    or "command_result_digest" not in recovered.payload
                    or (
                        prior.status is OperationStatus.SUCCEEDED
                        and prior.outcome != recovered.payload
                    )
                ):
                    raise CommandRecoveryRequired("validation receipt cannot establish the result")
                await _fence_command(command, work)
                latest = await self._approved_plans.load(work, run.id)
                if latest.run != run or git.head_sha(worktree) != head_sha:
                    raise CommandRecoveryRequired("validation authority changed during recovery")
                if owner is not None:
                    await work.operations.complete(prior.id, recovered, owner_id=owner)
                await work.commit()
                terminal = CommandTerminalResult(
                    result=decode_command_result(
                        await self._store.open_bytes(
                            str(recovered.payload["command_result_digest"])
                        )
                    ),
                    caller_cancelled=recovered.payload["caller_cancelled"] is True,
                )
                if terminal.caller_cancelled:
                    raise CommandRecoveryRequired("validation check was cancelled")
                results.append((result_id, terminal))
                continue
            if step.status is ExecutionStatus.SUCCEEDED:
                raise CommandRecoveryRequired("completed validation has a missing check intent")
            request = controller_check_request(
                run_id=run.id,
                step_id=step_id,
                result_id=result_id,
                worktree=worktree,
                policy=approved.policy,
                command_name=spec.name,
                head_sha=head_sha,
                environment=values,
            )
            intent = await work.operations.begin(
                run_id=run.id,
                operation_type=request.kind,
                idempotency_key=request.idempotency_key,
                request_digest=request.request_digest,
                request_payload=request.request_payload,
                execution_owner=f"forge-validate-{uuid4().hex}",
                execution_lease_seconds=30,
            )
            if not intent.is_new:
                raise CommandRecoveryRequired("validation check requires receipt recovery")
            await work.commit()
            cancellation = NamedCheckCancellation()

            async def effect(
                intent: OperationIntent = intent,
                result_id: UUID = result_id,
                spec: CommandSpec = spec,
                values: Mapping[str, str] = values,
                cancellation: NamedCheckCancellation = cancellation,
            ) -> OperationOutcome:
                assert self._uow_factory is not None and self._executor is not None
                async with self._uow_factory() as execution:
                    adapter = ControllerCheckOperationAdapter(
                        run_id=run.id,
                        step_id=step_id,
                        result_id=result_id,
                        worktree=worktree,
                        policy=approved.policy,
                        command_name=spec.name,
                        head_sha=head_sha,
                        artifacts=execution.artifacts,
                        artifact_store=self._store,
                        controlled_git=git,
                        runner_factory=self._runner_factory,
                        environment=values,
                        cancellation=cancellation,
                    )
                    outcome = await self._executor.invoke_admitted(intent, adapter)
                    await execution.commit()
                    return outcome

            task = asyncio.create_task(effect())
            cancelled = False
            while True:
                try:
                    outcome = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancelled = True
                    cancellation.request()
                except Exception:  # noqa: BLE001 - never retry an uncertain runner effect
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise CommandRecoveryRequired("validation check requires recovery") from None
            if cancelled:
                raise asyncio.CancelledError
            if (
                outcome.status is not OperationStatus.SUCCEEDED
                or "command_result_digest" not in outcome.payload
            ):
                raise CommandRecoveryRequired("validation check has no terminal result")
            await _fence_command(command, work)
            current = await self._approved_plans.load(work, run.id)
            if current.run != run or git.head_sha(worktree) != head_sha:
                raise CommandRecoveryRequired("validation authority changed after check")
            await work.operations.complete(intent.id, outcome, owner_id=intent.execution_owner)
            await work.commit()
            result = decode_command_result(
                await self._store.open_bytes(str(outcome.payload["command_result_digest"]))
            )
            results.append(
                (
                    result_id,
                    CommandTerminalResult(
                        result=result, caller_cancelled=outcome.payload["caller_cancelled"] is True
                    ),
                )
            )
            if outcome.payload["caller_cancelled"] is True:
                raise CommandRecoveryRequired("validation check was cancelled")
        await _fence_command(command, work)
        current = await self._approved_plans.load(work, run.id)
        if current.run != run or git.head_sha(worktree) != head_sha:
            raise CommandRecoveryRequired("validation authority changed before publication")
        evidence = await self.publish(
            work,
            run_id=run.id,
            step_id=step_id,
            evidence_set_id=evidence_id,
            policy=approved.policy,
            head_sha=head_sha,
            results=results,
            prior_review_evidence_set_id=prior_review_id,
        )
        await _fence_command(command, work)
        current = await self._approved_plans.load(work, run.id)
        if (
            current.run != run
            or current.approval_id != approved.approval_id
            or git.head_sha(worktree) != head_sha
        ):
            raise CommandRecoveryRequired("validation authority changed during publication")
        await work.commit()
        return evidence

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
            or step.status not in {ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED}
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
        if step.status is ExecutionStatus.SUCCEEDED:
            existing = await work.evidence.get_by_id(evidence_set_id, run_id=run_id)
            if (
                existing.manifest_digest != hashlib.sha256(wire).hexdigest()
                or existing.step_id != step_id
                or existing.manifest_artifact_id != step.output_artifact_id
                or await self._store.open_bytes(existing.manifest_digest) != wire
            ):
                raise ValidationError("completed validation evidence differs")
            return existing
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
        by_result_id = {projection.member.result_id: projection for projection in projections}
        evidence = await work.evidence.record_set(
            ValidationEvidenceDraft(
                manifest, tuple(by_result_id[member.result_id] for member in manifest.members)
            ),
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
