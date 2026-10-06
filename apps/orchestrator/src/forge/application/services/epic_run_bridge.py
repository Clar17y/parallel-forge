"""Launch saved epic items as ordinary immutable Forge tasks and runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, Self
from uuid import UUID, uuid4

from forge.application.ports.epic_brief import EpicBriefRepository
from forge.application.ports.epic_items import EpicItemsRepository
from forge.application.ports.epic_run_bridge import EligibilityPort, EpicRunBridgeRepository
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import RunService, RunUnitOfWork
from forge.domain.epic_brief import BriefBindingConflict, BriefContent
from forge.domain.epic_items import GraphBindingConflict, ItemSnapshot
from forge.domain.epic_run_bridge import (
    DependencyEvidence,
    EpicAttempt,
    EpicExecutionBindingConflict,
    EpicExecutionSnapshot,
    EpicLaunchConflict,
    ExecutionStartRequest,
    LaunchRequest,
)
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.persistence.repositories.tasks import MAX_BODY_BYTES

_TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


class EpicLaunchWork(RunUnitOfWork, Protocol):
    epics: EpicBriefRepository
    epic_items: EpicItemsRepository
    epic_run_bridge: EpicRunBridgeRepository

    async def __aenter__(self) -> Self: ...


class EpicRunBridgeService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], EpicLaunchWork],
        *,
        run_service: RunService,
        eligibility: EligibilityPort | None = None,
        epic_ceiling: TaskBudget | None = None,
        child_hold: TaskBudget | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._runs = run_service
        self._eligibility = eligibility
        self._epic_ceiling = epic_ceiling or TaskBudget(
            max_provider_attempts=12, max_duration_seconds=3600, max_tool_calls=300
        )
        self._child_hold = child_hold or TaskBudget(
            max_provider_attempts=1, max_duration_seconds=300, max_tool_calls=25
        )

    async def start(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        expected_epic_version: int,
        brief_revision_id: UUID | None = None,
        brief_digest: str | None = None,
        graph_revision_id: UUID | None = None,
        graph_digest: str | None = None,
        owner_override: bool = False,
        override_note: str | None = None,
    ) -> EpicExecutionSnapshot:
        """Freeze one saved pair without admitting a task or child run."""
        request = ExecutionStartRequest(
            expected_epic_version=expected_epic_version,
            brief_revision_id=brief_revision_id,
            brief_digest=brief_digest,
            graph_revision_id=graph_revision_id,
            graph_digest=graph_digest,
            owner_override=owner_override,
            override_note=override_note,
        )
        digest = canonical_digest(
            {"epic_id": str(epic_id), "request": request.model_dump(mode="json")}
        )
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.execution.start",
                scope=f"epic:{epic_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                if receipt.response_payload is None:
                    raise EpicExecutionBindingConflict("start receipt has no response")
                result = EpicExecutionSnapshot.model_validate(receipt.response_payload)
                await work.commit()
                return result
            epic = await work.epics.get(epic_id, for_update=True)
            if epic.version != request.expected_epic_version:
                raise EpicLaunchConflict(["epic_version_stale"], actual_epic_version=epic.version)
            selected = (
                request.brief_revision_id or epic.accepted_brief_revision_id,
                request.brief_digest or epic.accepted_brief_digest,
                request.graph_revision_id or epic.accepted_graph_revision_id,
                request.graph_digest or epic.accepted_graph_digest,
            )
            brief_id, selected_brief_digest, graph_id, selected_graph_digest = selected
            if (
                brief_id is None
                or selected_brief_digest is None
                or graph_id is None
                or selected_graph_digest is None
            ):
                raise EpicExecutionBindingConflict("matching saved brief and graph are required")
            brief = await work.epics.get_revision(epic_id, brief_id)
            graph = await work.epic_items.get_revision(epic_id, graph_id)
            if brief.content_digest != selected_brief_digest:
                raise EpicExecutionBindingConflict("brief revision digest mismatch")
            if (
                graph.graph_digest != selected_graph_digest
                or graph.brief_revision_id != brief_id
                or graph.brief_digest != selected_brief_digest
            ):
                raise EpicExecutionBindingConflict("graph revision does not match brief")
            warnings: list[str] = []
            if selected != (
                epic.accepted_brief_revision_id,
                epic.accepted_brief_digest,
                epic.accepted_graph_revision_id,
                epic.accepted_graph_digest,
            ):
                warnings.append("source_not_accepted")
            if await work.epic_run_bridge.has_active_execution(epic_id):
                warnings.append("active_execution")
            if warnings and not request.owner_override:
                raise EpicLaunchConflict(warnings, actual_epic_version=epic.version)
            result = await work.epic_run_bridge.create_execution(
                epic_id=epic_id,
                brief_revision_id=brief_id,
                brief_digest=selected_brief_digest,
                graph_revision_id=graph_id,
                graph_digest=selected_graph_digest,
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.execution_started",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=receipt.id,
                payload={
                    "execution_id": str(result.execution_id),
                    "brief_revision_id": str(brief_id),
                    "graph_revision_id": str(graph_id),
                    "owner_override": request.owner_override,
                    "override_note": request.override_note,
                    "warnings": warnings,
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload=result.model_dump(mode="json"),
                resource_kind="epic_execution",
                resource_id=result.execution_id,
            )
            await work.commit()
            return result

    async def launch(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: LaunchRequest,
    ) -> EpicAttempt:
        request = _request(request)
        digest = canonical_digest(
            {"epic_id": str(epic_id), "request": request.model_dump(mode="json")}
        )
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.item.launch",
                scope=f"epic:{epic_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                if receipt.resource_kind != "epic_item_attempt" or receipt.resource_id is None:
                    raise EpicExecutionBindingConflict("launch receipt has no attempt")
                result = await work.epic_run_bridge.get_attempt(epic_id, receipt.resource_id)
                await work.commit()
                return result

            # Epic -> project is the fixed admission lock order. Every launch,
            # including an override, passes through this same serial fence.
            epic = await work.epics.get(epic_id, for_update=True)
            brief = await work.epics.get_revision(epic_id, request.brief_revision_id)
            graph = await work.epic_items.get_revision(epic_id, request.graph_revision_id)
            if brief.content_digest != request.brief_digest:
                raise BriefBindingConflict("brief revision digest mismatch")
            if (graph.graph_digest, graph.brief_revision_id, graph.brief_digest) != (
                request.graph_digest,
                request.brief_revision_id,
                request.brief_digest,
            ):
                raise GraphBindingConflict("graph revision binding mismatch")
            item = next((value for value in graph.items if value.item_id == request.item_id), None)
            if item is None:
                raise GraphBindingConflict("item is not in saved graph")
            blockers: list[str] = []
            if epic.version != request.expected_epic_version:
                blockers.append("epic_version_stale")
            if (request.brief_revision_id, request.brief_digest) != (
                epic.accepted_brief_revision_id,
                epic.accepted_brief_digest,
            ):
                blockers.append("brief_not_accepted")
            if (request.graph_revision_id, request.graph_digest) != (
                epic.accepted_graph_revision_id,
                epic.accepted_graph_digest,
            ):
                blockers.append("graph_not_accepted")
            budget_blockers = await work.epic_run_bridge.child_budget_blockers(
                epic_id, epic.project_id, ceiling=self._epic_ceiling, hold=self._child_hold
            )
            blockers.extend(budget_blockers)
            project = await work.projects.get(epic.project_id, for_update=True)
            inspection = self._runs.inspect_base(project)
            if request.execution_id is None:
                execution = await work.epic_run_bridge.create_execution(
                    epic_id=epic_id,
                    brief_revision_id=request.brief_revision_id,
                    brief_digest=request.brief_digest,
                    graph_revision_id=request.graph_revision_id,
                    graph_digest=request.graph_digest,
                )
            else:
                execution = await work.epic_run_bridge.get_execution(request.execution_id)
                if (
                    execution.epic_id,
                    execution.brief_revision_id,
                    execution.brief_digest,
                    execution.graph_revision_id,
                    execution.graph_digest,
                ) != (
                    epic_id,
                    request.brief_revision_id,
                    request.brief_digest,
                    request.graph_revision_id,
                    request.graph_digest,
                ):
                    raise EpicExecutionBindingConflict("execution source binding does not match")
            execution_id = execution.execution_id
            control_state = await work.epic_run_bridge.control_state(execution_id)
            if control_state is not None and control_state != "ACTIVE":
                blockers.append("execution_not_active")
            prior = await work.epic_run_bridge.list_attempts(epic_id)
            if item.disposition == "deferred":
                blockers.append("item_deferred")
            for value in prior:
                previous_run = await work.runs.get_for_update(value.run_id)
                if previous_run.state not in _TERMINAL:
                    blockers.append("active_child")
                elif not (await work.runs.prove_quiescent(value.run_id)).is_quiescent:
                    blockers.append("child_effects_unsettled")
            # A trusted producer can provide positive proof. With none, each
            # dependency stays unknown and default progression remains blocked.
            for _ in range(3):
                reported = (
                    await self._eligibility.evidence(
                        epic_id=epic_id,
                        item_ids=item.dependency_item_ids,
                        base_sha=inspection.base_sha,
                    )
                    if self._eligibility is not None and item.dependency_item_ids
                    else ()
                )
                final_inspection = self._runs.inspect_base(project)
                if (final_inspection.base_ref, final_inspection.base_sha) == (
                    inspection.base_ref,
                    inspection.base_sha,
                ):
                    break
                inspection = final_inspection
            else:
                # The final inspection is a real base, but the last eligibility
                # response describes its predecessor. An owner may explicitly
                # launch on this base with unknown dependency proof.
                blockers.append("repository_base_moved")
                reported = ()
            indexed: dict[UUID, DependencyEvidence] = {}
            for raw in reported:
                if isinstance(raw, DependencyEvidence):
                    document = raw.model_dump(mode="json")
                elif isinstance(raw, Mapping):
                    document = dict(raw)
                else:
                    raise TypeError("eligibility evidence is invalid")
                validate_durable_payload(document)
                observation = DependencyEvidence.model_validate(document)
                if observation.item_id in item.dependency_item_ids:
                    if observation.item_id in indexed:
                        raise ValueError("duplicate eligibility evidence")
                    indexed[observation.item_id] = observation
            evidence = [
                indexed.get(value, DependencyEvidence(item_id=value, status="unknown"))
                for value in item.dependency_item_ids
            ]
            if any(value.status != "verified" for value in evidence):
                blockers.append("predecessor_unverified")
            if blockers and not request.owner_override:
                raise EpicLaunchConflict(blockers, actual_epic_version=epic.version)
            body, context_digest = build_task_context(
                execution_id=execution_id,
                epic_id=epic_id,
                expected_epic_version=request.expected_epic_version,
                actual_epic_version=epic.version,
                brief_revision_id=request.brief_revision_id,
                brief_digest=request.brief_digest,
                graph_revision_id=request.graph_revision_id,
                graph_digest=request.graph_digest,
                item=item,
                brief=brief.content,
                base_ref=inspection.base_ref,
                base_sha=inspection.base_sha,
                dependency_evidence=evidence,
                owner_override=request.owner_override,
                override_note=request.override_note,
                blocker_codes=blockers,
            )
            task = await work.tasks.create(
                task_id=uuid4(),
                project_id=epic.project_id,
                title=item.title,
                body=body,
            )
            run = await self._runs.create_in_transaction(
                work=work,
                actor=actor,
                task=task,
                project=project,
                profile_id=request.profile_id,
                profile_version=request.profile_version,
                inspection=inspection,
            )
            attempt = EpicAttempt(
                attempt_id=uuid4(),
                execution_id=execution_id,
                epic_id=epic_id,
                item_id=item.item_id,
                attempt_number=1
                + max(
                    (
                        value.attempt_number
                        for value in prior
                        if value.execution_id == execution_id and value.item_id == item.item_id
                    ),
                    default=0,
                ),
                actor_id=actor.actor_id,
                item_disposition=item.disposition,
                expected_epic_version=request.expected_epic_version,
                actual_epic_version=epic.version,
                task_id=task.id,
                run_id=run.id,
                brief_revision_id=request.brief_revision_id,
                brief_digest=request.brief_digest,
                graph_revision_id=request.graph_revision_id,
                graph_digest=request.graph_digest,
                item_digest=item.item_digest,
                context_digest=context_digest,
                task_digest=task.task_digest,
                base_ref=inspection.base_ref,
                base_sha=inspection.base_sha,
                owner_override=request.owner_override,
                override_note=request.override_note,
                blocker_codes=blockers,
                dependency_evidence=evidence,
                created_at=datetime.now(UTC),
            )
            await work.epic_run_bridge.create_attempt(attempt)
            await work.epic_run_bridge.create_child_hold(attempt, self._child_hold)
            if request.owner_override and control_state is not None and control_state != "ACTIVE":
                await work.epic_run_bridge.note_owner_child_admission(execution_id)
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.item_launched",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=receipt.id,
                payload={
                    "attempt_id": str(attempt.attempt_id),
                    "run_id": str(run.id),
                    "task_id": str(task.id),
                    "item_id": str(item.item_id),
                    "execution_id": str(execution_id),
                    "expected_epic_version": request.expected_epic_version,
                    "actual_epic_version": epic.version,
                    "item_disposition": item.disposition,
                    "context_digest": context_digest,
                    "base_sha": run.base_sha,
                    "owner_override": request.owner_override,
                    "blocker_codes": blockers,
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload=attempt.model_dump(mode="json"),
                resource_kind="epic_item_attempt",
                resource_id=attempt.attempt_id,
            )
            await work.commit()
            return attempt

    async def get(self, epic_id: UUID, attempt_id: UUID) -> EpicAttempt:
        async with self._unit_of_work_factory() as work:
            result = await work.epic_run_bridge.get_attempt(epic_id, attempt_id)
            await work.commit()
            return result

    async def list(self, epic_id: UUID) -> Sequence[EpicAttempt]:
        async with self._unit_of_work_factory() as work:
            await work.epics.get(epic_id)
            result = await work.epic_run_bridge.list_attempts(epic_id)
            await work.commit()
            return result


def build_task_context(
    *,
    execution_id: UUID,
    epic_id: UUID,
    expected_epic_version: int,
    actual_epic_version: int,
    brief_revision_id: UUID,
    brief_digest: str,
    graph_revision_id: UUID,
    graph_digest: str,
    item: ItemSnapshot,
    brief: BriefContent,
    base_ref: str,
    base_sha: str,
    dependency_evidence: Sequence[DependencyEvidence],
    owner_override: bool,
    override_note: str | None,
    blocker_codes: Sequence[str],
) -> tuple[str, str]:
    """Freeze only selected requirement excerpts, decisions and real proof."""
    selected = set(item.source_requirement_ids)
    payload: dict[str, object] = {
        "schema_version": 1,
        "execution_id": str(execution_id),
        "epic_id": str(epic_id),
        "expected_epic_version": expected_epic_version,
        "actual_epic_version": actual_epic_version,
        "brief_revision_id": str(brief_revision_id),
        "brief_digest": brief_digest,
        "graph_revision_id": str(graph_revision_id),
        "graph_digest": graph_digest,
        "item_id": str(item.item_id),
        "item_digest": item.item_digest,
        "item_disposition": item.disposition,
        "owner_override": owner_override,
        "override_note": override_note,
        "blocker_codes": list(blocker_codes),
        "base_ref": base_ref,
        "base_sha": base_sha,
        "item": {
            "title": item.title,
            "outcome": item.outcome,
            "acceptance_criteria": list(item.acceptance_criteria),
        },
        "requirements": [
            {
                "id": str(value.requirement_id),
                "text": value.text,
                "acceptance_criteria": list(value.acceptance_criteria),
            }
            for value in brief.requirements
            if value.requirement_id in selected
        ],
        "decisions": list(brief.decisions),
        "predecessors": [value.model_dump(mode="json") for value in dependency_evidence],
    }
    validate_durable_payload(payload)
    digest = canonical_digest(payload)
    body = json.dumps(
        {**payload, "context_digest": digest},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("epic task context is too large")
    return body, digest


def _request(value: LaunchRequest | Mapping[str, object]) -> LaunchRequest:
    if isinstance(value, LaunchRequest):
        validate_durable_payload(value.override_note)
        return LaunchRequest.model_validate(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        validate_durable_payload(value.get("override_note"))
    return LaunchRequest.model_validate(value)
