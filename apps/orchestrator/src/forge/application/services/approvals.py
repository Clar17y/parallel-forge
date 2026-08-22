"""Evidence-bound approval challenges and the API-to-worker authorization bridge."""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.clock import Clock, SystemClock
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthRepository,
    AuthUnitOfWork,
    hash_token,
)
from forge.domain.approval import ApprovalGate, ApprovalRecord
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.persistence.models import Approval

CHALLENGE_LIFETIME = timedelta(minutes=5)
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMAND_FOR_GATE = {
    "plan": "approve_plan",
    "pr": "approve_pr",
    "merge": "approve_merge",
}


class AuthorizationError(RuntimeError):
    """Approval authorization failed without revealing which binding differed."""


class ApprovalCommandValidationError(AuthorizationError):
    """A worker-side approval command is stale or malformed."""


@dataclass(frozen=True, slots=True)
class ApprovalChallengeToken:
    """A one-time challenge response; the raw token is never part of repr output."""

    id: UUID
    session_id: UUID
    run_id: UUID
    gate: str
    run_version: int
    policy_version: int
    evidence_digest: str
    expires_at: datetime
    token: str = field(repr=False)


class EventWriter(Protocol):
    async def append(self, event: RunEvent) -> RunEvent: ...


class CommandWriter(Protocol):
    async def enqueue(
        self,
        *,
        run_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        expected_run_version: int,
        actor_id: UUID,
        payload_schema_version: int = 1,
        available_at: datetime | None = None,
    ) -> CommandEnvelope: ...


class ApprovalUnitOfWork(AuthUnitOfWork, Protocol):
    auth: AuthRepository
    events: EventWriter
    commands: CommandWriter


