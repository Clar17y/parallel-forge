"""Application-boundary approval challenge tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.application.services.approvals import (
    ApprovalChallengeService,
    AuthorizationError,
)
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticationError,
    CsrfError,
)


@dataclass
class FakeClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class FakeApprovalStore:
    def __init__(self) -> None:
        self.sessions: dict[UUID, UUID] = {}
        self.revoked_sessions: set[UUID] = set()
        self.challenges: dict[str, dict[str, object]] = {}
        self.runs: dict[UUID, dict[str, object]] = {}
        self.approvals: list[dict[str, object]] = []
        self.events: list[object] = []
        self.commands: list[dict[str, object]] = []

    async def get_session_by_id(
        self, *, session_id: UUID, now: datetime, for_update: bool, actor_id: UUID | None = None
    ) -> object | None:
        del now, for_update
        expected_actor_id = self.sessions.get(session_id)
        if (
            expected_actor_id is None
            or session_id in self.revoked_sessions
            or actor_id != expected_actor_id
        ):
            return None
        return {"id": session_id, "actor_id": expected_actor_id}

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

    async def create_approval(self, **values: object) -> SimpleNamespace:
        self.approvals.append(dict(values))
        return SimpleNamespace(**values)


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
        self.store.commands.append(dict(values))
        return SimpleNamespace(**values)


class FakeUow:
    def __init__(self, store: FakeApprovalStore) -> None:
        self.auth = store
        self.events = FakeEvents(store)
        self.commands = FakeCommands(store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class FakeRouteAuthService:
    def __init__(self, actor: object) -> None:
        self.session_token = "route-session-token"
        self.csrf_token = "route-csrf-token"
        self.actor = actor
        self.error: Exception | None = None

    async def require_session(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> object:
        if self.error is not None:
            raise self.error
        if token != self.session_token:
            raise AuthenticationError("invalid or expired session")
        if require_csrf and csrf_token != self.csrf_token:
            raise CsrfError("invalid csrf token")
        return self.actor


def _route_app() -> tuple[object, FakeApprovalStore, FakeRouteAuthService, FakeClock, UUID]:
    from forge.api.app import create_app
    from forge.settings import Settings

    store = FakeApprovalStore()
    session_id = uuid4()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=session_id)
    store.sessions[session_id] = actor.actor_id
    run_id = uuid4()
    store.runs[run_id] = {
        "id": run_id,
        "version": 17,
        "policy_version": 3,
        "pending_gate": "plan",
        "pending_evidence_digest": "a" * 64,
    }
    auth = FakeRouteAuthService(actor)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(
        settings,
        unit_of_work_factory=lambda: FakeUow(store),
        clock=clock,
        auth_service=auth,
    )
    return app, store, auth, clock, run_id


def _route_headers(
    *,
    origin: str = "http://127.0.0.1:3000",
    host: str = "127.0.0.1:3000",
    csrf: str | None = "route-csrf-token",
) -> dict[str, str]:
    headers = {"Origin": origin, "Host": host}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


@pytest.mark.asyncio
async def test_challenge_cannot_approve_different_evidence() -> None:
    store = FakeApprovalStore()
    session_id = uuid4()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=session_id)
    store.sessions[session_id] = actor.actor_id
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
        actor=actor,
        run_id=run_id,
        gate="merge",
        run_version=17,
        evidence_digest="a" * 64,
    )

    with pytest.raises(AuthorizationError, match="challenge does not match approval evidence"):
        await service.consume(
            challenge.token,
            actor=actor,
            run_id=challenge.run_id,
            gate="merge",
            run_version=17,
            evidence_digest="b" * 64,
        )


@pytest.mark.asyncio
async def test_challenge_expires_at_five_minutes_and_is_single_use() -> None:
    store = FakeApprovalStore()
    session_id = uuid4()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=session_id)
    store.sessions[session_id] = actor.actor_id
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
        actor=actor,
        run_id=run_id,
        gate="plan",
        run_version=0,
        evidence_digest="a" * 64,
    )
    assert challenge.expires_at == clock.value + timedelta(minutes=5)
    consumed = await service.consume(
        challenge.token,
        actor=actor,
        run_id=challenge.run_id,
        gate="plan",
        run_version=0,
        evidence_digest="a" * 64,
    )
    assert consumed.id == challenge.id

    with pytest.raises(AuthorizationError, match="challenge is expired or already consumed"):
        await service.consume(
            challenge.token,
            actor=actor,
            run_id=challenge.run_id,
            gate="plan",
            run_version=0,
            evidence_digest="a" * 64,
        )


@pytest.mark.asyncio
async def test_challenge_issue_requires_exact_server_actor_binding() -> None:
    store = FakeApprovalStore()
    session_id = uuid4()
    expected_actor_id = uuid4()
    store.sessions[session_id] = uuid4()
    run_id = uuid4()
    store.runs[run_id] = {
        "version": 0,
        "policy_version": 1,
        "pending_gate": "plan",
        "pending_evidence_digest": "a" * 64,
    }
    service = ApprovalChallengeService(
        lambda: FakeUow(store),
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )

    with pytest.raises(TypeError):
        await service.issue(
            session_id=session_id,
            run_id=run_id,
            gate="plan",
            run_version=0,
            evidence_digest="a" * 64,
        )

    with pytest.raises(AuthorizationError, match="invalid or expired session"):
        await service.issue(
            actor=AuthenticatedActor(
                actor_id=expected_actor_id,
                actor_class="operator",
                session_id=session_id,
            ),
            session_id=session_id,
            run_id=run_id,
            gate="plan",
            run_version=0,
            evidence_digest="a" * 64,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate,command_type",
    [
        ("plan", "approve_plan"),
        ("pr", "approve_pr"),
        ("merge", "approve_merge"),
    ],
)
async def test_approval_routes_authorize_each_gate_without_transition(
    gate: str, command_type: str
) -> None:
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, clock, run_id = _route_app()
    store.runs[run_id]["pending_gate"] = gate
    settings = Settings(web_origin="http://127.0.0.1:3000")
    digest = "a" * 64
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        challenge_response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": gate, "run_version": 17, "evidence_digest": digest},
        )
        challenge = challenge_response.json()["token"]
        approval_response = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": gate,
                "run_version": 17,
                "evidence_digest": digest,
                "challenge_token": challenge,
            },
        )
        replay_response = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": gate,
                "run_version": 17,
                "evidence_digest": digest,
                "challenge_token": challenge,
            },
        )

    assert challenge_response.status_code == 200
    assert approval_response.status_code == 202
    assert replay_response.status_code == 409
    assert len(store.approvals) == 1
    assert len(store.events) == 1
    assert len(store.commands) == 1
    assert store.commands[0]["command_type"] == command_type
    approval_id = str(approval_response.json()["approval_id"])
    assert store.commands[0]["payload"] == {"approval_id": approval_id}
    assert store.runs[run_id]["version"] == 17
    assert store.runs[run_id]["pending_gate"] == gate
    assert clock.value == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,headers",
    [
        ("wrong origin", _route_headers(origin="http://localhost:3000")),
        ("wrong host", _route_headers(host="127.0.0.1:3001")),
        ("missing csrf", _route_headers(csrf=None)),
        ("wrong csrf", _route_headers(csrf="wrong-csrf-token")),
    ],
)
async def test_challenge_route_rejects_mutation_security_failures_without_side_effects(
    name: str, headers: dict[str, str]
) -> None:
    del name
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, _clock, run_id = _route_app()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=headers,
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )

    assert response.status_code == 403
    assert store.challenges == {}
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_class", ["worker", "agent"])
async def test_challenge_route_rejects_non_operator_actor_without_side_effects(
    actor_class: str,
) -> None:
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, _clock, run_id = _route_app()
    auth.actor = SimpleNamespace(
        actor_id=next(iter(store.sessions.values())),
        actor_class=actor_class,
        session_id=next(iter(store.sessions)),
    )
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )

    assert response.status_code == 403
    assert store.challenges == {}
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_approval_route_rejects_missing_or_expired_session_without_side_effects() -> None:
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, _clock, run_id = _route_app()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        missing = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": "plan",
                "run_version": 17,
                "evidence_digest": "a" * 64,
                "challenge_token": "not-issued",
            },
        )
        client.cookies.set("forge_session", auth.session_token)
        challenge_response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )
        challenge = challenge_response.json()["token"]
        auth.error = AuthenticationError("invalid or expired session")
        expired_auth = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": "plan",
                "run_version": 17,
                "evidence_digest": "a" * 64,
                "challenge_token": challenge,
            },
        )

    assert missing.status_code == 401
    assert expired_auth.status_code == 401
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,headers",
    [
        ("wrong origin", _route_headers(origin="http://localhost:3000")),
        ("wrong host", _route_headers(host="127.0.0.1:3001")),
        ("missing csrf", _route_headers(csrf=None)),
        ("wrong csrf", _route_headers(csrf="wrong-csrf-token")),
    ],
)
async def test_approval_route_rejects_mutation_security_failures_without_side_effects(
    name: str, headers: dict[str, str]
) -> None:
    del name
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, _clock, run_id = _route_app()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        challenge_response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )
        response = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=headers,
            json={
                "gate": "plan",
                "run_version": 17,
                "evidence_digest": "a" * 64,
                "challenge_token": challenge_response.json()["token"],
            },
        )

    assert response.status_code == 403
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["expired", "reused", "version", "evidence"])
async def test_approval_route_binding_failures_have_no_side_effects(binding: str) -> None:
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, clock, run_id = _route_app()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        challenge_response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )
        challenge = challenge_response.json()["token"]
        stored = next(iter(store.challenges.values()))
        request_version = 17
        request_digest = "a" * 64
        if binding == "expired":
            stored["expires_at"] = clock.value
        elif binding == "reused":
            stored["consumed_at"] = clock.value
        elif binding == "version":
            request_version = 18
        else:
            request_digest = "b" * 64
        response = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": "plan",
                "run_version": request_version,
                "evidence_digest": request_digest,
                "challenge_token": challenge,
            },
        )

    assert response.status_code == 409
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_approval_route_rechecks_session_after_dependency_race() -> None:
    from forge.settings import Settings
    from httpx import ASGITransport, AsyncClient

    app, store, auth, _clock, run_id = _route_app()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session_token)
        challenge_response = await client.post(
            f"/api/runs/{run_id}/approval-challenges",
            headers=_route_headers(),
            json={"gate": "plan", "run_version": 17, "evidence_digest": "a" * 64},
        )
        store.revoked_sessions.add(next(iter(store.sessions)))
        response = await client.post(
            f"/api/runs/{run_id}/approvals",
            headers=_route_headers(),
            json={
                "gate": "plan",
                "run_version": 17,
                "evidence_digest": "a" * 64,
                "challenge_token": challenge_response.json()["token"],
            },
        )

    assert response.status_code == 401
    assert store.approvals == []
    assert store.events == []
    assert store.commands == []


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
