"""Read-only projection of legacy validation and controlled subscription checks."""

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, literal, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy, scoped_command
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, SpecialistPurpose
from forge.domain.tool import ToolName
from forge.domain.validation import command_spec_digest
from forge.persistence.models import (
    Artifact,
    ArtifactLineage,
    ArtifactLineageParent,
    EvidenceSet,
    OperationIntent,
    Run,
    Step,
    ToolCall,
    ValidationResult,
)
from forge.persistence.models.subscription import SubscriptionAttempt

_NAMED_TOOL = ToolName.BUILD_RUN_NAMED_CHECK
_RESULT_MEDIA = "application/vnd.forge.command-result+json"
_RECEIPT_MEDIA = "application/vnd.forge.named-check-receipt+json"
_OUTPUT_MEDIA = "application/vnd.forge.command-output+json"
CHECK_FIELDS = (
    "id",
    "name",
    "command_name",
    "command_version",
    "status",
    "exit_code",
    "output_artifact_digest",
    "completed_at",
    "head_sha",
    "source",
    "subscription_task_id",
    "subscription_attempt_id",
    "stderr_artifact_digest",
)


async def local_checks(
    session: AsyncSession,
    run: Run,
    policy: ProjectPolicy,
    offset: int,
    limit: int,
    *,
    newest_first: bool = False,
) -> tuple[list[dict[str, object]], bool]:
    """Page a shared execution index; invalid authority never occupies a visible row."""
    request = OperationIntent.request_payload
    metadata = ToolCall.result_metadata
    tool_id = cast(ToolCall.id, String)
    subscription = (
        select(ToolCall.id, ToolCall.started_at, literal("subscription").label("source"))
        .join(
            OperationIntent,
            and_(OperationIntent.id == ToolCall.id, OperationIntent.run_id == run.id),
        )
        .join(
            SubscriptionAttempt,
            and_(
                SubscriptionAttempt.id == ToolCall.subscription_attempt_id,
                SubscriptionAttempt.task_row_id == ToolCall.subscription_task_id,
                SubscriptionAttempt.run_id == run.id,
            ),
        )
        .where(
            ToolCall.run_id == run.id,
            ToolCall.authorized.is_(True),
            ToolCall.tool_name == _NAMED_TOOL.value,
            ToolCall.agent_execution_id.is_(None),
            ToolCall.arguments_schema_version == 1,
            ToolCall.result_metadata_schema_version == 1,
            OperationIntent.operation_kind == "named_check",
            OperationIntent.idempotency_key == literal("named_check:") + tool_id,
            OperationIntent.request_schema_version == 1,
            request["run_id"].as_string() == str(run.id),
            request["project_id"].as_string() == str(run.project_id),
            request["tool_call_id"].as_string() == tool_id,
            request["subscription_task_id"].as_string()
            == cast(ToolCall.subscription_task_id, String),
            request["subscription_attempt_id"].as_string()
            == cast(ToolCall.subscription_attempt_id, String),
            request["subscription_purpose"].as_string() == ToolCall.subscription_purpose,
            metadata["operation_intent_id"].as_string() == tool_id,
            metadata["correlation_id"].as_string() == tool_id,
            metadata["resource_id"].as_string() == request["worktree_id"].as_string(),
        )
    )
    index = union_all(
        select(
            ValidationResult.id, ValidationResult.started_at, literal("validation").label("source")
        ).where(ValidationResult.run_id == run.id),
        subscription,
    ).subquery()
    ordering = (index.c.started_at, index.c.id, index.c.source)
    query = select(index).order_by(*(value.desc() if newest_first else value for value in ordering))
    items: list[dict[str, object]] = []
    scanned = visible = 0
    # Pages are bounded even when malformed historical authority is skipped.
    while len(items) <= limit:
        rows = (await session.execute(query.offset(scanned).limit(100))).all()
        legacy = await _legacy_items(
            session, run, policy, [row.id for row in rows if row.source == "validation"]
        )
        subscription_ids = [row.id for row in rows if row.source == "subscription"]
        if subscription_ids:
            subscriptions = (
                await session.execute(
                    select(ToolCall, OperationIntent, SubscriptionAttempt)
                    .join(OperationIntent, OperationIntent.id == ToolCall.id)
                    .join(
                        SubscriptionAttempt,
                        SubscriptionAttempt.id == ToolCall.subscription_attempt_id,
                    )
                    .where(ToolCall.id.in_(subscription_ids))
                )
            ).all()
        else:
            subscriptions = []
        artifacts, parents = await _artifacts(session, run.id, [op for _, op, _ in subscriptions])
        named = {
            call.id: _subscription_item(run, policy, call, op, attempt, artifacts, parents)
            for call, op, attempt in subscriptions
        }
        for row in rows:
            item = legacy.get(row.id) if row.source == "validation" else named.get(row.id)
            if item is None:
                continue
            if visible >= offset:
                items.append(item)
            visible += 1
            if len(items) > limit:
                return items[:limit], True
        scanned += len(rows)
        if len(rows) < 100:
            break
    return items, False


