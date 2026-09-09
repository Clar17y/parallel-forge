import pytest
from forge.api.app import create_app
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health_reports_process_role_without_touching_the_database() -> None:
    app = create_app(
        Settings(
            process_role="api",
            database_url="postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge",
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "role": "api"}
