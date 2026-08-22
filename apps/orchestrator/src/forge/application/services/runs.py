"""Atomic run creation and closed operator run-command services."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.ports.audit import AuditRepository
from forge.application.ports.commands import CommandRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.projects import ProjectRepository, RepositoryInspector
from forge.application.ports.runs import RunRepository
from forge.application.ports.tasks import TaskRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import IdempotencyConflict
from forge.persistence.repositories.runs import (
    ConcurrencyConflict,
    RunCreationError,
)
from forge.settings import Settings

_BASE_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_IDEMPOTENCY_KEY_BYTES = 255
_MAX_FEEDBACK_BYTES = 4096
_TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})


class RunServiceError(RuntimeError):
    """A bounded run application failure."""


class RunCommandValidationError(ValueError):
    """A closed command request does not match the locked run state."""


class RunUnitOfWork(Protocol):
    projects: ProjectRepository
    tasks: TaskRepository
    runs: RunRepository
    events: object
    commands: CommandRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...


@runtime_checkable
class EventWriter(Protocol):
    async def append(self, event: RunEvent) -> RunEvent: ...


class RunCommandType(StrEnum):
    """Closed command names accepted by the operator command boundary."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    REQUEST_PLAN_REVISION = "request_plan_revision"
    REQUEST_CANDIDATE_CHANGES = "request_candidate_changes"
    REJECT_MERGE = "reject_merge"
    TEARDOWN_RUN_RESOURCES = "teardown_run_resources"


_COMMAND_NAMES = frozenset(
    {
        RunCommandType.PAUSE,
        RunCommandType.RESUME,
        RunCommandType.CANCEL,
        RunCommandType.REQUEST_PLAN_REVISION,
        RunCommandType.REQUEST_CANDIDATE_CHANGES,
        RunCommandType.REJECT_MERGE,
        RunCommandType.TEARDOWN_RUN_RESOURCES,
    }
)


