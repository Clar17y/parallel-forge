"""API process entry point."""

import uvicorn

from forge.api.app import create_app
from forge.settings import Settings


def run() -> None:
    """Start the API process using validated local settings."""

    settings = Settings(process_role="api")
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.api_port,
    )