class ApprovalChallengeService:
    """Issue and consume challenges after transaction-local binding checks."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], ApprovalUnitOfWork],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()

    async def issue(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        gate: str,
        run_version: int,
        evidence_digest: str,
        session_id: UUID | None = None,
        policy_version: int | None = None,
    ) -> ApprovalChallengeToken:
        bound_session_id = _require_actor_binding(actor, session_id)
        _require_gate(gate)
        _require_digest(evidence_digest)
        now = _aware_now(self._clock)
        token = _new_token()
        challenge_id = uuid4()
        async with self._unit_of_work_factory() as work:
            session = await work.auth.get_session_by_id(
                session_id=bound_session_id,
                now=now,
                for_update=True,
                actor_id=actor.actor_id,
            )
            if session is None:
                raise AuthorizationError("invalid or expired session")
            run = await work.auth.lock_run(run_id)
            if run is None or not _run_matches(
                run,
                gate=gate,
                run_version=run_version,
                evidence_digest=evidence_digest,
            ):
                raise AuthorizationError("run does not match approval evidence")
            resolved_policy_version = _int_value(run, "policy_version")
            if resolved_policy_version is None:
                resolved_policy_version = policy_version
            if resolved_policy_version is None or resolved_policy_version < 1:
                raise AuthorizationError("run does not match approval evidence")
            if policy_version is not None and policy_version != resolved_policy_version:
                raise AuthorizationError("run does not match approval evidence")
            await work.auth.create_challenge(
                challenge_id=challenge_id,
                session_id=bound_session_id,
                run_id=run_id,
                gate=gate,
                run_version=run_version,
                policy_version=resolved_policy_version,
                evidence_digest=evidence_digest,
                token_hash=hash_token(token),
                expires_at=now + CHALLENGE_LIFETIME,
            )
            await work.commit()
        return ApprovalChallengeToken(
            id=challenge_id,
            session_id=bound_session_id,
            run_id=run_id,
            gate=gate,
            run_version=run_version,
            policy_version=resolved_policy_version,
            evidence_digest=evidence_digest,
            expires_at=now + CHALLENGE_LIFETIME,
            token=token,
        )

    async def consume(
        self,
        token: str,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        gate: str,
        run_version: int,
        evidence_digest: str,
        session_id: UUID | None = None,
    ) -> ApprovalChallengeToken:
        bound_session_id = _require_actor_binding(actor, session_id)
        _require_gate(gate)
        _require_digest(evidence_digest)
        if not isinstance(token, str) or not token:
            raise AuthorizationError("challenge is expired or already consumed")
        now = _aware_now(self._clock)
        async with self._unit_of_work_factory() as work:
            challenge = await work.auth.get_challenge(
                token_hash=hash_token(token),
                for_update=True,
            )
            if challenge is None or _row_value(challenge, "consumed_at") is not None:
                raise AuthorizationError("challenge is expired or already consumed")
            expires_at = _datetime_value(challenge, "expires_at")
            if expires_at <= now:
                raise AuthorizationError("challenge is expired or already consumed")
            challenge_session = _uuid_value(challenge, "session_id")
            if challenge_session != bound_session_id:
                raise AuthorizationError("challenge does not match approval evidence")
            session = await work.auth.get_session_by_id(
                session_id=bound_session_id,
                now=now,
                for_update=True,
                actor_id=actor.actor_id,
            )
            if session is None:
                raise AuthorizationError("invalid or expired session")
            if not _challenge_matches(
                challenge,
                run_id=run_id,
                gate=gate,
                run_version=run_version,
                evidence_digest=evidence_digest,
            ):
                raise AuthorizationError("challenge does not match approval evidence")
            challenge_id = _uuid_value(challenge, "id")
            if challenge_id is None:
                raise AuthorizationError("challenge is expired or already consumed")
            await work.auth.consume_challenge(challenge_id=challenge_id, at=now)
            await work.commit()
        return _challenge_from_row(challenge, token=token)


class ApprovalAuthorizationService:
    """Persist one approval, event, and typed worker command in one transaction."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], ApprovalUnitOfWork],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()

    async def authorize(
        self,
        *,
        actor: AuthenticatedActor,
        run_id: UUID,
        gate: str,
        run_version: int | None = None,
        evidence_digest: str,
        challenge_token: str,
        expected_run_version: int | None = None,
    ) -> ApprovalRecord:
        if not isinstance(actor, AuthenticatedActor) or actor.actor_class != "operator":
            raise AuthorizationError("operator authorization required")
        _require_gate(gate)
        _require_digest(evidence_digest)
        requested_version = (
            expected_run_version if expected_run_version is not None else run_version
        )
        if requested_version is None or requested_version < 0:
            raise AuthorizationError("approval evidence is stale")
        if not isinstance(challenge_token, str) or not challenge_token:
            raise AuthorizationError("challenge is expired or already consumed")
        now = _aware_now(self._clock)

        async with self._unit_of_work_factory() as work:
            # Lock order is deliberate: authoritative run, then session, then challenge.
            run = await work.auth.lock_run(run_id)
            if run is None:
                raise AuthorizationError("approval evidence is stale")
            session = await work.auth.get_session_by_id(
                session_id=actor.session_id,
                now=now,
                for_update=True,
                actor_id=actor.actor_id,
            )
            if session is None:
                raise AuthorizationError("invalid or expired session")
            challenge = await work.auth.get_challenge(
                token_hash=hash_token(challenge_token),
                for_update=True,
            )
            if challenge is None or _row_value(challenge, "consumed_at") is not None:
                raise AuthorizationError("challenge is expired or already consumed")
            if _datetime_value(challenge, "expires_at") <= now:
                raise AuthorizationError("challenge is expired or already consumed")
            if not _challenge_matches(
                challenge,
                run_id=run_id,
                gate=gate,
                run_version=requested_version,
                evidence_digest=evidence_digest,
            ):
                raise AuthorizationError("challenge does not match approval evidence")
            if _uuid_value(challenge, "session_id") != actor.session_id:
                raise AuthorizationError("challenge does not match approval evidence")
            if not _run_matches(
                run,
                gate=gate,
                run_version=requested_version,
                evidence_digest=evidence_digest,
            ):
                raise AuthorizationError("approval evidence is stale")
            policy_version = _int_value(run, "policy_version")
            challenge_policy = _int_value(challenge, "policy_version")
            if policy_version is None or challenge_policy != policy_version:
                raise AuthorizationError("approval evidence is stale")
            challenge_id = _uuid_value(challenge, "id")
            if challenge_id is None:
                raise AuthorizationError("challenge is expired or already consumed")

            approval_id = uuid4()
            await work.auth.create_approval(
                id=approval_id,
                run_id=run_id,
                gate=gate,
                evidence_digest=evidence_digest,
                run_version=requested_version,
                policy_version=policy_version,
                authenticated_actor_id=actor.actor_id,
                invalidated_at=None,
                invalidation_reason=None,
                created_at=now,
            )
            await work.auth.consume_challenge(challenge_id=challenge_id, at=now)
            await work.events.append(
                RunEvent(
                    run_id=run_id,
                    run_version=requested_version,
                    event_type="approval-authorized",
                    payload={
                        "approval_id": str(approval_id),
                        "gate": gate,
                        "evidence_digest": evidence_digest,
                    },
                    actor_class="operator",
                    actor_id=actor.actor_id,
                    occurred_at=now,
                )
            )
            await work.commands.enqueue(
                run_id=run_id,
                command_type=_COMMAND_FOR_GATE[gate],
                idempotency_key=f"approval:{approval_id}",
                payload={"approval_id": str(approval_id)},
                expected_run_version=requested_version,
                actor_id=actor.actor_id,
                payload_schema_version=1,
                available_at=now,
            )
            await work.commit()

        return ApprovalRecord(
            id=approval_id,
            gate=ApprovalGate(gate),
            evidence_digest=evidence_digest,
            run_id=run_id,
            run_version=requested_version,
            policy_version=policy_version,
            authenticated_actor_id=str(actor.actor_id),
            created_at=now,
        )

    async def validate_worker_command(
        self,
        command: CommandEnvelope,
        *,
        session: AsyncSession,
    ) -> ApprovalRecord:
        """Validate an approval command before a later task-specific handler runs."""

        expected_gate = {
            "approve_plan": "plan",
            "approve_pr": "pr",
            "approve_merge": "merge",
        }.get(command.command_type)
        if expected_gate is None or set(command.payload) != {"approval_id"}:
            raise ApprovalCommandValidationError("approval command is invalid")
        raw_approval_id = command.payload.get("approval_id")
        try:
            approval_id = UUID(str(raw_approval_id))
        except TypeError, ValueError:
            raise ApprovalCommandValidationError("approval command is invalid") from None
        approval = await _get_approval(session, approval_id)
        if approval is None:
            raise ApprovalCommandValidationError("approval evidence is stale")
        run = await _get_run(session, command.run_id)
        if run is None:
            raise ApprovalCommandValidationError("approval evidence is stale")
        now = _aware_now(self._clock)
        if not _approval_is_current(
            approval,
            run=run,
            command=command,
            gate=expected_gate,
        ):
            await _invalidate_approval(session, approval, now)
            raise ApprovalCommandValidationError("approval evidence is stale")
        return _approval_from_row(approval)


