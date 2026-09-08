"""GitHub import composition is opt-in and closes only owned transports."""

from unittest.mock import AsyncMock

import pytest
from forge.api.app import create_app
from forge.settings import Settings


@pytest.mark.asyncio
async def test_configured_import_is_composed_and_owned_client_closes(tmp_path, monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr("forge.api.app.GitHubClient", lambda *args, **kwargs: client)
    app = create_app(
        Settings(data_root=tmp_path, github_token_reference="env://FORGE_TEST_GITHUB_TOKEN"),
        unit_of_work_factory=lambda: None,
    )
    assert app.state.github_issue_import_service is not None
    client.get_issue.assert_not_called()
    async with app.router.lifespan_context(app):
        client.aclose.assert_not_called()
    client.aclose.assert_awaited_once()


def test_unconfigured_import_stays_unavailable(tmp_path):
    app = create_app(Settings(data_root=tmp_path), unit_of_work_factory=lambda: None)
    assert app.state.github_issue_import_service is None