class RunCommandRequest(BaseModel):
    """Closed operator command request independent of HTTP transport."""

    model_config = ConfigDict(extra="forbid")

    command_type: str
    expected_run_version: int = Field(ge=0)
    feedback: str | None = None
    delete_branch: bool = False
    confirm_branch_name: str | None = Field(default=None, max_length=512)

    @field_validator("command_type")
    @classmethod
    def command_type_must_be_closed(cls, value: str) -> str:
        if value not in _COMMAND_NAMES:
            raise ValueError("unsupported run command")
        return value

    @field_validator("feedback")
    @classmethod
    def feedback_must_be_bounded(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip() or "\x00" in value:
                raise ValueError("feedback must be nonblank")
            if len(value.encode("utf-8")) > _MAX_FEEDBACK_BYTES:
                raise ValueError("feedback is too long")
        return value

    @field_validator("confirm_branch_name")
    @classmethod
    def branch_confirmation_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("branch confirmation must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_command_shape(self) -> RunCommandRequest:
        feedback_commands = {
            RunCommandType.REQUEST_PLAN_REVISION,
            RunCommandType.REQUEST_CANDIDATE_CHANGES,
            RunCommandType.REJECT_MERGE,
        }
        if self.command_type in feedback_commands:
            if self.feedback is None:
                raise ValueError("feedback is required for this command")
            if self.delete_branch or self.confirm_branch_name is not None:
                raise ValueError("feedback commands do not accept teardown fields")
        elif self.feedback is not None:
            raise ValueError("feedback is not accepted for this command")

        if self.command_type == RunCommandType.TEARDOWN_RUN_RESOURCES:
            if self.delete_branch and self.confirm_branch_name is None:
                raise ValueError("branch confirmation is required when deleting a branch")
        elif self.delete_branch or self.confirm_branch_name is not None:
            raise ValueError("teardown fields are only accepted for teardown")
        return self


class RunService:
    """Create and query runs inside one PostgreSQL unit-of-work transaction."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], RunUnitOfWork],
        *,
        repository_inspector: RepositoryInspector | None = None,
        settings: Settings | None = None,
        data_root: str | Path | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_inspector = repository_inspector or LocalGitRepositoryInspector()
        configured_root: str | Path
        if settings is not None:
            configured_root = settings.data_root
        elif data_root is not None:
            configured_root = data_root
        else:
            configured_root = Settings().data_root
        self._data_root = str(configured_root)

    async def create_run(
        self,
        *,
        actor: AuthenticatedActor,
        idempotency_key: str,
        task_id: UUID,
    ) -> RunSnapshot:
        """Atomically create a CREATED run, event, planning command, and receipt."""

        request_digest = _digest({"task_id": str(task_id)})
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="create_run",
                scope=f"task:{task_id}",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                run = await work.runs.get(_resource_id(receipt, "run"))
                await work.commit()
                return run

            task = await work.tasks.get(task_id, for_update=True)
            project = await work.projects.get(task.project_id, for_update=True)
            if project.policy is None or project.current_policy_version is None:
                raise RunCreationError("project has no current policy")
            inspection = self._repository_inspector.inspect(
                repository_path=project.canonical_path,
                data_root=self._data_root,
                github_repository=project.github_repository,
                default_branch=project.default_branch,
            )
            base_ref = f"refs/heads/{project.default_branch}"
            if (
                inspection.default_branch != project.default_branch
                or inspection.base_ref != base_ref
                or _BASE_SHA.fullmatch(inspection.base_sha) is None
            ):
                raise RunCreationError("default branch binding is invalid")
            run = RunSnapshot(
                id=uuid4(),
                project_id=project.id,
                task_id=task.id,
                policy_version=project.current_policy_version,
                base_ref=base_ref,
                base_sha=inspection.base_sha,
            )
            await work.runs.create(run)
            event_writer = _event_writer(work.events)
            await event_writer.append(
                RunEvent(
                    run_id=run.id,
                    run_version=0,
                    event_type="run.created",
                    actor_class="operator",
                    actor_id=actor.actor_id,
                    payload={
                        "project_id": str(project.id),
                        "task_id": str(task.id),
                        "task_digest": task.task_digest,
                        "policy_version": run.policy_version,
                        "base_ref": run.base_ref,
                        "base_sha": run.base_sha,
                    },
                )
            )
            await work.commands.enqueue(
                run_id=run.id,
                command_type="start_planning",
                idempotency_key=f"{run.id}:start-planning",
                payload={},
                expected_run_version=0,
                actor_id=actor.actor_id,
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": str(run.id), "state": run.state.value},
                resource_kind="run",
                resource_id=run.id,
            )
            await work.commit()
            return run

    async def get(self, run_id: UUID) -> RunSnapshot:
        async with self._unit_of_work_factory() as work:
            run = await work.runs.get(run_id)
            await work.commit()
            return run

    async def list(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> Sequence[RunSnapshot]:
        async with self._unit_of_work_factory() as work:
            runs = await work.runs.list(project_id=project_id, task_id=task_id)
            await work.commit()
            return runs


class RunCommandService:
    """Enqueue one closed operator command without changing run state."""

    def __init__(self, unit_of_work_factory: Callable[[], RunUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def enqueue(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        idempotency_key: str,
        request: RunCommandRequest,
    ) -> CommandEnvelope:
        request = _coerce_request(request, RunCommandRequest)
        queue_key = hash_run_command_idempotency_key(
            idempotency_key, actor_id=actor.actor_id, run_id=run_id
        )
        async with self._unit_of_work_factory() as work:
            existing = await work.commands.get_by_idempotency_key(queue_key)
            if existing is not None:
                _validate_command_replay(
                    existing,
                    run_id=run_id,
                    actor_id=actor.actor_id,
                    request=request,
                )
                await work.commit()
                return existing
            run = await work.runs.get_for_update(run_id)
            # A concurrent producer may have committed while this request was
            # waiting for the run lock. Re-read the queue key before checking
            # the current run version/state so a retry remains idempotent even
            # after the worker has advanced the run.
            existing = await work.commands.get_by_idempotency_key(queue_key)
            if existing is not None:
                _validate_command_replay(
                    existing,
                    run_id=run_id,
                    actor_id=actor.actor_id,
                    request=request,
                )
                await work.commit()
                return existing
            if request.expected_run_version != run.version:
                raise ConcurrencyConflict(run_id, request.expected_run_version, run.version)
            _validate_state(run.state, request.command_type)
            payload = _command_payload(request, run)
            command = await work.commands.enqueue(
                run_id=run_id,
                command_type=request.command_type,
                idempotency_key=queue_key,
                payload=payload,
                expected_run_version=request.expected_run_version,
                actor_id=actor.actor_id,
            )
            await work.commit()
            return command

    async def enqueue_command(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        idempotency_key: str,
        request: RunCommandRequest,
    ) -> CommandEnvelope:
        """Descriptive alias for callers that name the operation explicitly."""

        return await self.enqueue(
            actor=actor,
            run_id=run_id,
            idempotency_key=idempotency_key,
            request=request,
        )


RunApplicationService = RunService
RunCreationService = RunService
RunCommandApplicationService = RunCommandService
RunCommandInput = RunCommandRequest


def hash_run_command_idempotency_key(raw_key: str, *, actor_id: UUID, run_id: UUID) -> str:
    """Hash the raw header into a bounded actor/run/route-scoped queue key."""

    if (
        not isinstance(raw_key, str)
        or not raw_key
        or len(raw_key.encode("utf-8")) > _MAX_IDEMPOTENCY_KEY_BYTES
    ):
        raise ValueError("idempotency key is invalid")
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"run-command:commands:{actor_id}:{run_id}:{digest}"


def _validate_state(state: RunState, command_type: str) -> None:
    allowed = {
        RunCommandType.PAUSE: set(RunState) - _TERMINAL_STATES - {RunState.PAUSED},
        RunCommandType.RESUME: {RunState.PAUSED},
        RunCommandType.CANCEL: set(RunState) - _TERMINAL_STATES,
        RunCommandType.REQUEST_PLAN_REVISION: {RunState.AWAITING_PLAN_APPROVAL},
        RunCommandType.REQUEST_CANDIDATE_CHANGES: {RunState.AWAITING_PR_APPROVAL},
        RunCommandType.REJECT_MERGE: {RunState.AWAITING_MERGE_APPROVAL},
        RunCommandType.TEARDOWN_RUN_RESOURCES: set(_TERMINAL_STATES),
    }
    if state not in allowed[RunCommandType(command_type)]:
        raise RunCommandValidationError("command is not valid for the current run state")


def _command_payload(request: RunCommandRequest, run: RunSnapshot) -> dict[str, object]:
    if request.command_type in {
        RunCommandType.REQUEST_PLAN_REVISION,
        RunCommandType.REQUEST_CANDIDATE_CHANGES,
        RunCommandType.REJECT_MERGE,
    }:
        if request.feedback is None:
            raise RunCommandValidationError("feedback is required for this command")
        return {"feedback": request.feedback}
    if request.command_type == RunCommandType.TEARDOWN_RUN_RESOURCES:
        if request.delete_branch:
            if run.branch_name is None or request.confirm_branch_name != run.branch_name:
                raise RunCommandValidationError("branch confirmation does not match the run")
            return {
                "delete_branch": True,
                "confirm_branch_name": request.confirm_branch_name,
            }
        return {"delete_branch": False}
    return {}


def _validate_command_replay(
    existing: CommandEnvelope,
    *,
    run_id: UUID,
    actor_id: UUID,
    request: RunCommandRequest,
) -> None:
    """Require an existing queue key to carry the exact original request."""

    expected_payload = _request_payload(request)
    if (
        existing.run_id != run_id
        or existing.actor_id != actor_id
        or existing.command_type != request.command_type
        or existing.expected_run_version != request.expected_run_version
        or dict(existing.payload) != expected_payload
    ):
        raise IdempotencyConflict("command idempotency key was reused for a different request")


def _request_payload(request: RunCommandRequest) -> dict[str, object]:
    """Build a request fingerprint without consulting mutable run state."""

    if request.command_type in {
        RunCommandType.REQUEST_PLAN_REVISION,
        RunCommandType.REQUEST_CANDIDATE_CHANGES,
        RunCommandType.REJECT_MERGE,
    }:
        return {"feedback": request.feedback}
    if request.command_type == RunCommandType.TEARDOWN_RUN_RESOURCES:
        if request.delete_branch:
            return {
                "delete_branch": True,
                "confirm_branch_name": request.confirm_branch_name,
            }
        return {"delete_branch": False}
    return {}


def _event_writer(value: object) -> EventWriter:
    if not isinstance(value, EventWriter):
        raise RunServiceError("run event repository is unavailable")
    return value


def _resource_id(receipt: ApiMutationRecord, expected_kind: str) -> UUID:
    if receipt.resource_kind != expected_kind or receipt.resource_id is None:
        raise RunServiceError("mutation receipt resource is unavailable")
    return receipt.resource_id


def _coerce_request(value: object, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("request must be a validated run command request")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "RunApplicationService",
    "RunCommandApplicationService",
    "RunCommandInput",
    "RunCommandRequest",
    "RunCommandService",
    "RunCommandType",
    "RunCommandValidationError",
    "RunCreationService",
    "RunService",
    "RunServiceError",
    "hash_run_command_idempotency_key",
]
