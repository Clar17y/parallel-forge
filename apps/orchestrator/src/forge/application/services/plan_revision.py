"""Bounded revision command boundary (expanded by the semantic replanning slice)."""

from __future__ import annotations

import hashlib
import json

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approvals import ApprovalCommandValidationError
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.plan_restart import restart_source
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunState


class PlanRevisionService:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        evidence_validator: PlanEvidenceValidator,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._evidence_validator = evidence_validator
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "request_plan_revision"
            or command.status is not CommandStatus.LEASED
            or command.actor_id is None
        ):
            raise ApprovalCommandValidationError("plan revision is not admissible")
        feedback = command.payload.get("feedback")
        if (
            set(command.payload) != {"feedback"}
            or not isinstance(feedback, str)
            or not feedback.strip()
            or len(feedback.encode()) > 16_384
        ):
            raise ApprovalCommandValidationError("plan revision is not admissible")
        feedback_bytes = json.dumps(
            {"schema_version": 1, "command_id": str(command.id), "feedback": feedback},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        run = await work.runs.get_for_update(command.run_id)
        if (
            run.state is RunState.PLANNING
            and run.version == command.expected_run_version + 1
            and await self._committed_restart(
                command, work, hashlib.sha256(feedback_bytes).hexdigest()
            )
        ):
            # The gate transaction committed before the worker acknowledged its
            # queue command. Exact persisted linkage proves this is a replay.
            await work.commit()
            return
        if (
            run.state is not RunState.AWAITING_PLAN_APPROVAL
            or run.version != command.expected_run_version
        ):
            raise ApprovalCommandValidationError("plan revision is not admissible")
        descriptor = await self._artifact_store.put_bytes(
            feedback_bytes,
            media_type="application/json",
            max_bytes=131_072,
            bounding_policy="head_tail",
        )
        persisted = await work.artifacts.record(
            descriptor,
            run_id=run.id,
            producer_type="plan_revision_feedback",
            producer_id=command.id,
        )
        source = await self._evidence_validator.current_source(work, run.id)
        await work.auth.invalidate_plan_gate(
            run_id=run.id, run_version=run.version, at=self._clock.now()
        )
        attempt = await work.executions.next_attempt(run.id, "plan")
        attempt = max(attempt, 2)
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="start_planning",
            idempotency_key=f"{run.id}:start-planning:{attempt}",
            payload={"semantic_attempt": attempt, "feedback_digest": persisted.digest},
            expected_run_version=run.version + 1,
            actor_id=command.actor_id,
        )
        await work.runs.restart_planning(
            run.id,
            run.version,
            policy_version=source.policy_version,
            base_ref=source.base_ref,
            base_sha=source.base_sha,
            event_type="run.plan_revision_requested",
            event_payload={
                "command_id": str(command.id),
                "planning_command_id": str(queued.id),
                "planning_payload": dict(queued.payload),
                "feedback_digest": persisted.digest,
                "semantic_attempt": attempt,
            },
            actor_class="operator",
            actor_id=command.actor_id,
            occurred_at=self._clock.now() if self._clock else None,
        )
        await work.commit()

    async def _committed_restart(
        self, command: CommandEnvelope, work: UnitOfWork, feedback_digest: str
    ) -> bool:
        artifacts = await work.artifacts.get_by_producer(
            run_id=command.run_id, producer_type="plan_revision_feedback", producer_id=command.id
        )
        if (
            len(artifacts) != 1
            or artifacts[0].digest != feedback_digest
            or artifacts[0].run_id != command.run_id
            or artifacts[0].producer_id != command.id
            or artifacts[0].truncated
        ):
            return False
        attempt = await work.executions.next_attempt(command.run_id, "plan")
        queued = await work.commands.get_by_idempotency_key(
            f"{command.run_id}:start-planning:{attempt}"
        )
        matches = (
            queued is not None
            and queued.run_id == command.run_id
            and queued.actor_id == command.actor_id
            and queued.command_type == "start_planning"
            and queued.status is CommandStatus.PENDING
            and queued.expected_run_version == command.expected_run_version + 1
            and queued.payload == {"semantic_attempt": attempt, "feedback_digest": feedback_digest}
        )
        if not matches or queued is None:
            return False
        source = await restart_source(work, queued, source_status=CommandStatus.LEASED)
        return source is not None and source.id == command.id


__all__ = ["PlanRevisionService"]
