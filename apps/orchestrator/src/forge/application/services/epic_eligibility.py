"""Read-only Git verification backed by durable, immutable completion receipts."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from forge.application.adapters.git import LocalGitRepositoryInspector, RepositoryInspectionError
from forge.domain.epic_run_bridge import DependencyEvidence, EpicExecutionNotFound
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models.epic_eligibility import EpicCompletionHandoff
from forge.persistence.models.epic_run_bridge import EpicExecution, EpicItemAttempt
from forge.persistence.models.execution import OperationIntent, RunEvent
from forge.persistence.models.project import Project
from forge.persistence.models.release import PullRequest
from forge.persistence.models.run import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_ITEMS = 128


class EpicItemEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    item_id: UUID
    disposition: Literal["required", "deferred"]
    status: Literal["ready", "blocked", "deferred", "active", "verified"]
    blocker_code: str | None
    dependency_evidence: tuple[DependencyEvidence, ...]
    completion_evidence: DependencyEvidence | None


class EpicEligibilityService:
    def __init__(
        self, unit_of_work_factory: Callable[[], PostgresUnitOfWork], *, data_root: str | None = None
    ) -> None:
        self._work = unit_of_work_factory
        self._data_root = data_root
        self._git = LocalGitRepositoryInspector()

    async def readiness(
        self, *, epic_id: UUID, execution_id: UUID
    ) -> tuple[EpicItemEligibility, ...]:
        async with self._work() as work:
            execution = await work.session.get(EpicExecution, execution_id)
            if execution is None or execution.epic_id != epic_id:
                raise EpicExecutionNotFound("execution was not found")
            graph = await work.epic_items.get_revision(epic_id, execution.graph_revision_id)
            epic = await work.epics.get(epic_id)
            project = await work.session.get(Project, epic.project_id)
            if graph.graph_digest != execution.graph_digest or project is None:
                raise EpicExecutionNotFound("execution source is invalid")
            integration_ref = f"refs/heads/{project.default_branch}"
            try:
                inspection = self._git.inspect(
                    repository_path=project.canonical_path,
                    data_root=self._data_root,
                    github_repository=project.github_repository,
                    default_branch=project.default_branch,
                )
                base_sha = inspection.base_sha
                success = True
            except RepositoryInspectionError:
                base_sha = ""
                success = False
            await work.commit()
        base_available = bool(success and _SHA.fullmatch(base_sha))
        if not base_available:
            base_sha = "0" * 40
        dependencies = sorted((item.item_id for item in graph.items), key=str)
        proof = await self.evidence(
            epic_id=epic_id,
            execution_id=execution_id,
            item_ids=dependencies,
            base_ref=integration_ref,
            base_sha=base_sha,
        )
        indexed = {value.item_id: value for value in proof}
        async with self._work() as work:
            attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution_id)
            latest: dict[UUID, tuple[int, RunState]] = {}
            for attempt in attempts:
                if (
                    attempt.item_id not in latest
                    or attempt.attempt_number > latest[attempt.item_id][0]
                ):
                    run = await work.runs.get(attempt.run_id)
                    latest[attempt.item_id] = (attempt.attempt_number, run.state)
            await work.commit()
        result = []
        for item in sorted(graph.items, key=lambda value: (value.ordinal, str(value.item_id))):
            status: Literal["ready", "blocked", "deferred", "active", "verified"]
            evidence = tuple(indexed[dependency] for dependency in item.dependency_item_ids)
            blocker = next(
                (
                    value.blocker_code or "predecessor_unverified"
                    for value in evidence
                    if value.status != "verified"
                ),
                None,
            )
            if not base_available:
                blocker = "integration_ref_unavailable"
            if base_available and indexed[item.item_id].status == "verified":
                status = "verified"
                blocker = None
            elif item.item_id in latest:
                run_state = latest[item.item_id][1]
                status = (
                    "blocked" if run_state in {"COMPLETED", "FAILED", "CANCELLED"} else "active"
                )
                if status == "blocked":
                    blocker = (
                        indexed[item.item_id].blocker_code or "predecessor_integration_unverified"
                    )
            elif item.disposition == "deferred":
                status = "deferred"
                blocker = blocker or "item_deferred"
            elif blocker:
                status = "blocked"
            else:
                status = "ready"
            result.append(
                EpicItemEligibility(
                    item_id=item.item_id,
                    disposition=item.disposition,
                    status=status,
                    blocker_code=blocker,
                    dependency_evidence=evidence,
                    completion_evidence=indexed.get(item.item_id),
                )
            )
        return tuple(result)

    async def evidence(
        self,
        *,
        epic_id: UUID,
        execution_id: UUID,
        item_ids: Sequence[UUID],
        base_ref: str,
        base_sha: str,
    ) -> tuple[DependencyEvidence, ...]:
        if len(item_ids) > _MAX_ITEMS or len(set(item_ids)) != len(item_ids):
            raise ValueError("invalid dependency item count")
        async with self._work() as work:
            execution = await work.session.get(EpicExecution, execution_id)
            if execution is None or execution.epic_id != epic_id:
                raise EpicExecutionNotFound("execution was not found")
            epic = await work.epics.get(epic_id)
            project = await work.session.get(Project, epic.project_id)
            graph = await work.epic_items.get_revision(epic_id, execution.graph_revision_id)
            if graph.graph_digest != execution.graph_digest or project is None:
                raise EpicExecutionNotFound("execution source is invalid")
            valid_ids = {item.item_id for item in graph.items}
            if not set(item_ids) <= valid_ids:
                raise ValueError("dependency is outside frozen graph")
            expected_ref = f"refs/heads/{project.default_branch}"
            ref_ok = base_ref == expected_ref and bool(_SHA.fullmatch(base_sha))
            if ref_ok:
                try:
                    inspection = self._git.inspect(
                        repository_path=project.canonical_path,
                        data_root=self._data_root,
                        github_repository=project.github_repository,
                        default_branch=project.default_branch,
                    )
                    ref_ok = inspection.base_sha == base_sha
                except RepositoryInspectionError:
                    ref_ok = False
            result = []
            for item_id in item_ids:
                result.append(
                    await self._one(
                        work,
                        execution_id,
                        item_id,
                        project.id,
                        project.canonical_path,
                        project.github_repository,
                        expected_ref,
                        base_sha,
                        ref_ok,
                    )
                )
            await work.commit()
            return tuple(result)

    async def _one(
        self,
        work: PostgresUnitOfWork,
        execution_id: UUID,
        item_id: UUID,
        project_id: UUID,
        repository: str,
        github_repository: str,
        integration_ref: str,
        base_sha: str,
        ref_ok: bool,
    ) -> DependencyEvidence:
        attempts = (
            await work.session.scalars(
                select(EpicItemAttempt)
                .where(
                    EpicItemAttempt.execution_id == execution_id,
                    EpicItemAttempt.item_id == item_id,
                )
                .order_by(EpicItemAttempt.attempt_number.desc())
                .limit(128)
            )
        ).all()
        if not attempts:
            return DependencyEvidence(
                item_id=item_id, status="unknown", blocker_code="predecessor_not_started"
            )
        if not ref_ok:
            return DependencyEvidence(
                item_id=item_id, status="unverified", blocker_code="integration_ref_changed"
            )
        handoff = await work.session.scalar(
            select(EpicCompletionHandoff).where(
                EpicCompletionHandoff.execution_id == execution_id,
                EpicCompletionHandoff.item_id == item_id,
            )
        )
        for attempt in attempts:
            run = await work.session.get(Run, attempt.run_id)
            if run is None or run.state != "COMPLETED" or run.project_id != project_id:
                continue
            if attempt.base_ref != integration_ref:
                continue
            if not (await work.runs.prove_quiescent(run.id)).is_quiescent:
                continue
            pull = await work.session.scalar(
                select(PullRequest).where(PullRequest.run_id == run.id)
            )
            if (
                pull is None
                or pull.state != "MERGED"
                or pull.merge_intent_id is None
                or not pull.merge_sha
            ):
                continue
            intent = await work.session.get(OperationIntent, pull.merge_intent_id)
            if (
                intent is None
                or intent.run_id != run.id
                or intent.operation_kind != "merge_pr"
                or intent.status != "SUCCEEDED"
                or intent.outcome_payload is None
                or intent.outcome_payload.get("merge_sha") != pull.merge_sha
                or intent.request_digest != canonical_digest(intent.request_payload)
                or intent.idempotency_key != f"{run.id}:merge_pr:{intent.request_digest}"
                or pull.base_ref not in (integration_ref, integration_ref.removeprefix("refs/heads/"))
                or pull.repository != github_repository
            ):
                continue
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "run.merge_completed",
                    RunEvent.payload["merge_intent_id"].astext == str(intent.id),
                    RunEvent.payload["merge_sha"].astext == pull.merge_sha,
                )
            )
            if event is None:
                continue
            if event.actor_class != "worker" or event.run_version > run.version:
                continue
            try:
                integrated = self._git.proves_integration(
                    repository_path=repository,
                    data_root=self._data_root,
                    github_repository=github_repository,
                    default_branch=integration_ref.removeprefix("refs/heads/"),
                    base_sha=base_sha,
                    merge_sha=pull.merge_sha,
                )
            except RepositoryInspectionError:
                integrated = False
            if not integrated:
                continue
            proof = {
                "schema_version": 1,
                "execution_id": str(execution_id),
                "item_id": str(item_id),
                "attempt_id": str(attempt.id),
                "run_id": str(run.id),
                "run_version": event.run_version,
                "merge_intent_id": str(intent.id),
                "merge_sha": pull.merge_sha,
                "integration_ref": integration_ref,
                "verified_base_sha": base_sha,
            }
            digest = canonical_digest(proof)
            if handoff is not None:
                if (
                    handoff.attempt_id,
                    handoff.merge_intent_id,
                    handoff.merge_sha,
                    handoff.integration_ref,
                ) != (
                    attempt.id,
                    intent.id,
                    pull.merge_sha,
                    integration_ref,
                ):
                    continue
                return DependencyEvidence(
                    item_id=item_id,
                    status="verified",
                    predecessor_run_id=run.id,
                    integrated_sha=pull.merge_sha,
                    handoff_id=handoff.id,
                )
            handoff_id = uuid4()
            await work.session.execute(
                insert(EpicCompletionHandoff)
                .values(
                    id=handoff_id,
                    execution_id=execution_id,
                    item_id=item_id,
                    attempt_id=attempt.id,
                    run_id=run.id,
                    run_version=event.run_version,
                    merge_intent_id=intent.id,
                    merge_sha=pull.merge_sha,
                    integration_ref=integration_ref,
                    verified_base_sha=base_sha,
                    evidence_digest=digest,
                )
                .on_conflict_do_nothing(index_elements=["execution_id", "item_id"])
            )
            handoff = await work.session.scalar(
                select(EpicCompletionHandoff).where(
                    EpicCompletionHandoff.execution_id == execution_id,
                    EpicCompletionHandoff.item_id == item_id,
                )
            )
            if handoff is not None and (
                handoff.attempt_id,
                handoff.merge_intent_id,
                handoff.merge_sha,
            ) == (
                attempt.id,
                intent.id,
                pull.merge_sha,
            ):
                return DependencyEvidence(
                    item_id=item_id,
                    status="verified",
                    predecessor_run_id=run.id,
                    integrated_sha=pull.merge_sha,
                    handoff_id=handoff.id,
                )
        latest = await work.session.get(Run, attempts[0].run_id)
        blocker = (
            "predecessor_failed"
            if latest and latest.state in {"FAILED", "CANCELLED"}
            else "predecessor_integration_unverified"
        )
        return DependencyEvidence(item_id=item_id, status="unverified", blocker_code=blocker)