async def _legacy_items(
    session: AsyncSession,
    run: Run,
    policy: ProjectPolicy,
    check_ids: list[UUID],
) -> dict[UUID, dict[str, object]]:
    if not check_ids:
        return {}
    evidence = (
        select(EvidenceSet.head_sha, EvidenceSet.manifest_artifact_id)
        .where(
            EvidenceSet.run_id == run.id,
            EvidenceSet.kind == "validation",
            EvidenceSet.step_id == ValidationResult.step_id,
        )
        .order_by(EvidenceSet.created_at.desc(), EvidenceSet.id.desc())
        .limit(1)
        .correlate(ValidationResult)
        .lateral()
    )
    output = aliased(Artifact)
    rows = (
        await session.execute(
            select(
                ValidationResult, Step.attempt, evidence.c.head_sha, Artifact.digest, output.digest
            )
            .outerjoin(Step, and_(Step.id == ValidationResult.step_id, Step.run_id == run.id))
            .outerjoin(evidence, true())
            .outerjoin(Artifact, Artifact.id == evidence.c.manifest_artifact_id)
            .outerjoin(output, output.id == ValidationResult.output_artifact_id)
            .where(ValidationResult.id.in_(check_ids), ValidationResult.run_id == run.id)
        )
    ).all()
    return {
        check.id: {
            "id": check.id,
            "name": check.check_name,
            "command_name": check.command_name,
            "command_version": check.command_version,
            "status": check.status,
            "exit_code": check.exit_code,
            "output_artifact_digest": output_digest,
            "stderr_artifact_digest": None,
            "completed_at": check.completed_at,
            "head_sha": head,
            "step_id": check.step_id,
            "attempt": attempt,
            "started_at": check.started_at,
            "duration_ms": int((check.completed_at - check.started_at).total_seconds() * 1000)
            if check.completed_at
            else None,
            "evidence_digest": digest,
            "configured_runner_mode": policy.runner_mode.value,
            "source": "validation",
            "subscription_task_id": None,
            "subscription_attempt_id": None,
        }
        for check, attempt, head, digest, output_digest in rows
    }


def _subscription_item(
    run: Run,
    policy: ProjectPolicy,
    call: ToolCall,
    operation: OperationIntent,
    attempt: SubscriptionAttempt,
    artifacts: dict[str, tuple[Artifact, ArtifactLineage]],
    parents: dict[UUID, set[UUID]],
) -> dict[str, object] | None:
    if call.subscription_purpose is None:
        return None
    request, metadata = operation.request_payload, call.result_metadata or {}
    try:
        purpose = SpecialistPurpose(call.subscription_purpose)
        command = next(item for item in policy.commands if item.name == request.get("command_name"))
        if "scoped_paths" in request:
            command = scoped_command(command, request["scoped_paths"])
        elif command.path_scope is not None:
            return None
        if (
            attempt is None
            or _NAMED_TOOL not in SPECIALIST_ALLOWED_TOOLS[purpose]
            or request.get("authority_schema_version") != 2
            or request.get("protocol_version") != 1
            or request.get("policy_version") != policy.version
            or metadata.get("policy_version") != policy.version
            or metadata.get("invocation_schema_version") != 1
            or request.get("command_digest") != command_spec_digest(command)
            or request.get("kind") != command.kind.value
            or canonical_digest(request) != operation.request_digest
            or call.normalized_arguments != {"command_name": command.name}
            or canonical_digest(call.normalized_arguments) != metadata.get("request_digest")
        ):
            return None
    except TypeError, ValueError, StopIteration:
        return None
    head = request.get("head_sha")
    head = head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) else None
    status = "UNKNOWN"
    if call.status in {"PENDING", "RUNNING"} and operation.status == "PENDING":
        status = call.status
    outcome = operation.outcome_payload or {}
    terminal = _terminal_evidence(run.id, call, operation, metadata, outcome, artifacts, parents)
    exit_code: int | None = None
    duration: int | None = None
    evidence: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    if terminal is not None:
        evidence, stdout, stderr = terminal
        if outcome.get("disposition") == "cancelled_before_launch":
            status = "CANCELLED"
        else:
            exit_code = outcome.get("exit_code")
            status = (
                "CANCELLED"
                if outcome["caller_cancelled"]
                else "FAILED"
                if outcome["timed_out"] or exit_code != 0
                else "SUCCEEDED"
            )
            value = outcome.get("command_duration_ms")
            duration = value if type(value) is int and value >= 0 else None
    return {
        "id": call.id,
        "name": command.name,
        "command_name": command.name,
        "command_version": policy.version,
        "status": status,
        "exit_code": exit_code,
        "output_artifact_digest": stdout,
        "stderr_artifact_digest": stderr,
        "completed_at": call.completed_at,
        "head_sha": head,
        "step_id": None,
        "attempt": attempt.attempt_number,
        "started_at": call.started_at,
        "duration_ms": duration,
        "evidence_digest": evidence,
        "configured_runner_mode": policy.runner_mode.value,
        "source": "subscription",
        "subscription_task_id": call.subscription_task_id,
        "subscription_attempt_id": call.subscription_attempt_id,
    }


