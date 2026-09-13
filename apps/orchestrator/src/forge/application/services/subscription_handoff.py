"""Read-only historical child evidence verification; never candidate approval."""

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from uuid import UUID

from forge.application.adapters.named_check_receipts import decode_command_result
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_handoff import (
    HandoffCallProof,
    RejectedSubscriptionHandoff,
    VerifiedSubscriptionHandoff,
    artifact_descriptor_digest,
    handoff_claim_error,
    operation_evidence_digest,
)
from forge.application.ports.tool_recovery import TerminalEffectVerifier, terminal_call_digest
from forge.application.ports.tools import ToolCallRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitSnapshotFile, GitWorkingTreeSnapshot
from forge.application.services.subscription_receipt_artifacts import read_receipt_artifacts
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.subscription import (
    HandoffStatus,
    LogicalTaskContract,
    ReviewedTaskHandoff,
    SpecialistPurpose,
    TaskHandoff,
    encode_subscription_record,
)
from forge.domain.tool import ToolCallStatus, ToolName


class SubscriptionHandoffVerifier:
    def __init__(
        self,
        work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        artifact_store: ArtifactStore,
        terminal_verifier: TerminalEffectVerifier,
    ) -> None:
        self._work_factory, self._store, self._terminal = (
            work_factory,
            artifact_store,
            terminal_verifier,
        )

    async def assess(
        self,
        handoff: TaskHandoff,
        *,
        task: LogicalTaskContract,
        policy_version: int,
        worktree_id: str,
        base_sha: str,
        resource_id: str,
        current_snapshot: GitWorkingTreeSnapshot,
    ) -> VerifiedSubscriptionHandoff | RejectedSubscriptionHandoff | None:
        # Absence of verified evidence never authorizes a repair debit. Only a
        # definite mismatch against verified historical facts reaches rejection.
        proof = await self.verify(
            handoff,
            task=task,
            policy_version=policy_version,
            worktree_id=worktree_id,
            base_sha=base_sha,
            resource_id=resource_id,
        )
        if proof is None:
            return None
        try:
            observed = RejectedSubscriptionHandoff(
                proof,
                current_snapshot.candidate_tree_digest,
                handoff_output_digest(current_snapshot, task),
                current_snapshot.head_sha,
            )
        except TypeError, ValueError:
            return None
        if observed.reason(handoff) is not None:
            return observed
        return replace(proof, current_tree_digest=current_snapshot.candidate_tree_digest)

    async def verify(
        self,
        handoff: TaskHandoff,
        *,
        task: LogicalTaskContract,
        policy_version: int,
        worktree_id: str,
        base_sha: str,
        resource_id: str,
        current_snapshot: GitWorkingTreeSnapshot | None = None,
    ) -> VerifiedSubscriptionHandoff | None:
        """Caller supplies frozen task/context; caller must recheck proof under its locks."""
        try:
            return await self._verify(
                handoff, task, policy_version, worktree_id, base_sha, resource_id, current_snapshot
            )
        except KeyError, OSError, RuntimeError, TypeError, ValueError, RecursionError:
            return None

    async def _verify(
        self,
        handoff: TaskHandoff,
        task: LogicalTaskContract,
        policy_version: int,
        worktree_id: str,
        base_sha: str,
        resource_id: str,
        current_snapshot: GitWorkingTreeSnapshot | None,
    ) -> VerifiedSubscriptionHandoff | None:
        if (
            handoff.status is not HandoffStatus.COMPLETED
            or task.purpose is SpecialistPurpose.PRIMARY
            or (handoff.run_id, handoff.task_id) != (task.run_id, task.task_id)
            or type(policy_version) is not int
            or policy_version < 1
            or not worktree_id
            or not resource_id
            or handoff_claim_error(handoff, task) is not None
        ):
            return None
        ids = tuple(UUID(value) for value in handoff.evidence_receipt_ids)
        calls: dict[UUID, ToolCallRecord] = {}
        receipts: dict[UUID, str] = {}
        async with self._work_factory() as work:
            for identity in ids:
                call = await work.tool_calls.get(identity)
                evidence = await work.subscription.operation_evidence(
                    identity,
                    run_id=task.run_id,
                    task_id=task.task_id,
                    attempt_id=handoff.attempt_id,
                )
                if evidence is None:
                    return None
                binding, receipt = evidence
                result = receipt.get("result")
                failed_check = (
                    call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK
                    and call.status is ToolCallStatus.FAILED
                )
                if (
                    call.id != identity
                    or call.run_id != task.run_id
                    or call.subscription_task_id != task.task_id
                    or call.subscription_attempt_id != handoff.attempt_id
                    or call.subscription_purpose != task.purpose.value
                    or call.policy_version != policy_version
                    or call.resource_id != resource_id
                    or not call.authorized
                    or (call.status is not ToolCallStatus.SUCCEEDED and not failed_check)
                    or call.completed_at is None
                    or call.request_digest is None
                    or call.invocation_schema_version != 1
                    or binding.durable_operation_id != identity
                    or binding.attempt_id != handoff.attempt_id
                    or binding.tool_name is not call.tool_name
                    # Commit audit arguments retain only message_digest. The
                    # original request digest is bound again by terminal proof.
                    or binding.arguments_digest
                    != (
                        call.request_digest
                        if call.tool_name is ToolName.GIT_COMMIT
                        else canonical_digest(call.normalized_arguments)
                    )
                    or receipt.get("accepted") is not (not failed_check)
                    or not isinstance(result, Mapping)
                    or result.get("tool_name") != call.tool_name.value
                    or result.get("status") != call.status.value
                    or tuple(result.get("artifact_digests", ())) != call.artifact_digests
                    or result.get("operation_intent_id")
                    != (str(call.operation_intent_id) if call.operation_intent_id else None)
                ):
                    return None
                calls[identity], receipts[identity] = (
                    call,
                    operation_evidence_digest(binding, receipt),
                )
            await work.rollback()
        artifacts = await read_receipt_artifacts(
            self._work_factory,
            self._store,
            task.run_id,
            tuple(digest for call in calls.values() for digest in call.artifact_digests),
        )
        if artifacts is None:
            return None
        descriptors, blobs = artifacts
        snapshots = [
            call
            for call in calls.values()
            if call.tool_name is ToolName.GIT_DIFF
            and call.normalized_arguments == {"scope": "snapshot"}
        ]
        if len(snapshots) != 1:
            return None
        snapshot_call = snapshots[0]
        if (
            snapshot_call.operation_intent_id is not None
            or len(snapshot_call.artifact_digests) != 1
        ):
            return None
        manifest_digest = snapshot_call.artifact_digests[0]
        descriptor = descriptors[manifest_digest]
        if (
            descriptor.producer_type != "subscription_working_tree_snapshot"
            or descriptor.producer_id != snapshot_call.id
            or descriptor.parent_digests
            or descriptor.schema_version != 1
            or descriptor.media_type != "application/json"
            or descriptor.byte_count > 4 * 1024 * 1024
        ):
            return None
        snapshot = _decode_snapshot(
            blobs[manifest_digest], snapshot_call, worktree_id, base_sha, policy_version
        )
        metadata = snapshot_call.result_metadata or {}
        if (
            snapshot.candidate_tree_digest != handoff.candidate_tree_digest
            or metadata.get("manifest_digest") != manifest_digest
            or metadata.get("candidate_tree_digest") != snapshot.candidate_tree_digest
        ):
            return None
        output_digest = handoff_output_digest(snapshot, task)
        if current_snapshot is not None and (
            not isinstance(current_snapshot, GitWorkingTreeSnapshot)
            or handoff_output_digest(current_snapshot, task) != output_digest
            or handoff.candidate_commit is not None
            and current_snapshot.head_sha != handoff.candidate_commit
        ):
            return None
        owned_changes = tuple(
            path for path in snapshot.changed_paths if _owned(path, task.owned_paths)
        )
        if tuple(sorted(handoff.changed_paths)) != owned_changes:
            return None
        proofs = [
            HandoffCallProof(
                snapshot_call.id, terminal_call_digest(snapshot_call), receipts[snapshot_call.id]
            )
        ]
        checks = {item.receipt_id: item for item in handoff.check_results}
        latest = {item.command_name: item for item in handoff.check_results}
        previous_checks: dict[str, ToolCallRecord] = {}
        for ordered_claim in handoff.check_results:
            check_call = calls[UUID(ordered_claim.receipt_id)]
            previous = previous_checks.get(ordered_claim.command_name)
            if previous is not None and (
                previous.completed_at is None
                or previous.completed_at > check_call.started_at
                or check_call.completed_at is None
                or check_call.completed_at > snapshot_call.started_at
            ):
                return None
            previous_checks[ordered_claim.command_name] = check_call
        proved_checks: set[str] = set()
        checks_match_snapshot = True
        proved_commit = False
        for identity, call in calls.items():
            if identity == snapshot_call.id:
                continue
            terminal = await self._terminal.verify_terminal_effect(identity)
            if (
                terminal is None
                or terminal.effect_id != identity
                or terminal.call_digest != terminal_call_digest(call)
            ):
                return None
            metadata = call.result_metadata or {}
            if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
                claim = checks.get(str(identity))
                result_digest = metadata.get("command_result_digest")
                if (
                    claim is None
                    or not isinstance(result_digest, str)
                    or result_digest not in blobs
                ):
                    return None
                result = decode_command_result(blobs[result_digest])
                result_descriptor = descriptors[result_digest]
                if (
                    result_descriptor.producer_type != "command_result"
                    or result_descriptor.producer_id != identity
                    or result.evidence_digest != result_digest
                    or claim.output_digest != result_digest
                    or claim.command_name != result.command_name
                    or call.normalized_arguments.get("command_name") != result.command_name
                    or result.policy_version != policy_version
                    or claim.exit_code != result.exit_code
                    or claim.passed is not (call.status is ToolCallStatus.SUCCEEDED)
                    or result.timed_out
                    or result.stdout_truncated
                    or result.stderr_truncated
                    or claim.duration_ms != result.duration_ms
                ):
                    return None
                if latest[claim.command_name] == claim:
                    checks_match_snapshot = checks_match_snapshot and (
                        metadata.get("candidate_tree_digest_before")
                        == snapshot.candidate_tree_digest
                        and metadata.get("candidate_tree_digest_after")
                        == snapshot.candidate_tree_digest
                    )
                else:
                    before = metadata.get("candidate_tree_digest_before")
                    if (
                        not isinstance(before, str)
                        or len(before) != 64
                        or any(char not in "0123456789abcdef" for char in before)
                        or metadata.get("candidate_tree_digest_after") != before
                    ):
                        return None
                proved_checks.add(str(identity))
            elif call.tool_name is ToolName.GIT_COMMIT:
                # The terminal validator binds the prepared/published commit intents.
                if (
                    handoff.candidate_commit is None
                    or metadata.get("new_sha") != handoff.candidate_commit
                    or handoff.candidate_commit != snapshot.head_sha
                ):
                    return None
                proved_commit = True
            else:
                return None
            proofs.append(
                HandoffCallProof(identity, terminal.call_digest, receipts[identity], terminal)
            )
        # A typed independent reviewer identifies the observed candidate HEAD;
        # its read-only contract cannot produce a commit. The locked application
        # separately requires this report to match the selected closed candidate.
        observed_review_head = (
            task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW
            and isinstance(handoff, ReviewedTaskHandoff)
            and handoff.candidate_commit == snapshot.head_sha
        )
        if proved_checks != set(checks) or (
            handoff.candidate_commit is not None and not (proved_commit or observed_review_head)
        ):
            return None
        return VerifiedSubscriptionHandoff(
            task.run_id,
            task.task_id,
            handoff.attempt_id,
            snapshot_call.id,
            manifest_digest,
            snapshot.candidate_tree_digest,
            policy_version,
            canonical_digest(encode_subscription_record(task)),
            canonical_digest(encode_subscription_record(handoff)),
            tuple(proofs),
            tuple(
                (digest, artifact_descriptor_digest(descriptor))
                for digest, descriptor in sorted(descriptors.items())
            ),
            checks_match_snapshot,
            output_digest,
            None if current_snapshot is None else current_snapshot.candidate_tree_digest,
        )


