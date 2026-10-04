from pathlib import Path

import pytest
from forge.application.ports.repository import RepositoryAccessDenied
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader


@pytest.mark.asyncio
async def test_read_only_tools_bound_content_and_reject_secret_paths(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("a" * 100, encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / "private.pem").write_text("secret", encoding="utf-8")
    (tmp_path / "public.txt").write_text("api_key=abcdefghijklmnop", encoding="utf-8")
    tools = BrainstormReadOnlyTools(
        RepositoryReader(tmp_path, force_python_search=True), max_bytes=32
    )
    read = await tools.read_file("notes.txt")
    assert read.truncated and len(read.content) == 32
    assert {entry.path for entry in await tools.list_files()} == {"notes.txt", "public.txt"}
    assert "abcdefghijklmnop" not in (await tools.read_file("public.txt")).content
    for forbidden in (".env", "private.pem", "../other.txt", "/tmp/other.txt", "secrets/token.txt"):
        with pytest.raises(RepositoryAccessDenied):
            await tools.read_file(forbidden)
    assert not hasattr(tools, "write_file")


@pytest.mark.asyncio
async def test_authority_is_checked_before_each_read(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("safe", encoding="utf-8")
    active = True
    calls = 0

    async def authorize() -> None:
        nonlocal calls
        calls += 1
        if not active:
            raise RepositoryAccessDenied("authority revoked")

    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path), authorize=authorize)
    assert (await tools.read_file("notes.txt")).content == "safe"
    active = False
    with pytest.raises(RepositoryAccessDenied, match="revoked"):
        await tools.read_file("notes.txt")
    assert calls == 2