def _terminal_evidence(
    run_id: UUID,
    call: ToolCall,
    operation: OperationIntent,
    metadata: Mapping[str, Any],
    outcome: Mapping[str, Any],
    by_digest: dict[str, tuple[Artifact, ArtifactLineage]],
    parents: dict[UUID, set[UUID]],
) -> tuple[str, str | None, str | None] | None:
    """Use only the persisted, cross-bound result; this is not blob verification."""
    if (
        operation.status != "SUCCEEDED"
        or operation.outcome_schema_version != 1
        or call.completed_at is None
        or call.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
        or outcome.get("tool_call_id") != str(call.id)
        or any(metadata.get(key) != value for key, value in outcome.items())
    ):
        return None
    receipt = outcome.get("receipt_digest")
    if not isinstance(receipt, str):
        return None
    cancelled = outcome.get("disposition") == "cancelled_before_launch"
    if not cancelled and (
        type(outcome.get("caller_cancelled")) is not bool
        or type(outcome.get("timed_out")) is not bool
        or (outcome.get("exit_code") is not None and type(outcome["exit_code"]) is not int)
    ):
        return None
    expected = [(receipt, "named_check", call.id, _RECEIPT_MEDIA)]
    if not cancelled:
        for key, producer, producer_id, media in (
            ("command_result_digest", "command_result", call.id, _RESULT_MEDIA),
            ("stdout_digest", "command_output", run_id, _OUTPUT_MEDIA),
            ("stderr_digest", "command_output", run_id, _OUTPUT_MEDIA),
        ):
            digest = outcome.get(key)
            if not isinstance(digest, str):
                return None
            expected.append((digest, producer, producer_id, media))
    if any(
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest, _, _, _ in expected
    ):
        return None
    for digest, producer, producer_id, media in expected:
        pair = by_digest.get(digest)
        if pair is None or (pair[0].media_type, pair[1].producer_kind, pair[1].producer_id) != (
            media,
            producer,
            producer_id,
        ):
            return None
    if cancelled:
        return (receipt, None, None) if call.status == "CANCELLED" else None
    expected_status = (
        "CANCELLED"
        if outcome["caller_cancelled"]
        else "FAILED"
        if outcome["timed_out"] or outcome.get("exit_code") != 0
        else "SUCCEEDED"
    )
    if call.status != expected_status:
        return None
    receipt_id = by_digest[receipt][0].id
    result_id = by_digest[outcome["command_result_digest"]][0].id
    outputs = {by_digest[outcome[key]][0].id for key in ("stdout_digest", "stderr_digest")}
    if (
        parents.get(receipt_id, set()) != outputs | {result_id}
        or parents.get(result_id, set()) != outputs
    ):
        return None
    return receipt, outcome["stdout_digest"], outcome["stderr_digest"]


async def _artifacts(
    session: AsyncSession,
    run_id: UUID,
    operations: list[OperationIntent],
) -> tuple[dict[str, tuple[Artifact, ArtifactLineage]], dict[UUID, set[UUID]]]:
    """Batch the exact referenced evidence for a page, never all run artifacts."""
    digests = {
        value
        for op in operations
        for key in (
            "receipt_digest",
            "command_result_digest",
            "stdout_digest",
            "stderr_digest",
        )
        for value in ((op.outcome_payload or {}).get(key),)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
    }
    if not digests:
        return {}, {}
    rows = (
        await session.execute(
            select(Artifact, ArtifactLineage)
            .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
            .where(ArtifactLineage.run_id == run_id, Artifact.digest.in_(digests))
        )
    ).all()
    edges = (
        await session.execute(
            select(
                ArtifactLineageParent.artifact_id,
                ArtifactLineageParent.parent_artifact_id,
            ).where(
                ArtifactLineageParent.run_id == run_id,
                ArtifactLineageParent.artifact_id.in_([artifact.id for artifact, _ in rows]),
            )
        )
    ).all()
    parents: dict[UUID, set[UUID]] = {}
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)
    return {artifact.digest: (artifact, lineage) for artifact, lineage in rows}, parents