def handoff_output_digest(snapshot: GitWorkingTreeSnapshot, task: LogicalTaskContract) -> str:
    """Compare complete owned outputs, or the whole tree for a task without scope.

    Base identity and scoped changed paths matter even when file bytes coincide.
    This observation is useful only while the dispatcher retains task ownership
    and rechecks its durable source and candidate epoch before application.
    """
    paths = [policy_path_key(item.path) for item in snapshot.files]
    if len(set(paths)) != len(paths):
        raise ValueError("snapshot contains aliased paths")
    changes = [policy_path_key(path) for path in snapshot.changed_paths]
    if len(set(changes)) != len(changes):
        raise ValueError("snapshot contains aliased changes")
    return canonical_digest(
        {
            "base_sha": snapshot.base_sha,
            "files": [
                [item.path, item.mode, item.content_digest, item.byte_count]
                for item in snapshot.files
                if not task.owned_paths or _owned(item.path, task.owned_paths)
            ],
            "changed_paths": [
                path
                for path in snapshot.changed_paths
                if not task.owned_paths or _owned(path, task.owned_paths)
            ],
        }
    )


def _owned(path: str, scopes: tuple[str, ...]) -> bool:
    key = policy_path_key(path)
    return any(
        key == policy_path_key(scope) or key.startswith(policy_path_key(scope) + "/")
        for scope in scopes
    )


def _decode_snapshot(
    data: bytes, call: ToolCallRecord, worktree_id: str, base_sha: str, policy_version: int
) -> GitWorkingTreeSnapshot:
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or type(value.get("files")) is not list
        or len(value["files"]) > 10_000
        or type(value.get("changed_paths")) is not list
        or len(value["changed_paths"]) > 20_000
    ):
        raise ValueError("snapshot schema is invalid")
    snapshot = GitWorkingTreeSnapshot(
        head_sha=value["head_sha"],
        base_sha=value["base_sha"],
        files=tuple(GitSnapshotFile(**item) for item in value["files"]),
        changed_paths=tuple(value["changed_paths"]),
    )
    expected = dict(
        snapshot.manifest(),
        run_id=str(call.run_id),
        task_id=str(call.subscription_task_id),
        attempt_id=str(call.subscription_attempt_id),
        tool_call_id=str(call.id),
        worktree_id=worktree_id,
        policy_version=policy_version,
    )
    if (
        snapshot.base_sha != base_sha
        or json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        != data
    ):
        raise ValueError("snapshot identity differs")
    return snapshot
