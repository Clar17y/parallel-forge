"""Notify persistent streams before the server waits for requests to drain."""

import asyncio
import socket
from contextlib import suppress

import pytest
import uvicorn
from forge.api.app import create_app
from forge.api.main import ForgeServer
from forge.domain.event import RunEvent
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.asyncio
async def test_shutdown_notifies_streams_before_uvicorn_drain(monkeypatch, tmp_path):
    settings = Settings(data_root=tmp_path)
    app = create_app(settings)
    notified = False

    async def drain(self, sockets=None):
        nonlocal notified
        assert app.state.shutdown_event.is_set()
        notified = True

    monkeypatch.setattr(uvicorn.Server, "shutdown", drain)
    server = ForgeServer(app, settings)
    await server.shutdown()
    assert notified


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_server_shutdown_drains_an_open_authenticated_stream(
    persisted_run, session_factory, tmp_path
):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = Settings(data_root=tmp_path, api_port=port, web_origin=f"http://127.0.0.1:{port}")
    app = create_app(settings, session_factory=session_factory)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.events.append(
            RunEvent(run_id=persisted_run.id, run_version=0, event_type="run.ready", payload={})
        )
        await work.commit()
    token = await app.state.auth_service.issue_bootstrap()
    server = ForgeServer(app, settings)
    server.config.access_log = False
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
            async with AsyncClient(base_url=settings.web_origin) as client:
                response = await client.post(
                    "/api/auth/bootstrap",
                    json={"token": token},
                    headers={"Origin": settings.web_origin},
                )
                assert response.status_code == 200
                async with client.stream("GET", f"/api/runs/{persisted_run.id}/events") as stream:
                    assert stream.status_code == 200
                    lines = stream.aiter_lines()
                    assert await anext(lines) == "id: 1"
                    server.should_exit = True
                    await serving
                    assert app.state.shutdown_event.is_set()
                    assert [line async for line in lines][-1] == ""
    finally:
        if not serving.done():
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving
        listener.close()
