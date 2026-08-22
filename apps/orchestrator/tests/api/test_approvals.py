"""Application-boundary approval challenge tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.application.services.approvals import (
    ApprovalChallengeService,
    AuthorizationError,
)


@dataclass
class FakeClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class FakeApprovalStore:
    def __init__(self) -> None:
        self.sessions: set[UUID] = set()
        self.challenges: dict[str, dict[str, object]] = {}
        self.runs: dict[UUID, dict[str, object]] = {}

    async def get_session_by_id(
        self, *, session_id: UUID, now: datetime, for_update: bool, actor_id: UUID | None = None
    ) -> object | None:
        del now, for_update, actor_id
        return object() if session_id in self.sessions else None

    async def create_challenge(self, **values: object) -> dict[str, object]:
        row = dict(values)
        row.setdefault("id", row["challenge_id"])
        self.challenges[str(row["token_hash"])] = row
        return row

    async def lock_run(self, run_id: UUID) -> dict[str, object] | None:
        return self.runs.get(run_id)

    async def get_challenge(self, *, token_hash: str, for_update: bool) -> dict[str, object] | None:
        del for_update
        return self.challenges.get(token_hash)

    async def consume_challenge(self, *, challenge_id: UUID, at: datetime) -> None:
        for row in self.challenges.values():
            if row["id"] == challenge_id:
                row["consumed_at"] = at


class FakeUow:
    def __init__(self, store: FakeApprovalStore) -> None:
        self.auth = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.mark.asyncio
async def test_challenge_cannot_approve_different_evidence() -> None:
    store = FakeApprovalStore()
    session_id = uuid4()
    store.sessions.add(session_id)
    run_id = uuid4()
    store.runs[run_id] = {
        "version": 17,
        "policy_version": 1,
        "pending_gate": "merge",
        "pending_evidence_digest": "a" * 64,
    }
    service = ApprovalChallengeService(
        lambda: FakeUow(store),
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    challenge = await service.issue(
        session_id=session_id,
        run_id=run_id,
        gate="merge",
        run_version=17,
        evidence_digest="a" * 64,
    )

    with pytest.raises(AuthorizationError, match="challenge does not match approval evidence"):
        await service.consume(
            challenge.token,
            session_id=session_id,
            run_id=challenge.run_id,
            gate="merge",
            run_version=17,
            evidence_digest="b" * 64,
        )


@pytest.mark.asyncio
async def test_challenge_expires_at_five_minutes_and_is_single_use() -> None:
    store = FakeApprovalStore()
    session_id = uuid4()
    store.sessions.add(session_id)
    run_id = uuid4()
    store.runs[run_id] = {
        "version": 0,
        "policy_version": 1,
        "pending_gate": "plan",
        "pending_evidence_digest": "a" * 64,
    }
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = ApprovalChallengeService(lambda: FakeUow(store), clock=clock)

    challenge = await service.issue(
        session_id=session_id,
        run_id=run_id,
        gate="plan",
        run_version=0,
        evidence_digest="a" * 64,
    )
    assert challenge.expires_at == clock.value + timedelta(minutes=5)
    consumed = await service.consume(
        challenge.token,
        session_id=session_id,
        run_id=challenge.run_id,
        gate="plan",
        run_version=0,
        evidence_digest="a" * 64,
    )
    assert consumed.id == challenge.id

    with pytest.raises(AuthorizationError, match="challenge is expired or already consumed"):
        await service.consume(
            challenge.token,
            session_id=session_id,
            run_id=challenge.run_id,
            gate="plan",
            run_version=0,
            evidence_digest="a" * 64,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_approval_routes_issue_and_authorize_once(
    session_factory, persisted_run
) -> None:
    from forge.api.app import create_app
    from forge.persistence.models import Run
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import update

    digest = "c" * 64
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
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(settings, session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        bootstrap = await app.state.auth_service.issue_bootstrap()
        exchanged = await client.post(
            "/api/auth/bootstrap",
            headers={"Origin": settings.web_origin, "Host": "127.0.0.1:3000"},
            json={"token": bootstrap},
        )
        csrf = exchanged.json()["csrf_token"]
        headers = {
            "Origin": settings.web_origin,
            "Host": "127.0.0.1:3000",
            "X-CSRF-Token": csrf,
        }
        challenge_response = await client.post(
            f"/api/runs/{persisted_run.id}/approval-challenges",
            headers=headers,
            json={
                "gate": "plan",
                "run_version": 0,
                "evidence_digest": digest,
            },
        )
        challenge = challenge_response.json()["token"]
        approval_response = await client.post(
            f"/api/runs/{persisted_run.id}/approvals",
            headers=headers,
            json={
                "gate": "plan",
                "run_version": 0,
                "evidence_digest": digest,
                "challenge_token": challenge,
            },
        )

    assert challenge_response.status_code == 200
    assert approval_response.status_code == 202
    assert set(approval_response.json()) == {"approval_id"}
