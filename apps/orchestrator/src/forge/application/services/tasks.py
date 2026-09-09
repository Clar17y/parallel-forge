"""Immutable task-source application services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.projects import ProjectRepository
from forge.application.ports.tasks import TaskRecord, TaskRepository
from forge.application.services.auth import AuthenticatedActor


class TaskServiceError(RuntimeError):
    """A bounded task application failure."""


class TaskUnitOfWork(Protocol):
    projects: ProjectRepository
    tasks: TaskRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...


class PlainTextTaskRequest(BaseModel):
    """Exact source fields accepted for an operator-created task."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(max_length=1_048_576)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task title must not be blank")
        if len(value.encode("utf-8")) > 512:
            raise ValueError("task title is too long")
        return value

    @field_validator("body")
    @classmethod
    def body_must_fit_storage_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1_048_576:
            raise ValueError("task body is too long")
        return value


PlainTextTask = PlainTextTaskRequest


class ExternalTaskRequest(PlainTextTaskRequest):
    """Closed source contract for later external issue imports."""

    external_source: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=255)
    source_url: str | None = Field(default=None, max_length=2048)
    source_updated_at: datetime | None = None

    @field_validator("external_source", "external_id")
    @classmethod
    def external_identity_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("external task identity must not be blank")
        return value

    @field_validator("source_updated_at")
    @classmethod
    def source_time_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("task source update time must be timezone-aware")
        return value


ExternalTask = ExternalTaskRequest


class TaskService:
    """Coordinate immutable task persistence, durable replay, and operator audit."""

    def __init__(self, unit_of_work_factory: Callable[[], TaskUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def create_plain_text(
        self,
        *,
        actor: AuthenticatedActor,
        idempotency_key: str,
        request: PlainTextTaskRequest,
    ) -> TaskRecord:
        request = _coerce_request(request, PlainTextTaskRequest)
        request_digest = _digest(request.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="task.create",
                scope=f"project:{request.project_id}",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                task = await work.tasks.get(_resource_id(receipt))
                await work.commit()
                return task
            task = await work.tasks.create(
                task_id=uuid4(),
                project_id=request.project_id,
                title=request.title,
                body=request.body,
            )
            await self._append_audit(work, actor, receipt, task, request_digest)
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": str(task.id), "task_digest": task.task_digest},
                resource_kind="task",
                resource_id=task.id,
            )
            await work.commit()
            return task

    async def create_from_external(
        self,
        *,
        actor: AuthenticatedActor,
        idempotency_key: str,
        request: ExternalTaskRequest,
    ) -> TaskRecord:
        request = _coerce_request(request, ExternalTaskRequest)
        request_digest = _digest(request.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="task.create",
                scope=f"project:{request.project_id}",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                task = await work.tasks.get(_resource_id(receipt))
                await work.commit()
                return task
            task = await work.tasks.create(
                task_id=uuid4(),
                project_id=request.project_id,
                title=request.title,
                body=request.body,
                source_url=request.source_url,
                source_updated_at=request.source_updated_at,
                external_source=request.external_source,
                external_id=request.external_id,
            )
            await self._append_audit(work, actor, receipt, task, request_digest)
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": str(task.id), "task_digest": task.task_digest},
                resource_kind="task",
                resource_id=task.id,
            )
            await work.commit()
            return task

    async def list(self, project_id: UUID) -> Sequence[TaskRecord]:
        async with self._unit_of_work_factory() as work:
            records = await work.tasks.list(project_id)
            await work.commit()
            return records

    async def get(self, task_id: UUID) -> TaskRecord:
        async with self._unit_of_work_factory() as work:
            record = await work.tasks.get(task_id)
            await work.commit()
            return record

    @staticmethod
    async def _append_audit(
        work: TaskUnitOfWork,
        actor: AuthenticatedActor,
        receipt: ApiMutationRecord,
        task: TaskRecord,
        request_digest: str,
    ) -> None:
        identity = None
        if task.external_source is not None:
            identity = _digest(
                {"external_source": task.external_source, "external_id": task.external_id}
            )
        await work.audit.append(
            actor_id=actor.actor_id,
            event_type="task.created",
            subject_type="task",
            subject_id=task.id,
            correlation_id=receipt.id,
            payload={
                "request_digest": request_digest,
                "task_digest": task.task_digest,
                "external_identity_digest": identity,
                "untrusted_external_content": task.untrusted_external_content,
            },
        )


def _coerce_request(value: object, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("request must be a validated task request")


def _resource_id(receipt: ApiMutationRecord) -> UUID:
    if receipt.resource_kind != "task" or receipt.resource_id is None:
        raise TaskServiceError("mutation receipt resource is unavailable")
    return receipt.resource_id


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonicalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(item) for item in value), key=lambda item: repr(item))
    return value


__all__ = [
    "ExternalTask",
    "ExternalTaskRequest",
    "PlainTextTask",
    "PlainTextTaskRequest",
    "TaskService",
    "TaskServiceError",
]