async def validate_approval_command(
    command: CommandEnvelope,
    *,
    session: AsyncSession,
    clock: Clock | None = None,
) -> ApprovalRecord:
    """Convenience worker boundary for handlers that own an active session."""

    service = object.__new__(ApprovalAuthorizationService)
    service._clock = clock or SystemClock()
    return await service.validate_worker_command(command, session=session)


def _require_gate(gate: str) -> None:
    if gate not in _COMMAND_FOR_GATE:
        raise AuthorizationError("unsupported approval gate")


def _require_actor_binding(
    actor: AuthenticatedActor,
    session_id: UUID | None,
) -> UUID:
    if not isinstance(actor, AuthenticatedActor) or actor.actor_class != "operator":
        raise AuthorizationError("operator authorization required")
    if session_id is not None and session_id != actor.session_id:
        raise AuthorizationError("invalid or expired session")
    return actor.session_id


def _require_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AuthorizationError("approval evidence is stale")


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _aware_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("security clock must return an aware datetime")
    return value.astimezone(UTC)


def _run_matches(
    run: object,
    *,
    gate: str,
    run_version: int,
    evidence_digest: str,
) -> bool:
    return (
        _string_value(run, "pending_gate") == gate
        and _int_value(run, "version") == run_version
        and _digest_matches(_string_value(run, "pending_evidence_digest"), evidence_digest)
    )


