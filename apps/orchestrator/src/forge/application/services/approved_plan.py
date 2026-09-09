"""Resolve immutable delivery input from the actual human-approved plan."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.tasks import TaskRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.approval import PlanApprovalEvidence, canonical_digest
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.plan import PlanOutput
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot
from forge.persistence.models import Approval
from forge.persistence.repositories.tasks import compute_task_digest


class ApprovedPlanError(RuntimeError):
    """Delivery lacks exact, still-valid plan approval authority."""


@dataclass(frozen=True, slots=True)
class ApprovedPlan:
    run: RunSnapshot
    task: TaskRecord
    policy: ProjectPolicy
    plan: PlanOutput
    evidence: PlanApprovalEvidence
    approval_id: UUID
    approval_version: int
    approval_actor_id: UUID


class ApprovedPlanLoader:
    """Load the approved historical base, never a guessed latest plan or gate."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def load(self, work: UnitOfWork, run_id: UUID) -> ApprovedPlan:
        try:
            run = await work.runs.get_for_update(run_id)
            events = [
                event
                for event in await work.events.list_after(run_id, 0)
                if event.event_type == "run.plan_approved"
            ]
            if len(events) != 1:
                raise ApprovedPlanError
            event = events[0]
            approval_id = UUID(str(event.payload.get("approval_id")))
            approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
            if (
                not isinstance(approval, Approval)
                or approval.run_id != run_id
                or approval.gate != "plan"
                or approval.invalidated_at is not None
                or approval.policy_version != run.policy_version
                or event.run_version != approval.run_version + 1
                or event.run_version > run.version
                or event.actor_class != "operator"
                or event.actor_id != approval.authenticated_actor_id
            ):
                raise ApprovedPlanError
            task = await work.tasks.get(run.task_id, for_update=True)
            project = await work.projects.get(run.project_id, for_update=True)
            policy_record = await work.projects.get_policy(
                run.project_id, approval.policy_version, for_update=True
            )
            policy = ProjectPolicy.model_validate(policy_record.document)
            policy_bytes = json.dumps(
                policy_record.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if (
                policy.id != run.project_id
                or task.project_id != run.project_id
                or project.current_policy_version != policy.version
                or policy.version != approval.policy_version
                or policy_record.version != policy.version
                or policy_record.document_schema_version != 1
                or hashlib.sha256(policy_bytes).hexdigest() != policy_record.policy_digest
                or policy.repository_path != project.canonical_path
                or policy.github_repository != project.github_repository
                or policy.default_branch != project.default_branch
            ):
                raise ApprovedPlanError
            evidence_descriptor, evidence_bytes = await self._load(
                work, run_id, approval.evidence_digest
            )
            evidence = PlanApprovalEvidence.model_validate_json(evidence_bytes)
            if canonical_digest(evidence) != approval.evidence_digest:
                raise ApprovedPlanError
            execution = await work.executions.get_outcome(run_id, "plan", evidence.plan_attempt)
            if (
                execution is None
                or execution.status is not ExecutionStatus.SUCCEEDED
                or evidence_descriptor.producer_type != "plan_approval_evidence"
                or evidence_descriptor.producer_id != execution.agent_execution_id
            ):
                raise ApprovedPlanError
            plan_descriptor, plan_bytes = await self._load(work, run_id, evidence.plan_digest)
            if (
                plan_descriptor.producer_type != "implementation_plan"
                or plan_descriptor.artifact_id != execution.output_artifact_id
                or evidence_descriptor.parent_digests != (plan_descriptor.digest,)
            ):
                raise ApprovedPlanError
            plan = PlanOutput.model_validate_json(plan_bytes)
            task_digest = compute_task_digest(
                title=task.title,
                body=task.body,
                source_url=task.source_url,
                source_updated_at=task.source_updated_at,
                external_source=task.external_source,
                external_id=task.external_id,
            )
            if (
                task.task_digest != task_digest
                or run.base_ref is None
                or run.base_sha is None
                or run.base_ref != f"refs/heads/{policy.default_branch}"
            ):
                raise ApprovedPlanError
            expected = PlanApprovalEvidence(
                task_version=1,
                plan_attempt=evidence.plan_attempt,
                task_digest=task_digest,
                plan_digest=plan_descriptor.digest,
                repository=project.github_repository,
                base_ref=run.base_ref,
                base_sha=run.base_sha,
                policy_version=policy.version,
                dependency_changes=tuple(sorted(plan.dependency_changes)),
                required_checks={name: "planned" for name in sorted(plan.required_checks)},
                runner_mode=policy.runner_mode,
                local_remediation_limit=policy.local_remediation_limit,
                token_budget=policy.planner_model.max_input_tokens
                + policy.planner_model.max_output_tokens,
                cost_budget_minor=policy.planner_model.max_cost_minor,
                duration_budget_seconds=policy.planner_model.max_duration_seconds,
            )
            if canonical_digest(expected) != approval.evidence_digest:
                raise ApprovedPlanError
            return ApprovedPlan(
                run,
                task,
                policy,
                plan,
                evidence,
                approval_id,
                approval.run_version,
                approval.authenticated_actor_id,
            )
        except ApprovedPlanError:
            raise
        except Exception:  # noqa: BLE001 - persisted authority is untrusted
            raise ApprovedPlanError("approved plan authority is invalid") from None

    async def _load(
        self, work: UnitOfWork, run_id: UUID, digest: str
    ) -> tuple[ArtifactDescriptor, bytes]:
        descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
        data = await self._artifacts.open_bytes(digest)
        if (
            descriptor.schema_version != 1
            or descriptor.media_type != "application/json"
            or descriptor.byte_count != len(data)
            or len(data) > 1_048_576
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ApprovedPlanError
        return descriptor, data


__all__ = ["ApprovedPlan", "ApprovedPlanError", "ApprovedPlanLoader"]
