"""Authenticated owner configuration of durable per-epoch dispatch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_run_bridge import EpicExecutionNotFound
from forge.domain.operation import canonical_digest
from forge.persistence.models.epic_dispatch import EpicDispatchSetting
from forge.persistence.models.epic_run_bridge import EpicExecution, EpicExecutionControl
from forge.persistence.unit_of_work import PostgresUnitOfWork


class EpicDispatchConflict(RuntimeError):
    """The owner edited an outdated dispatch setting."""


class EpicDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_dispatch_version: int = Field(ge=0, strict=True)
    enabled: bool = Field(strict=True)
    profile_id: UUID | None = None
    profile_version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def profile_pair(self) -> EpicDispatchRequest:
        if (self.profile_id is None) != (self.profile_version is None):
            raise ValueError("profile selection must include ID and version")
        return self


class EpicDispatchProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    execution_id: UUID
    version: int
    enabled: bool
    profile_id: UUID | None
    profile_version: int | None
    enabled_by_actor_id: UUID | None
    claim_item_id: UUID | None
    claim_expires_at: datetime | None
    blocker_code: str | None


def _project(execution_id: UUID, row: EpicDispatchSetting | None) -> EpicDispatchProjection:
    return EpicDispatchProjection(
        execution_id=execution_id,
        version=row.version if row else 0,
        enabled=row.enabled if row else False,
        profile_id=row.profile_id if row else None,
        profile_version=row.profile_version if row else None,
        enabled_by_actor_id=row.actor_id if row else None,
        claim_item_id=row.claim_item_id if row else None,
        claim_expires_at=row.claim_expires_at if row else None,
        blocker_code=row.blocker_code if row else None,
    )


class EpicDispatchService:
    def __init__(self, unit_of_work_factory: Callable[[], PostgresUnitOfWork]) -> None:
        self._work = unit_of_work_factory

    async def get(self, epic_id: UUID, execution_id: UUID) -> EpicDispatchProjection:
        async with self._work() as work:
            execution = await work.session.get(EpicExecution, execution_id)
            if execution is None or execution.epic_id != epic_id:
                raise EpicExecutionNotFound("execution was not found")
            row = await work.session.get(EpicDispatchSetting, execution_id)
            result = _project(execution_id, row)
            await work.commit()
            return result

    async def configure(
        self, *, actor: AuthenticatedActor, epic_id: UUID, execution_id: UUID,
        idempotency_key: str, request: EpicDispatchRequest,
    ) -> EpicDispatchProjection:
        digest = canonical_digest({
            "epic_id": str(epic_id), "execution_id": str(execution_id),
            "request": request.model_dump(mode="json"),
        })
        async with self._work() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id, action="epic.dispatch.configure",
                scope=f"epic:{epic_id}:execution:{execution_id}",
                idempotency_key=idempotency_key, request_digest=digest,
            )
            if receipt.is_replay:
                if receipt.response_payload is None:
                    raise EpicDispatchConflict("dispatch receipt has no response")
                result = EpicDispatchProjection.model_validate(receipt.response_payload)
                await work.commit()
                return result
            await work.epics.get(epic_id, for_update=True)
            execution = await work.session.get(EpicExecution, execution_id)
            if execution is None or execution.epic_id != epic_id:
                raise EpicExecutionNotFound("execution was not found")
            row = await work.session.scalar(
                select(EpicDispatchSetting)
                .where(EpicDispatchSetting.execution_id == execution_id)
                .with_for_update()
            )
            actual_version = row.version if row else 0
            if actual_version != request.expected_dispatch_version:
                raise EpicDispatchConflict("dispatch version is stale")
            if request.profile_id is not None and request.profile_version is not None:
                profile = await work.subscription.profile(request.profile_id, request.profile_version)
                if profile is None:
                    raise EpicDispatchConflict("profile does not exist")
            control = await work.session.get(EpicExecutionControl, execution_id)
            warning = "execution_not_active" if control is None or control.state != "ACTIVE" else None
            if row is None:
                row = EpicDispatchSetting(
                    execution_id=execution_id, epic_id=epic_id, version=1,
                    enabled=request.enabled, actor_id=actor.actor_id, session_id=actor.session_id,
                    profile_id=request.profile_id, profile_version=request.profile_version,
                    blocker_code=warning,
                )
                work.session.add(row)
            else:
                row.version += 1
                row.enabled = request.enabled
                row.actor_id = actor.actor_id
                row.session_id = actor.session_id
                row.profile_id = request.profile_id
                row.profile_version = request.profile_version
                row.blocker_code = warning
                row.claim_item_id = None
                row.claim_token = None
                row.claim_expires_at = None
                row.checked_at = None
            await work.session.flush()
            result = _project(execution_id, row)
            await work.audit.append(
                actor_id=actor.actor_id, event_type="epic.dispatch_configured",
                subject_type="epic", subject_id=epic_id, correlation_id=receipt.id,
                payload={
                    "execution_id": str(execution_id), "dispatch_version": row.version,
                    "enabled": request.enabled, "profile_id": str(request.profile_id) if request.profile_id else None,
                    "profile_version": request.profile_version, "warnings": [warning] if warning else [],
                },
            )
            await work.mutations.complete(
                receipt.id, response_status=200, response_payload=result.model_dump(mode="json"),
                resource_kind="epic_execution", resource_id=execution_id,
            )
            await work.commit()
            return result
