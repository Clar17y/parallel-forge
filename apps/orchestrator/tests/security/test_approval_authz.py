"""Transaction and authorization coverage for approval commands."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.application.services.approvals import (
    ApprovalAuthorizationService,
    ApprovalCommandValidationError,
    AuthorizationError,
    validate_approval_command,
)
from forge.application.services.auth import AuthenticatedActor, hash_token


class FakeApprovalStore:
    def __init__(self, *, gate: str) -> None:
        self.actor_id = uuid4()
        self.session_id = uuid4()
        self.run_id = uuid4()
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.run = {
            "id": self.run_id,
            "version": 17,
            "policy_version": 3,
            "pending_gate": gate,
            "pending_evidence_digest": "a" * 64,
        }
        self.challenge_id = uuid4()
        self.challenge_token = "challenge-raw"
        self.challenge = {
            "id": self.challenge_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "gate": gate,
            "run_version": 17,
            "policy_version": 3,
            "evidence_digest": "a" * 64,
            "token_hash": hash_token(self.challenge_token),
            "expires_at": self.now + timedelta(minutes=5),
            "consumed_at": None,
        }
        self.approvals: list[dict[str, object]] = []
        self.events: list[object] = []
        self.commands: list[dict[str, object]] = []
        self.fail_command = False
        self.revoked_sessions: set[UUID] = set()

    async def lock_run(self, run_id: UUID) -> dict[str, object] | None:
        return self.run if run_id == self.run_id else None

    async def get_session_by_id(
        self,
        *,
        session_id: UUID,
        now: datetime,
        for_update: bool,
        actor_id: UUID | None = None,
    ) -> object | None:
        del now, for_update
        if (
            session_id != self.session_id
            or session_id in self.revoked_sessions
            or (actor_id is not None and actor_id != self.actor_id)
        ):
            return None
        return {"id": session_id, "actor_id": self.actor_id}

    async def get_challenge(self, *, token_hash: str, for_update: bool) -> object | None:
        del for_update
        return self.challenge if token_hash == self.challenge["token_hash"] else None

    async def create_approval(self, **values: object) -> SimpleNamespace:
        self.approvals.append(dict(values))
        return SimpleNamespace(**values)

    async def consume_challenge(self, *, challenge_id: UUID, at: datetime) -> None:
        if challenge_id == self.challenge_id:
            self.challenge["consumed_at"] = at


class FakeEvents:
    def __init__(self, store: FakeApprovalStore) -> None:
        self.store = store

    async def append(self, event: object) -> object:
        self.store.events.append(event)
        return event


class FakeCommands:
    def __init__(self, store: FakeApprovalStore) -> None:
        self.store = store

    async def enqueue(self, **values: object) -> SimpleNamespace:
        if self.store.fail_command:
            raise RuntimeError("injected command failure")
        self.store.commands.append(dict(values))
        return SimpleNamespace(**values)


class FakeUow:
    def __init__(self, store: FakeApprovalStore) -> None:
        self.store = store
        self.auth = store
        self.events = FakeEvents(store)
        self.commands = FakeCommands(store)
        self.snapshot: tuple[object, ...] | None = None

    async def __aenter__(self) -> Self:
        self.snapshot = (
            deepcopy(self.store.challenge),
            list(self.store.approvals),
            list(self.store.events),
            list(self.store.commands),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if exc_type is not None and self.snapshot is not None:
            challenge, approvals, events, commands = self.snapshot
            self.store.challenge = challenge  # type: ignore[assignment]
            self.store.approvals[:] = approvals  # type: ignore[index]
            self.store.events[:] = events  # type: ignore[index]
            self.store.commands[:] = commands  # type: ignore[index]

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate,command_type",
    [("plan", "approve_plan"), ("pr", "approve_pr"), ("merge", "approve_merge")],
)
async def test_authorize_persists_one_approval_event_and_typed_command(
    gate: str, command_type: str
) -> None:
    store = FakeApprovalStore(gate=gate)
    actor = AuthenticatedActor(
        actor_id=store.actor_id,
        actor_class="operator",
        session_id=store.session_id,
    )
    service = ApprovalAuthorizationService(
        lambda: FakeUow(store),
        clock=SimpleNamespace(now=lambda: store.now),
    )

    approval = await service.authorize(
        actor=actor,
        run_id=store.run_id,
        gate=gate,
        run_version=17,
        evidence_digest="a" * 64,
        challenge_token=store.challenge_token,
    )

    assert approval.id is not None
    assert len(store.approvals) == 1
    assert len(store.events) == 1
    assert len(store.commands) == 1
    assert store.commands[0]["command_type"] == command_type
    assert store.commands[0]["payload"] == {"approval_id": str(approval.id)}
    assert store.run["version"] == 17
    assert store.run["pending_gate"] == gate

    with pytest.raises(AuthorizationError, match="challenge is expired or already consumed"):
        await service.authorize(
            actor=actor,
            run_id=store.run_id,
            gate=gate,
            run_version=17,
            evidence_digest="a" * 64,
            challenge_token=store.challenge_token,
        )


@pytest.mark.asyncio
async def test_command_failure_rolls_back_approval_event_and_challenge() -> None:
    store = FakeApprovalStore(gate="merge")
    store.fail_command = True
    actor = AuthenticatedActor(
        actor_id=store.actor_id,
        actor_class="operator",
        session_id=store.session_id,
    )
    service = ApprovalAuthorizationService(
        lambda: FakeUow(store),
        clock=SimpleNamespace(now=lambda: store.now),
    )

    with pytest.raises(RuntimeError, match="injected command failure"):
        await service.authorize(
            actor=actor,
            run_id=store.run_id,
            gate="merge",
            run_version=17,
            evidence_digest="a" * 64,
            challenge_token=store.challenge_token,
        )

    assert store.challenge["consumed_at"] is None
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_non_operator_actor_is_rejected() -> None:
    store = FakeApprovalStore(gate="plan")
    actor = SimpleNamespace(
        actor_id=store.actor_id,
        actor_class="worker",
        session_id=store.session_id,
    )
    service = ApprovalAuthorizationService(
        lambda: FakeUow(store),
        clock=SimpleNamespace(now=lambda: store.now),
    )

    with pytest.raises(AuthorizationError, match="operator authorization required"):
        await service.authorize(
            actor=actor,  # type: ignore[arg-type]
            run_id=store.run_id,
            gate="plan",
            run_version=17,
            evidence_digest="a" * 64,
            challenge_token=store.challenge_token,
        )


@pytest.mark.asyncio
async def test_session_revocation_race_is_rechecked_before_side_effects() -> None:
    store = FakeApprovalStore(gate="plan")
    actor = AuthenticatedActor(
        actor_id=store.actor_id,
        actor_class="operator",
        session_id=store.session_id,
    )
    service = ApprovalAuthorizationService(
        lambda: FakeUow(store),
        clock=SimpleNamespace(now=lambda: store.now),
    )
    # Model logout/rotation committing after the API dependency but before
    # the authorization transaction obtains its session lock.
    store.revoked_sessions.add(store.session_id)

    with pytest.raises(AuthorizationError, match="invalid or expired session"):
        await service.authorize(
            actor=actor,
            run_id=store.run_id,
            gate="plan",
            run_version=17,
            evidence_digest="a" * 64,
            challenge_token=store.challenge_token,
        )

    assert store.challenge["consumed_at"] is None
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_authorization_is_atomic_and_enqueues_only_approval_id(
    session_factory, persisted_run
) -> None:
    from forge.application.services.approvals import ApprovalChallengeService
    from forge.application.services.auth import AuthService
    from forge.persistence.models import Approval, Run, RunCommand, RunEvent
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select, update

    digest = "a" * 64
    async with session_factory() as db, db.begin():
        await db.execute(
            update(Run)
            .where(Run.id == persisted_run.id)
            .values(
                state="AWAITING_PLAN_APPROVAL",
                pending_gate="plan",
                pending_evidence_digest=digest,
            )
        )
    clock = SimpleNamespace(now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    uow_factory = lambda: PostgresUnitOfWork(session_factory)
    auth = AuthService(uow_factory, clock=clock)
    challenge_service = ApprovalChallengeService(uow_factory, clock=clock)
    authorization = ApprovalAuthorizationService(uow_factory, clock=clock)
    bootstrap = await auth.issue_bootstrap()
    session = await auth.exchange_bootstrap(bootstrap)
    challenge = await challenge_service.issue(
        actor=session.actor,
        run_id=persisted_run.id,
        gate="plan",
        run_version=0,
        evidence_digest=digest,
    )
    approval = await authorization.authorize(
        actor=session.actor,
        run_id=persisted_run.id,
        gate="plan",
        run_version=0,
        evidence_digest=digest,
        challenge_token=challenge.token,
    )

    async with session_factory() as db:
        approval_row = await db.scalar(select(Approval).where(Approval.id == approval.id))
        event_row = await db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == persisted_run.id,
                RunEvent.event_type == "approval-authorized",
            )
        )
        command_row = await db.scalar(
            select(RunCommand).where(RunCommand.run_id == persisted_run.id)
        )
        run_row = await db.scalar(select(Run).where(Run.id == persisted_run.id))
    assert approval_row is not None
    assert event_row is not None
    assert event_row.payload == {
        "approval_id": str(approval.id),
        "gate": "plan",
        "evidence_digest": digest,
    }
    assert command_row is not None
    assert command_row.command_type == "approve_plan"
    assert command_row.payload == {"approval_id": str(approval.id)}
    assert run_row is not None
    assert run_row.version == 0
    assert run_row.state == "AWAITING_PLAN_APPROVAL"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_command_failure_rolls_back_all_authorization_rows(
    session_factory, persisted_run, monkeypatch
) -> None:
    from forge.application.services.approvals import ApprovalChallengeService
    from forge.application.services.auth import AuthService
    from forge.persistence.models import Approval, ApprovalChallenge, Run, RunCommand, RunEvent
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select, update

    digest = "b" * 64
    async with session_factory() as db, db.begin():
        await db.execute(
            update(Run)
            .where(Run.id == persisted_run.id)
            .values(
                state="AWAITING_MERGE_APPROVAL",
                pending_gate="merge",
                pending_evidence_digest=digest,
            )
        )
    clock = SimpleNamespace(now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    uow_factory = lambda: PostgresUnitOfWork(session_factory)
    auth = AuthService(uow_factory, clock=clock)
    challenge_service = ApprovalChallengeService(uow_factory, clock=clock)
    authorization = ApprovalAuthorizationService(uow_factory, clock=clock)
    session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    challenge = await challenge_service.issue(
        actor=session.actor,
        run_id=persisted_run.id,
        gate="merge",
        run_version=0,
        evidence_digest=digest,
    )

    async def fail_enqueue(self, **values: object) -> object:
        del self, values
        raise RuntimeError("injected command failure")

    monkeypatch.setattr(PostgresCommandRepository, "enqueue", fail_enqueue)
    with pytest.raises(RuntimeError, match="injected command failure"):
        await authorization.authorize(
            actor=session.actor,
            run_id=persisted_run.id,
            gate="merge",
            run_version=0,
            evidence_digest=digest,
            challenge_token=challenge.token,
        )

    async with session_factory() as db:
        assert await db.scalar(select(Approval).where(Approval.run_id == persisted_run.id)) is None
        assert (
            await db.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == persisted_run.id,
                    RunEvent.event_type == "approval-authorized",
                )
            )
            is None
        )
        assert (
            await db.scalar(select(RunCommand).where(RunCommand.run_id == persisted_run.id)) is None
        )
        challenge_row = await db.scalar(
            select(ApprovalChallenge).where(ApprovalChallenge.id == challenge.id)
        )
    assert challenge_row is not None
    assert challenge_row.consumed_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_validator_invalidates_stale_run_version(
    session_factory, persisted_run
) -> None:
    from forge.application.services.approvals import ApprovalChallengeService
    from forge.application.services.auth import AuthService
    from forge.persistence.models import Approval, Run, RunCommand
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select, update

    digest = "d" * 64
    async with session_factory() as db, db.begin():
        await db.execute(
            update(Run)
            .where(Run.id == persisted_run.id)
            .values(
                state="AWAITING_PR_APPROVAL",
                pending_gate="pr",
                pending_evidence_digest=digest,
            )
        )
    clock = SimpleNamespace(now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    uow_factory = lambda: PostgresUnitOfWork(session_factory)
    auth = AuthService(uow_factory, clock=clock)
    challenge_service = ApprovalChallengeService(uow_factory, clock=clock)
    authorization = ApprovalAuthorizationService(uow_factory, clock=clock)
    session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    challenge = await challenge_service.issue(
        actor=session.actor,
        run_id=persisted_run.id,
        gate="pr",
        run_version=0,
        evidence_digest=digest,
    )
    approval = await authorization.authorize(
        actor=session.actor,
        run_id=persisted_run.id,
        gate="pr",
        run_version=0,
        evidence_digest=digest,
        challenge_token=challenge.token,
    )
    async with session_factory() as db:
        command_row = await db.scalar(
            select(RunCommand).where(RunCommand.run_id == persisted_run.id)
        )
    assert command_row is not None
    command = await PostgresCommandRepository(session_factory).get(command_row.id)

    async with session_factory() as db, db.begin():
        await db.execute(update(Run).where(Run.id == persisted_run.id).values(version=1))
        with pytest.raises(ApprovalCommandValidationError, match="approval evidence is stale"):
            await validate_approval_command(command, session=db, clock=clock)
        stale = await db.scalar(select(Approval).where(Approval.id == approval.id))

    assert stale is not None
    assert stale.invalidated_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert stale.invalidation_reason == "stale approval evidence"
