"""Deterministic event cursor, framing and streaming boundary checks."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.api.sse import encode_event, event_stream, parse_cursor
from forge.application.services.auth import AuthenticationError
from forge.domain.event import RunEvent
from forge.persistence.models import OperatorSession
from forge.persistence.queries.events import EventQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def test_last_event_id_precedes_query_cursor():
    assert parse_cursor("3", "1") == 3
    assert parse_cursor(None, "4") == 4
    assert parse_cursor(None, None) == 0


@pytest.mark.parametrize("value", ["", "-1", "+1", " 1", "1\n", "1.0", "١", "9223372036854775808"])
def test_cursor_rejects_noncanonical_or_out_of_postgres_range(value):
    with pytest.raises(ValueError):
        parse_cursor(value, "0")


def _event(sequence, **kwargs):
    return RunEvent(
        run_id=uuid4(),
        run_version=1,
        sequence=sequence,
        event_type=kwargs.pop("event_type", "run.updated"),
        payload=kwargs,
    )


def test_event_framing_cannot_inject_fields_and_redacts_payload():
    frame = encode_event(
        _event(
            4,
            event_type="changed\nevent: forged",
            api_key="secret-value",
            hidden_reasoning="private-thought",
        )
    )
    assert frame.startswith(b"id: 4\nevent: run.event\ndata: ")
    assert frame.count(b"\n") == 4
    assert b"secret-value" not in frame
    assert b"private-thought" not in frame


@pytest.mark.asyncio
async def test_revoked_session_stops_even_mid_page():
    revoked = False

    async def check():
        if revoked:
            raise AuthenticationError("expired")

    async def page(cursor):
        return [_event(4), _event(5)]

    async def connected():
        return False

    stream = event_stream(
        cursor=3,
        read_page=page,
        check_session=check,
        disconnected=connected,
        shutdown=asyncio.Event(),
    )
    assert (await anext(stream)).startswith(b"id: 4\n")
    revoked = True
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_kind", ["shutdown", "disconnect", "cancel"])
async def test_heartbeat_is_comment_only_and_stream_stops_without_more_reads(stop_kind):
    shutdown = asyncio.Event()
    disconnected = False
    calls = 0

    async def page(cursor):
        nonlocal calls
        calls += 1
        return []

    async def check():
        return None

    async def disconnect():
        return disconnected

    stream = event_stream(
        cursor=0,
        read_page=page,
        check_session=check,
        disconnected=disconnect,
        shutdown=shutdown,
        heartbeat_seconds=0,
        poll_seconds=0.001,
    )
    assert await anext(stream) == b": heartbeat\n\n"
    if stop_kind == "shutdown":
        shutdown.set()
    elif stop_kind == "disconnect":
        disconnected = True
    else:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
    if stop_kind != "cancel":
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("stop_kind", ["disconnect", "revoke", "expire"])
async def test_postgres_stream_resumes_and_sees_later_insert_once(
    persisted_run, session_factory, tmp_path, spec_version, stop_kind
):
    class Clock:
        current = datetime.now(UTC)

        def now(self):
            return self.current

    clock = Clock()
    settings = Settings(data_root=tmp_path / "data")
    app = create_app(settings, session_factory=session_factory, clock=clock)
    async with PostgresUnitOfWork(session_factory) as work:
        for index in range(5):
            await work.events.append(
                RunEvent(
                    run_id=persisted_run.id,
                    run_version=0,
                    event_type="run.updated",
                    payload={"index": index},
                )
            )
        await work.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        url = f"/api/runs/{persisted_run.id}/events"
        assert (await client.get(url)).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        response = await client.post(
            "/api/auth/bootstrap", headers={"Origin": settings.web_origin}, json={"token": token}
        )
        assert response.status_code == 200
        assert (await client.get(url, headers={"Last-Event-ID": "-1"})).status_code == 422
        assert (await client.get(f"/api/runs/{uuid4()}/events")).status_code == 404
        cookie = client.cookies.get("forge_session")
        actor = await app.state.auth_service.require_session(cookie)
        before = await app.state.auth_service.session_info(actor)
    inbound = asyncio.Queue()
    await inbound.put({"type": "http.request", "body": b"", "more_body": False})
    seen = []

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body.startswith(b"id: "):
                seen.append(int(body.split(b"\n", 1)[0][4:]))
                if seen == [4] and stop_kind != "disconnect":
                    if stop_kind == "revoke":
                        await app.state.auth_service.logout(actor)
                    else:
                        clock.current += timedelta(minutes=31)
                if seen == [4, 5]:
                    async with PostgresUnitOfWork(session_factory) as work:
                        await work.events.append(
                            RunEvent(
                                run_id=persisted_run.id,
                                run_version=0,
                                event_type="run.later",
                                payload={},
                            )
                        )
                        await work.commit()
                if seen == [4, 5, 6]:
                    await inbound.put({"type": "http.disconnect"})

    await asyncio.wait_for(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": spec_version},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": url,
                "raw_path": url.encode(),
                "query_string": b"after=0",
                "root_path": "",
                "headers": [
                    (b"host", b"127.0.0.1:3000"),
                    (b"last-event-id", b"3"),
                    (b"cookie", f"forge_session={cookie}".encode()),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("127.0.0.1", 3000),
            },
            inbound.get,
            send,
        ),
        timeout=5,
    )
    assert seen == ([4, 5, 6] if stop_kind == "disconnect" else [4])
    async with session_factory() as session:
        persisted_session = await session.get(OperatorSession, actor.session_id)
        assert persisted_session.idle_expires_at == before.idle_expires_at
    assert [
        event.sequence for event in await EventQuery(session_factory).page(persisted_run.id, 5)
    ] == ([6] if stop_kind == "disconnect" else [])
