"""Only safe current prompt metadata crosses the authenticated API."""

import hashlib

import pytest


@pytest.mark.asyncio
async def test_prompt_metadata_uses_configured_files_and_never_exposes_instruction_text(
    task10_client, task10_route_context, route_headers, tmp_path
) -> None:
    expected = {}
    for role in ("planner", "developer", "reviewer"):
        directory = tmp_path / role
        directory.mkdir()
        text = f"<!-- forge-instruction-version: test-{role}-1 -->\nPrivate role instructions.\n"
        (directory / "instructions.md").write_text(text, encoding="utf-8", newline="\n")
        expected[role] = hashlib.sha256(text.encode()).hexdigest()
    task10_route_context.app.state.settings.prompt_root = tmp_path
    response = await task10_client.get(
        "/api/agent-prompts", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200
    assert {item["role"]: item["digest"] for item in response.json()} == expected
    assert all(item["scope"] == "current_configuration" for item in response.json())
    assert "Private role" not in response.text
    assert all(set(item) == {"role", "version", "digest", "scope"} for item in response.json())


@pytest.mark.asyncio
async def test_prompt_metadata_fails_safely_and_requires_auth(
    task10_client, task10_route_context, route_headers, tmp_path
) -> None:
    task10_route_context.app.state.settings.prompt_root = tmp_path / "private-missing-folder"
    headers = {"Host": route_headers["Host"]}
    response = await task10_client.get("/api/agent-prompts", headers=headers)
    assert response.status_code == 503
    assert "private-missing-folder" not in response.text
    task10_client.cookies.clear()
    response = await task10_client.get("/api/agent-prompts", headers=headers)
    assert response.status_code == 401
