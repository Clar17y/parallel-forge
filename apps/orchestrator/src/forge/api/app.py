"""FastAPI application factory."""

from fastapi import FastAPI

from forge.api.routes.health import router_for
from forge.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the API without opening a database connection."""

    resolved_settings = settings or Settings(process_role="api")
    app = FastAPI(title="Parallel Forge", version="0.1.0")
    app.include_router(router_for(resolved_settings.process_role), prefix="/api")
    return app
