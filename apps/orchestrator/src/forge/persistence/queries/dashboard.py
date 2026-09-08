"""Selected, bounded dashboard reads from one consistent PostgreSQL snapshot."""

import json
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.services.public_data import public_payload
from forge.domain.actor import AgentRole
from forge.domain.agent import _ALLOWED_ROLE_TOOLS
from forge.domain.branch_removal import branch_removal_recorded
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.teardown import has_removable_resources, teardown_confirmation
from forge.domain.worktree_operation import worktree_creation_request
from forge.observability.redaction import redact_value
from forge.persistence.models import (
    AgentExecution,
    AgentExecutionEvidenceInput,
    Approval,
    Artifact,
    EvidenceSet,
    ModelUsage,
    OperationIntent,
    Project,
    ProjectPolicyVersion,
    PullRequest,
    Review,
    Run,
    RunEvent,
    Task,
    ValidationResult,
)
from forge.persistence.queries.recovery import startup_intervention_hold
from forge.persistence.repositories.events import _event_from_record
from forge.persistence.repositories.operations import (
    PostgresOperationRepository,
    _intent_from_record,
)
from forge.persistence.repositories.runs import (
    PersistenceDataError,
    PostgresRunRepository,
    _snapshot_from_record,
)


class DashboardQuery:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def summary(self) -> dict[str, object]:
        async with self._factory() as session:
            rows = (
                await session.execute(select(Run.state, func.count()).group_by(Run.state))
            ).all()
        counts = {state: count for state, count in rows}
        return {"runs": counts, "total_runs": sum(counts.values())}

    async def run_projection(self, run_id: UUID) -> dict[str, object] | None:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            run = await session.get(Run, run_id)
            if run is None:
                return None
            project = await session.get(Project, run.project_id)
            task = await session.get(Task, run.task_id)
            policy_row = await session.get(
                ProjectPolicyVersion, (run.project_id, run.policy_version)
            )
            if project is None or task is None or policy_row is None:
                raise PersistenceDataError("run projection references missing records")
            policy = ProjectPolicy.model_validate(policy_row.document)
            if policy.id != run.project_id or policy.version != run.policy_version:
                raise PersistenceDataError("run policy binding differs")
            agents = await _agents(session, run, policy)
            validation = await _latest_evidence(session, run_id, "validation")
            review = await _latest_evidence(session, run_id, "review")
            checks = (
                list(
                    await session.scalars(
                        select(ValidationResult)
                        .where(
                            ValidationResult.run_id == run_id,
                            ValidationResult.step_id == validation.step_id,
                        )
                        .order_by(ValidationResult.check_name)
                        .limit(101)
                    )
                )
                if validation is not None
                else []
            )
            if len(checks) > 100:
                raise PersistenceDataError("validation projection exceeds bound")
            findings = (
                list(
                    await session.scalars(
                        select(Review)
                        .where(
                            Review.run_id == run_id,
                            Review.reviewer_execution_id == review.producer_execution_id,
                        )
                        .order_by(Review.finding_id)
                        .limit(101)
                    )
                )
                if review is not None
                else []
            )
            if len(findings) > 100:
                raise PersistenceDataError("review projection exceeds bound")
            approval = await session.scalar(
                select(Approval)
                .where(
                    Approval.run_id == run_id,
                    Approval.gate == "plan",
                    Approval.invalidated_at.is_(None),
                    Approval.policy_version == run.policy_version,
                )
                .order_by(Approval.created_at.desc(), Approval.id)
                .limit(1)
            )
            pr = await session.scalar(
                select(PullRequest)
                .where(PullRequest.run_id == run_id)
                .order_by(PullRequest.updated_at.desc(), PullRequest.id)
                .limit(1)
            )
            events = list(
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence.desc())
                    .limit(50)
                )
            )
            snapshot = _snapshot_from_record(run)
            branch_removed = await _branch_removed(session, snapshot)
            branch_retained = not branch_removed and await _owned_retained_branch(
                session, snapshot, policy
            )
            run_fields = (
                "id",
                "project_id",
                "task_id",
                "state",
                "version",
                "suspended_state",
                "suspension_kind",
                "local_remediation_count",
                "remote_remediation_count",
                "policy_version",
                "base_ref",
                "base_sha",
                "branch_name",
            )
            validation_digest = await _digest(
                session, validation.manifest_artifact_id if validation else None
            )
            review_digest = await _digest(session, review.manifest_artifact_id if review else None)
            return {
                "run": {name: getattr(snapshot, name) for name in run_fields},
                "task": {
                    "id": task.id,
                    "title": _text(task.title),
                    "body": _text(task.body),
                    "external_source": task.external_source,
                    "source_url": _text(task.source_url),
                    "untrusted_external_content": task.untrusted_external_content,
                },
                "project": {
                    "id": project.id,
                    "name": _text(project.name),
                    "github_repository": project.github_repository,
                    "policy_version": policy.version,
                    "policy_digest": policy_row.policy_digest,
                },
                "resource": {
                    "branch_removed": branch_removed,
                    "teardown_confirmation": teardown_confirmation(_snapshot_from_record(run)),
                    "database_role": run.database_role,
                    "worktree_path": run.worktree_path,
                    "branch_name": run.branch_name,
                    "database_state": run.database_state,
                    "database_name": run.database_name,
                },
                "plan": {
                    "output_artifact_digest": agents["planner"]["output_artifact_digest"],
                    "approval_id": approval.id if approval else None,
                    "approval_evidence_digest": approval.evidence_digest if approval else None,
                },
                "candidate": {
                    "commit": run.candidate_commit,
                    "pending_evidence_digest": run.pending_evidence_digest,
                    "validation_evidence_digest": validation_digest,
                    "review_evidence_digest": review_digest,
                },
                "pull_request": {
                    "number": pr.pull_request_number,
                    "repository": pr.repository,
                    "branch": pr.branch,
                    "base_ref": pr.base_ref,
                    "head_sha": pr.head_sha,
                    "base_sha": pr.base_sha,
                    "state": pr.state,
                    "merge_state": pr.merge_state,
                }
                if pr
                else None,
                "remote_observation": _remote_observation(pr),
                "checks": [
                    {
                        "id": check.id,
                        "name": check.check_name,
                        "command_name": check.command_name,
                        "command_version": check.command_version,
                        "status": check.status,
                        "exit_code": check.exit_code,
                        "completed_at": check.completed_at,
                        "output_artifact_digest": await _digest(session, check.output_artifact_id),
                        "head_sha": validation.head_sha if validation else None,
                    }
                    for check in checks
                ],
                "review": {
                    "execution_id": review.producer_execution_id if review else None,
                    "evidence_digest": review_digest,
                    "head_sha": review.head_sha if review else None,
                    "findings": [
                        {
                            "id": finding.finding_id,
                            "severity": finding.severity,
                            "path": _text(finding.path),
                            "start_line": finding.start_line,
                            "summary": _text(finding.summary),
                            "evidence": _text(finding.evidence),
                            "proposed_resolution": _text(finding.proposed_resolution),
                            "status": finding.status,
                        }
                        for finding in findings
                    ],
                },
                "agents": agents,
                "budgets": {
                    "local_remediation_count": run.local_remediation_count,
                    "local_remediation_limit": policy.local_remediation_limit,
                    "local_remediation_remaining": max(
                        0, policy.local_remediation_limit - run.local_remediation_count
                    ),
                    "remote_remediation_count": run.remote_remediation_count,
                    "remote_remediation_limit": policy.remote_remediation_limit,
                    "remote_remediation_remaining": max(
                        0, policy.remote_remediation_limit - run.remote_remediation_count
                    ),
                    "token_limit": run.token_budget,
                    "cost_limit_minor": run.cost_budget_minor,
                    "duration_limit_seconds": run.duration_budget_seconds,
                },
                "usage": await _usage(session, run_id),
                "security": {
                    "commands": [
                        {"name": command.name, "network_enabled": command.network_enabled}
                        for command in policy.commands
                    ],
                    "secret_paths": list(policy.effective_secret_paths),
                    "runner_mode": policy.runner_mode.value,
                    "trusted_project": policy.trusted_project,
                    "database_enabled": policy.database.enabled,
                },
                "latest_events": [
                    {
                        "sequence": event.sequence,
                        "event_type": event.event_type,
                        "run_version": event.run_version,
                        "actor_class": event.actor_class,
                        "occurred_at": event.occurred_at,
                        "payload": public_payload(event.payload),
                    }
                    for event in reversed(events)
                ],
                "recovery_hold": bool(
                    await session.scalar(select(startup_intervention_hold(run_id)))
                ),
                "teardown_eligible": (
                    snapshot.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
                    and (has_removable_resources(snapshot) or branch_retained)
                    and (await PostgresRunRepository(session).prove_quiescent(run_id)).is_quiescent
                ),
                "available_commands": [],
                "next_gate": run.pending_gate,
            }


