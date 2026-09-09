"""PostgreSQL idempotency receipt repository."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.mutations import ApiMutationRecord
from forge.domain.payload import validate_durable_payload
from forge.persistence.models import ApiMutation

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_IDEMPOTENCY_KEY_BYTES = 255


class MutationRepositoryError(RuntimeError):
    """Base for safe mutation receipt errors."""


class MutationNotFound(MutationRepositoryError):
    """The requested receipt does not exist."""


class MutationConflict(MutationRepositoryError):
    """A key was reused for a different immutable request."""


class MutationIncomplete(MutationRepositoryError):
    """A visible unfinished receipt fails closed."""


class PostgresMutationRepository:
    """Reserve and complete hashed-key receipts in one caller transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self,
        *,
        actor_id: UUID,
        action: str,
        scope: str,
        idempotency_key: str,
        request_digest: str,
    ) -> ApiMutationRecord:
        """Insert one reservation or return an identical completed replay."""

        key_hash = hash_idempotency_key(idempotency_key)
        _validate_digest(request_digest, "request digest")
        _validate_text(action, 96, "mutation action")
        _validate_text(scope, 255, "mutation scope")
        statement = (
            insert(ApiMutation)
            .values(
                id=uuid4(),
                actor_id=actor_id,
                action=action,
                scope=scope,
                key_hash=key_hash,
                request_digest=request_digest,
                lifecycle_state="RESERVED",
            )
            .on_conflict_do_nothing(
                index_elements=[ApiMutation.actor_id, ApiMutation.action, ApiMutation.key_hash]
            )
            .returning(ApiMutation.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            record = await self._session.get(ApiMutation, inserted_id)
            if record is None:
                raise MutationRepositoryError("mutation receipt disappeared")
            return _mutation_from_record(record, is_replay=False)

        existing = (
            await self._session.execute(
                select(ApiMutation)
                .where(
                    ApiMutation.actor_id == actor_id,
                    ApiMutation.action == action,
                    ApiMutation.key_hash == key_hash,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            raise MutationRepositoryError("mutation receipt could not be loaded")
        if existing.scope != scope or existing.request_digest != request_digest:
            raise MutationConflict("idempotency key was reused for a different request")
        if existing.lifecycle_state != "COMPLETED":
            raise MutationIncomplete("idempotency receipt is incomplete")
        return _mutation_from_record(existing, is_replay=True)

    async def complete(
        self,
        mutation_id: UUID,
        *,
        response_status: int,
        response_payload: Mapping[str, object],
        resource_kind: str | None = None,
        resource_id: UUID | None = None,
    ) -> ApiMutationRecord:
        """Complete one reservation with only a safe bounded response."""

        if type(response_status) is not int or not 100 <= response_status <= 599:
            raise ValueError("mutation response status is invalid")
        if not isinstance(resource_kind, str) or not resource_kind.strip() or resource_id is None:
            raise MutationRepositoryError("completed mutation requires a resource kind and id")
        payload = dict(response_payload)
        validate_durable_payload(payload)
        record = (
            await self._session.execute(
                select(ApiMutation).where(ApiMutation.id == mutation_id).with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise MutationNotFound("mutation receipt was not found")
        if record.lifecycle_state == "COMPLETED":
            if (
                record.response_status != response_status
                or record.response_payload != payload
                or record.resource_kind != resource_kind
                or record.resource_id != resource_id
            ):
                raise MutationConflict("mutation receipt response cannot be changed")
            return _mutation_from_record(record, is_replay=True)
        record.lifecycle_state = "COMPLETED"
        record.response_status = response_status
        record.response_payload = payload
        record.resource_kind = resource_kind
        record.resource_id = resource_id
        await self._session.flush()
        return _mutation_from_record(record, is_replay=False)

    async def get(self, mutation_id: UUID) -> ApiMutationRecord:
        """Load one receipt without exposing any raw idempotency key."""

        record = await self._session.get(ApiMutation, mutation_id)
        if record is None:
            raise MutationNotFound("mutation receipt was not found")
        return _mutation_from_record(record, is_replay=False)


def hash_idempotency_key(value: str) -> str:
    """Hash the caller's raw key before it crosses the persistence boundary."""

    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_IDEMPOTENCY_KEY_BYTES
    ):
        raise ValueError("idempotency key is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mutation_from_record(record: ApiMutation, *, is_replay: bool) -> ApiMutationRecord:
    payload = record.response_payload
    if payload is not None and not isinstance(payload, Mapping):
        raise MutationRepositoryError("stored mutation response is malformed")
    return ApiMutationRecord(
        id=record.id,
        actor_id=record.actor_id,
        action=record.action,
        scope=record.scope,
        key_hash=record.key_hash,
        request_digest=record.request_digest,
        lifecycle_state=record.lifecycle_state,
        response_status=record.response_status,
        response_payload=None if payload is None else dict(payload),
        resource_kind=record.resource_kind,
        resource_id=record.resource_id,
        is_replay=is_replay,
    )


def _validate_digest(value: str, name: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _validate_text(value: str, maximum: int, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")


__all__ = [
    "MAX_IDEMPOTENCY_KEY_BYTES",
    "MutationConflict",
    "MutationIncomplete",
    "MutationNotFound",
    "MutationRepositoryError",
    "PostgresMutationRepository",
    "hash_idempotency_key",
]
