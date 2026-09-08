"""API process entry point."""

from socket import socket

import uvicorn
from fastapi import FastAPI

from forge.api.app import create_app
from forge.settings import Settings


class ForgeServer(uvicorn.Server):
    """Stop SSE before Uvicorn begins waiting for long-lived requests to drain."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self._forge_app = app
        super().__init__(
            uvicorn.Config(
                app, host=settings.bind_host, port=settings.api_port, timeout_graceful_shutdown=5
            )
        )

    async def shutdown(self, sockets: list[socket] | None = None) -> None:
        self._forge_app.state.shutdown_event.set()
        await super().shutdown(sockets=sockets)


def run() -> None:
    """Start the API process using validated local settings."""

    settings = Settings(process_role="api")
    ForgeServer(create_app(settings), settings).run()