async def _owned_retained_branch(
    session: AsyncSession, run: RunSnapshot, policy: ProjectPolicy
) -> bool:
    if (
        run.branch_name is None
        or run.base_sha is None
        or run.base_ref is None
        or run.branch_name
        in {
            policy.default_branch.removeprefix("refs/heads/"),
            run.base_ref.removeprefix("refs/heads/"),
        }
    ):
        return False
    identity = WorktreeIdentity.for_run(
        run.project_id, run.id, run.branch_name, policy.database.enabled
    )
    request = worktree_creation_request(run, identity, policy)
    intent = await PostgresOperationRepository(session=session).get_by_idempotency_key(
        request.idempotency_key
    )
    return (
        intent is not None
        and intent.run_id == run.id
        and intent.kind == request.kind
        and intent.request_schema_version == 1
        and intent.request_digest == request.request_digest
        and canonical_digest(intent.request_payload) == canonical_digest(request.request_payload)
        and intent.status is OperationStatus.SUCCEEDED
        and intent.outcome_schema_version == 1
        and intent.remote_resource_id == identity.worktree_name
        and intent.outcome is not None
        and canonical_digest(intent.outcome)
        == canonical_digest({"worktree_name": identity.worktree_name, "base_sha": run.base_sha})
    )


async def _branch_removed(session: AsyncSession, run: RunSnapshot) -> bool:
    events = list(
        await session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "resource.branch_removed")
            .limit(2)
        )
    )
    if len(events) != 1:
        return False
    try:
        intent_id = UUID(str(events[0].payload.get("operation_intent_id")))
        intent = await session.get(OperationIntent, intent_id)
        return intent is not None and branch_removal_recorded(
            run, _event_from_record(events[0]), _intent_from_record(intent)
        )
    except ValueError, TypeError, PersistenceDataError:
        return False


