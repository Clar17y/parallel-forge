"""Shared fail-closed validation for frozen plan-approval evidence."""

from __future__ import annotations

import hashlib
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.projects import RepositoryInspector
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.approval import ApprovalGate, PlanApprovalEvidence, canonical_digest
from forge.domain.plan import PlanOutput
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.tasks import compute_task_digest


class PlanEvidenceValidationError(RuntimeError):
    """The frozen plan gate no longer represents authoritative state."""


class CurrentPlanSource:
    def __init__(self, policy_version: int, base_ref: str, base_sha: str) -> None:
        self.policy_version = policy_version
        self.base_ref = base_ref
        self.base_sha = base_sha


class PlanEvidenceValidator:
    """Recompute plan-gate evidence using only immutable and authoritative inputs."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        repository_inspector: RepositoryInspector,
        *,
        data_root: str,
    ) -> None:
        if not isinstance(data_root, str) or not data_root:
            raise ValueError("data_root is required")
        self._artifact_store = artifact_store
        self._repository_inspector = repository_inspector
        self._data_root = data_root

    async def validate(self, work: UnitOfWork, run_id: UUID) -> PlanApprovalEvidence:
        try:
            run = await work.runs.get_for_update(run_id)
            if (
                run.state is not RunState.AWAITING_PLAN_APPROVAL
                or run.pending_gate is not ApprovalGate.PLAN
                or run.pending_evidence_digest is None
                or run.policy_version is None
            ):
                raise PlanEvidenceValidationError
            task = await work.tasks.get(run.task_id, for_update=True)
            project = await work.projects.get(run.project_id, for_update=True)
            policy_record = await work.projects.get_policy(
                run.project_id, run.policy_version, for_update=True
            )
            policy = ProjectPolicy.model_validate(policy_record.document)
            if (
                policy.id != project.id
                or policy.version != policy_record.version
                or project.current_policy_version != policy_record.version
                or policy.repository_path != project.canonical_path
                or policy.github_repository != project.github_repository
                or policy.default_branch != project.default_branch
            ):
                raise PlanEvidenceValidationError
            task_digest = compute_task_digest(
                title=task.title,
                body=task.body,
                source_url=task.source_url,
                source_updated_at=task.source_updated_at,
                external_source=task.external_source,
                external_id=task.external_id,
            )
            if task.task_digest != task_digest:
                raise PlanEvidenceValidationError
            evidence_bytes = await self._load_artifact(work, run, run.pending_evidence_digest)
            evidence = PlanApprovalEvidence.model_validate_json(evidence_bytes)
            if canonical_digest(evidence) != run.pending_evidence_digest:
                raise PlanEvidenceValidationError
            execution = await work.executions.get_outcome(run.id, "plan", evidence.plan_attempt)
            if (
                execution is None
                or execution.status is not ExecutionStatus.SUCCEEDED
                or execution.output_artifact_id is None
            ):
                raise PlanEvidenceValidationError
            evidence_descriptor = await work.artifacts.get_by_digest(
                run.pending_evidence_digest, run_id=run.id
            )
            if (
                evidence_descriptor.producer_type != "plan_approval_evidence"
                or evidence_descriptor.producer_id != execution.agent_execution_id
            ):
                raise PlanEvidenceValidationError
            plan_bytes = await self._load_artifact(work, run, evidence.plan_digest)
            plan_descriptor = await work.artifacts.get_by_digest(
                evidence.plan_digest, run_id=run.id
            )
            if (
                plan_descriptor.artifact_id != execution.output_artifact_id
                or evidence_descriptor.parent_digests != (plan_descriptor.digest,)
            ):
                raise PlanEvidenceValidationError
            plan = PlanOutput.model_validate_json(plan_bytes)
            inspection = self._repository_inspector.inspect(
                repository_path=project.canonical_path,
                data_root=self._data_root,
                github_repository=project.github_repository,
                default_branch=project.default_branch,
            )
            if run.base_ref != inspection.base_ref or run.base_sha != inspection.base_sha:
                raise PlanEvidenceValidationError
            expected = PlanApprovalEvidence(
                task_version=1,
                plan_attempt=(await work.executions.next_attempt(run.id, "plan")) - 1,
                task_digest=task_digest,
                plan_digest=_digest(plan_bytes),
                repository=inspection.github_repository,
                base_ref=inspection.base_ref,
                base_sha=inspection.base_sha,
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
            if canonical_digest(expected) != canonical_digest(evidence):
                raise PlanEvidenceValidationError
            return evidence
        except PlanEvidenceValidationError:
            raise
        except Exception as error:  # persisted/model/repository inputs are untrusted
            raise PlanEvidenceValidationError from error

    async def current_source(self, work: UnitOfWork, run_id: UUID) -> CurrentPlanSource:
        """Read current immutable policy and inspected base for a fresh semantic attempt."""
        try:
            run = await work.runs.get_for_update(run_id)
            project = await work.projects.get(run.project_id, for_update=True)
            if project.current_policy_version is None:
                raise PlanEvidenceValidationError
            record = await work.projects.get_policy(
                project.id, project.current_policy_version, for_update=True
            )
            policy = ProjectPolicy.model_validate(record.document)
            if policy.id != project.id or policy.version != record.version:
                raise PlanEvidenceValidationError
            inspection = self._repository_inspector.inspect(
                repository_path=project.canonical_path,
                data_root=self._data_root,
                github_repository=project.github_repository,
                default_branch=project.default_branch,
            )
            return CurrentPlanSource(record.version, inspection.base_ref, inspection.base_sha)
        except PlanEvidenceValidationError:
            raise
        except Exception as error:
            raise PlanEvidenceValidationError from error

    async def _load_artifact(self, work: UnitOfWork, run: RunSnapshot, digest: str) -> bytes:
        descriptor = await work.artifacts.get_by_digest(digest, run_id=run.id)
        data = await self._artifact_store.open_bytes(descriptor.digest)
        if _digest(data) != descriptor.digest:
            raise PlanEvidenceValidationError
        return data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["CurrentPlanSource", "PlanEvidenceValidationError", "PlanEvidenceValidator"]