def _challenge_matches(
    challenge: object,
    *,
    run_id: UUID,
    gate: str,
    run_version: int,
    evidence_digest: str,
) -> bool:
    challenge_run = _uuid_value(challenge, "run_id")
    challenge_gate = _string_value(challenge, "gate")
    challenge_version = _int_value(challenge, "run_version")
    challenge_digest = _string_value(challenge, "evidence_digest")
    return (
        challenge_run == run_id
        and challenge_gate == gate
        and challenge_version == run_version
        and _digest_matches(challenge_digest, evidence_digest)
    )


def _digest_matches(left: str | None, right: str) -> bool:
    return left is not None and hmac.compare_digest(left, right)


def _challenge_from_row(row: object, *, token: str) -> ApprovalChallengeToken:
    challenge_id = _uuid_value(row, "id")
    session_id = _uuid_value(row, "session_id")
    run_id = _uuid_value(row, "run_id")
    gate = _string_value(row, "gate")
    run_version = _int_value(row, "run_version")
    policy_version = _int_value(row, "policy_version")
    evidence_digest = _string_value(row, "evidence_digest")
    if (
        challenge_id is None
        or session_id is None
        or run_id is None
        or gate is None
        or run_version is None
        or policy_version is None
        or evidence_digest is None
    ):
        raise AuthorizationError("challenge is expired or already consumed")
    return ApprovalChallengeToken(
        id=challenge_id,
        session_id=session_id,
        run_id=run_id,
        gate=gate,
        run_version=run_version,
        policy_version=policy_version,
        evidence_digest=evidence_digest,
        expires_at=_datetime_value(row, "expires_at"),
        token=token,
    )


def _approval_from_row(row: Approval) -> ApprovalRecord:
    return ApprovalRecord(
        id=row.id,
        gate=ApprovalGate(row.gate),
        evidence_digest=row.evidence_digest,
        run_id=row.run_id,
        run_version=row.run_version,
        policy_version=row.policy_version,
        authenticated_actor_id=str(row.authenticated_actor_id),
        created_at=row.created_at,
        invalidated_at=row.invalidated_at,
    )


def _approval_is_current(
    approval: Approval,
    *,
    run: object,
    command: CommandEnvelope,
    gate: str,
) -> bool:
    actor_ok = command.actor_id == approval.authenticated_actor_id
    return (
        approval.invalidated_at is None
        and approval.run_id == command.run_id
        and approval.gate == gate
        and actor_ok
        and approval.run_version == command.expected_run_version
        and _int_value(run, "version") == approval.run_version
        and _int_value(run, "policy_version") == approval.policy_version
        and _string_value(run, "pending_gate") == gate
        and _digest_matches(
            _string_value(run, "pending_evidence_digest"),
            approval.evidence_digest,
        )
    )


async def _get_approval(session: AsyncSession, approval_id: UUID) -> Approval | None:
    from sqlalchemy import select

    result = await session.execute(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def _get_run(session: AsyncSession, run_id: UUID) -> object | None:
    from sqlalchemy import select

    from forge.persistence.models import Run

    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    return cast(object | None, result.scalar_one_or_none())


async def _invalidate_approval(session: AsyncSession, approval: Approval, at: datetime) -> None:
    approval.invalidated_at = at
    approval.invalidation_reason = "stale approval evidence"
    await session.flush()


def _row_value(row: object, name: str) -> object:
    value = getattr(row, name, None)
    if value is not None:
        return value
    if isinstance(row, Mapping):
        return row.get(name)
    return None


def _uuid_value(row: object, name: str) -> UUID | None:
    value = _row_value(row, name)
    return value if isinstance(value, UUID) else None


def _string_value(row: object, name: str) -> str | None:
    value = _row_value(row, name)
    return value if isinstance(value, str) else None


def _int_value(row: object, name: str) -> int | None:
    value = _row_value(row, name)
    return value if type(value) is int else None


def _datetime_value(row: object, name: str) -> datetime:
    value = _row_value(row, name)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuthorizationError("approval evidence is stale")
    return value


__all__ = [
    "CHALLENGE_LIFETIME",
    "ApprovalAuthorizationService",
    "ApprovalChallengeService",
    "ApprovalChallengeToken",
    "ApprovalCommandValidationError",
    "AuthorizationError",
    "validate_approval_command",
]