async def _digest(session: AsyncSession, artifact_id: UUID | None) -> str | None:
    if artifact_id is None:
        return None
    return cast(
        str | None, await session.scalar(select(Artifact.digest).where(Artifact.id == artifact_id))
    )


async def _latest_evidence(session: AsyncSession, run_id: UUID, kind: str) -> EvidenceSet | None:
    return cast(
        EvidenceSet | None,
        await session.scalar(
            select(EvidenceSet)
            .where(EvidenceSet.run_id == run_id, EvidenceSet.kind == kind)
            .order_by(EvidenceSet.created_at.desc(), EvidenceSet.id)
            .limit(1)
        ),
    )


async def _agents(
    session: AsyncSession, run: Run, policy: ProjectPolicy
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for role in AgentRole:
        execution = await session.scalar(
            select(AgentExecution)
            .where(AgentExecution.run_id == run.id, AgentExecution.role == role.value)
            .order_by(AgentExecution.created_at.desc(), AgentExecution.id)
            .limit(1)
        )
        binding = (
            await session.scalar(
                select(AgentExecutionEvidenceInput.evidence_set_id).where(
                    AgentExecutionEvidenceInput.run_id == run.id,
                    AgentExecutionEvidenceInput.consumer_execution_id == execution.id,
                    AgentExecutionEvidenceInput.purpose == "validation_results",
                )
            )
            if execution is not None and role is AgentRole.REVIEWER
            else None
        )
        model = {
            AgentRole.PLANNER: policy.planner_model,
            AgentRole.DEVELOPER: policy.developer_model,
            AgentRole.REVIEWER: policy.reviewer_model,
        }[role]
        result[role.value] = {
            "role": role.value,
            "provider": execution.provider if execution else model.provider,
            "model": execution.model if execution else model.model,
            "execution_id": execution.id if execution else None,
            "status": execution.status if execution else None,
            "instruction_version": execution.instruction_version if execution else None,
            "input_artifact_digest": await _digest(
                session, execution.input_artifact_id if execution else None
            ),
            "output_artifact_digest": await _digest(
                session, execution.output_artifact_id if execution else None
            ),
            "validation_evidence_set_id": binding,
            "independent": (
                binding is not None
                if role is AgentRole.REVIEWER and execution is not None
                else None
            ),
            "allowed_tools": sorted(tool.value for tool in _ALLOWED_ROLE_TOOLS[role]),
            "started_at": execution.started_at if execution else None,
            "completed_at": execution.completed_at if execution else None,
            "usage": await _usage(session, run.id, execution.id) if execution else None,
        }
    return result


async def _usage(
    session: AsyncSession, run_id: UUID | None, execution_id: UUID | None = None
) -> dict[str, object]:
    statement = (
        select(
            ModelUsage.currency,
            func.sum(ModelUsage.input_tokens),
            func.sum(ModelUsage.output_tokens),
            func.sum(ModelUsage.cached_input_tokens),
            func.sum(ModelUsage.duration_ms),
            func.sum(ModelUsage.tool_call_count),
            func.count(),
            func.coalesce(func.sum(ModelUsage.estimated_cost_minor), 0),
            func.count().filter(ModelUsage.estimated_cost_minor.is_(None)),
        )
        .group_by(ModelUsage.currency)
        .limit(101)
    )
    if run_id is not None:
        statement = statement.where(ModelUsage.run_id == run_id)
    if execution_id is not None:
        statement = statement.where(ModelUsage.agent_execution_id == execution_id)
    rows = (await session.execute(statement)).all()
    if len(rows) > 100:
        raise PersistenceDataError("usage currency projection exceeds bound")
    return {
        "input_tokens": sum(row[1] for row in rows),
        "output_tokens": sum(row[2] for row in rows),
        "cached_input_tokens": sum(row[3] for row in rows),
        "duration_ms": sum(row[4] for row in rows),
        "tool_calls": sum(row[5] for row in rows),
        "model_calls": sum(row[6] for row in rows),
        "currencies": [
            {"currency": row[0], "known_cost_minor": row[7], "unpriced_calls": row[8]}
            for row in sorted(rows, key=lambda row: row[0])
        ],
    }


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    result = redact_value(value)
    return result if isinstance(result, str) else "[REDACTED]"


def _remote_observation(pr: PullRequest | None) -> dict[str, object] | None:
    if pr is None or (pr.checks == {} and pr.review_state == {}):
        return None
    checks, reviews = pr.checks, pr.review_state
    if (
        pr.checks_schema_version != 1
        or pr.reviews_schema_version != 1
        or not isinstance(checks, dict)
        or not isinstance(reviews, dict)
        or set(checks) != {"observation_digest", "head_sha", "items"}
        or set(reviews) != set(checks)
        or checks["observation_digest"] != reviews["observation_digest"]
        or checks["head_sha"] != reviews["head_sha"]
        or not isinstance(checks["observation_digest"], str)
        or len(checks["observation_digest"]) != 64
        or any(c not in "0123456789abcdef" for c in checks["observation_digest"])
        or not isinstance(checks["items"], list)
        or not isinstance(reviews["items"], list)
        or len(json.dumps([checks, reviews]).encode("utf-8")) > 2 * 1048576
    ):
        raise PersistenceDataError("remote observation binding differs")
    check_fields = ("name", "status", "conclusion", "head_sha", "summary", "text")
    review_fields = (
        "reviewer",
        "state",
        "submitted_at",
        "requested_changes",
        "unresolved_threads",
        "comment_count",
        "body",
        "feedback",
    )
    if any(not isinstance(item, dict) for item in checks["items"] + reviews["items"]):
        raise PersistenceDataError("remote observation item differs")
    # Select normalized evidence fields only; remote links and unknown metadata
    # never become navigation or actions in the operator cockpit.
    return {
        "observation_digest": checks["observation_digest"],
        "head_sha": checks["head_sha"],
        "checks": [
            public_payload({key: item.get(key) for key in check_fields}) for item in checks["items"]
        ],
        "reviews": [
            public_payload({key: item.get(key) for key in review_fields})
            for item in reviews["items"]
        ],
    }
