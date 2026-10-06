"""Versioned authenticated edits of the shared epic authoring ceiling."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from forge.application.services.auth import AuthenticatedActor
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.subscription import (
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.persistence.models.api import OperatorAuditEvent
from forge.persistence.models.epic_brainstorm import BrainstormBudgetLedger
from forge.persistence.models.epic_run_bridge import EpicBudgetAdmissionPermit, EpicItemAttempt
from forge.persistence.repositories.epic_budget import PostgresEpicBudgetRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork


class EpicBudgetConflict(RuntimeError):
    """The submitted ceiling version is stale or conflicts with project identity."""


EPIC_DIMENSIONS = frozenset(
    (
        "duration_ms",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "estimated_api_cost_minor",
        "provider_attempts",
    )
)


class EpicBudgetEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_version: int = Field(ge=0, strict=True)
    ceiling: TaskBudget
    disabled_dimensions: tuple[str, ...] = ()
    note: str | None = Field(default=None, max_length=2048)

    @field_validator("disabled_dimensions")
    @classmethod
    def dimensions_are_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not set(value) <= EPIC_DIMENSIONS:
            raise ValueError("disabled epic budget dimensions are invalid")
        return tuple(sorted(value))

    @field_validator("note")
    @classmethod
    def note_is_safe(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("note is invalid")
        validate_durable_payload(value)
        return value


class EpicBudgetReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    epic_id: UUID
    version: int
    ceiling: TaskBudget
    disabled_dimensions: tuple[str, ...] = ()


class EpicBudgetPermitProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    permit_id: UUID
    run_id: UUID
    actor_id: UUID
    warnings: tuple[str, ...]
    consumed_attempt_id: UUID | None
    note: str | None


class EpicBudgetOwnerAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actor_id: UUID
    event_type: str
    version: int | None
    note: str | None


class EpicBudgetProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    epic_id: UUID
    initialized: bool
    version: int
    ceiling: TaskBudget
    disabled_dimensions: tuple[str, ...]
    known: dict[str, int]
    held: dict[str, int]
    unknown: bool
    currency: str | None
    warnings: tuple[str, ...]
    permits: tuple[EpicBudgetPermitProjection, ...]
    owner_actions: tuple[EpicBudgetOwnerAction, ...]


class EpicBudgetPermitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_version: int = Field(ge=0, strict=True)
    run_id: UUID
    note: str | None = Field(default=None, max_length=2048)

    @field_validator("note")
    @classmethod
    def note_is_safe(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("note is invalid")
        validate_durable_payload(value)
        return value


class EpicBudgetPermitReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    permit_id: UUID
    epic_id: UUID
    run_id: UUID
    actor_id: UUID
    budget_version: int
    warnings: tuple[str, ...]


class EpicBudgetService:
    def __init__(
        self,
        work_factory: Callable[[], PostgresUnitOfWork],
        *,
        default_ceiling: TaskBudget | None = None,
        child_hold: TaskBudget | None = None,
    ) -> None:
        self._work = work_factory
        self._default_ceiling = default_ceiling or TaskBudget()
        self._child_hold = child_hold or TaskBudget(max_provider_attempts=1)

    async def permit(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: EpicBudgetPermitRequest,
    ) -> EpicBudgetPermitReceipt:
        digest = canonical_digest(
            {"epic_id": str(epic_id), "request": request.model_dump(mode="json")}
        )
        async with self._work() as work:
            mutation = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.budget.permit",
                scope=f"epic:{epic_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if mutation.is_replay:
                if mutation.response_payload is None:
                    raise EpicBudgetConflict("budget admission receipt is incomplete")
                result = EpicBudgetPermitReceipt.model_validate(mutation.response_payload)
                await work.commit()
                return result
            epic = await work.epics.get(epic_id, for_update=True)
            child = await work.session.scalar(
                select(EpicItemAttempt).where(
                    EpicItemAttempt.run_id == request.run_id,
                    EpicItemAttempt.epic_id == epic_id,
                )
            )
            if child is None:
                raise EpicBudgetConflict("run is not bound to this epic")
            ledger = await work.session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
            actual_version = ledger.version if ledger is not None else 0
            if actual_version != request.expected_version:
                raise EpicBudgetConflict("epic budget version is stale")
            if ledger is None:
                ledger = BrainstormBudgetLedger(
                    epic_id=epic_id,
                    project_id=epic.project_id,
                    ceiling=encode_subscription_record(self._default_ceiling),
                    version=1,
                    disabled_dimensions=[],
                )
                work.session.add(ledger)
            else:
                ledger.version += 1
            budget = PostgresEpicBudgetRepository(work.session, legacy_hold=self._child_hold)
            warnings = await budget.internal_blockers(request.run_id, self._child_hold)
            permit = EpicBudgetAdmissionPermit(
                id=uuid4(),
                epic_id=epic_id,
                run_id=request.run_id,
                actor_id=actor.actor_id,
                note=request.note,
                warnings=warnings,
            )
            work.session.add(permit)
            await work.session.flush()
            result = EpicBudgetPermitReceipt(
                permit_id=permit.id,
                epic_id=epic_id,
                run_id=request.run_id,
                actor_id=actor.actor_id,
                budget_version=ledger.version,
                warnings=tuple(warnings),
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.budget_admission_permitted",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=mutation.id,
                payload={
                    "permit_id": str(permit.id),
                    "run_id": str(request.run_id),
                    "budget_version": ledger.version,
                    "warnings": warnings,
                    "note": request.note,
                },
            )
            await work.mutations.complete(
                mutation.id,
                response_status=201,
                response_payload=result.model_dump(mode="json"),
                resource_kind="epic_budget_permit",
                resource_id=permit.id,
            )
            await work.commit()
            return result

    async def edit(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: EpicBudgetEdit,
    ) -> EpicBudgetReceipt:
        digest = canonical_digest(
            {"epic_id": str(epic_id), "request": request.model_dump(mode="json")}
        )
        async with self._work() as work:
            mutation = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.budget.edit",
                scope=f"epic:{epic_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if mutation.is_replay:
                if mutation.response_payload is None:
                    raise EpicBudgetConflict("budget edit receipt is incomplete")
                result = EpicBudgetReceipt.model_validate(mutation.response_payload)
                await work.commit()
                return result
            epic = await work.epics.get(epic_id, for_update=True)
            row = await work.session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
            actual_version = row.version if row is not None else 0
            if actual_version != request.expected_version:
                raise EpicBudgetConflict("epic budget version is stale")
            if row is None:
                row = BrainstormBudgetLedger(
                    epic_id=epic_id,
                    project_id=epic.project_id,
                    ceiling=encode_subscription_record(request.ceiling),
                    version=1,
                    disabled_dimensions=list(request.disabled_dimensions),
                )
                work.session.add(row)
            else:
                if row.project_id != epic.project_id:
                    raise EpicBudgetConflict("epic budget project binding conflicts")
                row.ceiling = encode_subscription_record(request.ceiling)
                row.disabled_dimensions = list(request.disabled_dimensions)
                row.version += 1
            await work.session.flush()
            result = EpicBudgetReceipt(
                epic_id=epic_id,
                version=row.version,
                ceiling=request.ceiling,
                disabled_dimensions=request.disabled_dimensions,
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.budget_edited",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=mutation.id,
                payload={
                    "version": row.version,
                    "ceiling": encode_subscription_record(request.ceiling),
                    "disabled_dimensions": list(request.disabled_dimensions),
                    "note": request.note,
                },
            )
            await work.mutations.complete(
                mutation.id,
                response_status=200,
                response_payload=result.model_dump(mode="json"),
                resource_kind="epic_budget",
                resource_id=epic_id,
            )
            await work.commit()
            return result

    async def get(self, epic_id: UUID) -> EpicBudgetProjection:
        async with self._work() as work:
            await work.epics.get(epic_id)
            row = await work.session.get(BrainstormBudgetLedger, epic_id)
            ceiling = decode_subscription_record(row.ceiling) if row else self._default_ceiling
            if not isinstance(ceiling, TaskBudget):
                raise EpicBudgetConflict("stored epic budget is invalid")
            totals = await PostgresEpicBudgetRepository(
                work.session, legacy_hold=self._child_hold
            ).totals(epic_id)
            permits = (
                await work.session.scalars(
                    select(EpicBudgetAdmissionPermit)
                    .where(EpicBudgetAdmissionPermit.epic_id == epic_id)
                    .order_by(EpicBudgetAdmissionPermit.created_at, EpicBudgetAdmissionPermit.id)
                )
            ).all()
            actions = (
                await work.session.scalars(
                    select(OperatorAuditEvent)
                    .where(
                        OperatorAuditEvent.subject_type == "epic",
                        OperatorAuditEvent.subject_id == epic_id,
                        OperatorAuditEvent.event_type.in_(
                            (
                                "epic.budget_edited",
                                "epic.budget_admission_permitted",
                            )
                        ),
                    )
                    .order_by(OperatorAuditEvent.created_at, OperatorAuditEvent.id)
                )
            ).all()
            result = EpicBudgetProjection(
                epic_id=epic_id,
                initialized=row is not None,
                version=row.version if row else 0,
                ceiling=ceiling,
                disabled_dimensions=tuple(row.disabled_dimensions) if row else (),
                known=totals.known,
                held=totals.held,
                unknown=totals.unknown,
                currency=totals.currency,
                warnings=totals.warnings,
                permits=tuple(
                    EpicBudgetPermitProjection(
                        permit_id=value.id,
                        run_id=value.run_id,
                        actor_id=value.actor_id,
                        warnings=tuple(value.warnings),
                        consumed_attempt_id=value.consumed_attempt_id,
                        note=value.note,
                    )
                    for value in permits
                ),
                owner_actions=tuple(
                    EpicBudgetOwnerAction(
                        actor_id=value.actor_id,
                        event_type=value.event_type,
                        version=value.payload.get("version", value.payload.get("budget_version")),
                        note=value.payload.get("note"),
                    )
                    for value in actions
                ),
            )
            await work.commit()
            return result
