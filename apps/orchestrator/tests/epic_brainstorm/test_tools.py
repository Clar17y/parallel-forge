from pathlib import Path

import pytest
from forge.application.ports.repository import RepositoryAccessDenied, RepositoryEntry
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader


def _private_key_text(length: int) -> str:
    kind = "PRIVATE KEY"
    return f"-----BEGIN {kind}-----\n" + "A" * length + f"\n-----END {kind}-----"


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


@pytest.mark.asyncio
async def test_private_key_is_redacted_before_byte_limit_even_with_reader_truncation(
    tmp_path: Path,
) -> None:
    key = _private_key_text(200)
    (tmp_path / "public.txt").write_text(key, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(key, encoding="utf-8")
    reader = RepositoryReader(tmp_path, max_file_bytes=64, force_python_search=True)
    tools = BrainstormReadOnlyTools(reader, max_bytes=32)
    read = await tools.read_file("public.txt")
    assert read.truncated
    assert "BEGIN PRIVATE" not in read.content
    assert len(read.content.encode("utf-8")) <= 32
    for match in await tools.search("BEGIN", "."):
        assert "BEGIN PRIVATE" not in match.line_text
        assert len(match.line_text.encode("utf-8")) <= 32
    for match in await tools.search("AAAA", "."):
        assert match.line_text == "[REDACTED]"
    for document in await tools.read_instructions():
        assert document.truncated
        assert "BEGIN PRIVATE" not in document.content
        assert len(document.content.encode("utf-8")) <= 32


@pytest.mark.asyncio
async def test_unicode_and_redaction_growth_respect_utf8_limit(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("界" * 20, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("界" * 20 + "\n" + "api_key=x", encoding="utf-8")
    tools = BrainstormReadOnlyTools(
        RepositoryReader(tmp_path, force_python_search=True), max_bytes=7, max_items=1
    )
    read = await tools.read_file("notes.txt")
    assert read.truncated and read.content == "界界"
    instructions = await tools.read_instructions()
    assert len(instructions) == 1 and instructions[0].truncated
    assert instructions[0].content == "界界"
    matches = await tools.search("界")
    assert len(matches) == 1 and matches[0].line_text == "界界"


@pytest.mark.asyncio
async def test_hidden_search_marker_respects_small_byte_limit(tmp_path: Path) -> None:
    key = _private_key_text(80)
    (tmp_path / "notes.txt").write_text(key, encoding="utf-8")
    tools = BrainstormReadOnlyTools(
        RepositoryReader(tmp_path, force_python_search=True), max_bytes=1
    )
    matches = await tools.search("AAAA")
    assert matches
    assert all(len(match.line_text.encode("utf-8")) <= 1 for match in matches)


@pytest.mark.asyncio
async def test_list_files_stops_after_item_limit() -> None:
    class BoundedReader:
        def list_files(self, path):
            yield RepositoryEntry(path="one.txt", kind="file", byte_count=1)
            raise AssertionError("read beyond max_items")

    tools = BrainstormReadOnlyTools(BoundedReader(), max_items=1)
    entries = await tools.list_files()
    assert [item.path for item in entries] == ["one.txt"]
