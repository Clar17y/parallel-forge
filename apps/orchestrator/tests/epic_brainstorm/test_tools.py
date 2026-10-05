from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from forge.application.ports.repository import (
    FileRead,
    InstructionDocument,
    RepositoryAccessDenied,
    RepositoryEntry,
    SearchMatch,
)
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader


def _private_key_text(length: int) -> str:
    kind = "PRIVATE KEY"
    return f"-----BEGIN {kind}-----\n" + "A" * length + f"\n-----END {kind}-----"


def _other_private_key_text(kind: str, body: str, *, closed: bool = True) -> str:
    hyphens = "----" if kind.startswith("SSH2") else "-----"
    gap = " " if kind.startswith("SSH2") else ""
    start = f"{hyphens}{gap}BEGIN {kind}{gap}{hyphens}"
    end = f"{hyphens}{gap}END {kind}{gap}{hyphens}"
    return f"{start}\n{body}\n" + (end if closed else "")


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


@pytest.mark.asyncio
async def test_search_private_key_markers_follow_source_order_on_same_line(tmp_path: Path) -> None:
    kind = "private key"
    begin = f"-----BEGIN {kind}-----"
    end = f"-----END {kind}-----"
    (tmp_path / "notes.txt").write_text(
        f"{begin}\nFIRST_BODY\n{end} {begin}\nSECOND_BODY\n{end} {begin} {end} {begin}\n"
        "THIRD_BODY\n",
        encoding="utf-8",
    )
    tools = BrainstormReadOnlyTools(
        RepositoryReader(tmp_path, force_python_search=True), max_bytes=32
    )
    for body in ("FIRST_BODY", "SECOND_BODY", "THIRD_BODY"):
        matches = await tools.search(body)
        assert len(matches) == 1
        assert matches[0].line_text == "[REDACTED]"
    assert (await tools.read_file("notes.txt")).truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
@pytest.mark.parametrize("separator", ("\x0c\x0c", "\u2028\u2028", "\r\r"))
async def test_search_fails_closed_on_backend_line_number_ambiguity(
    tmp_path: Path, force_python: bool, separator: str
) -> None:
    (tmp_path / "notes.txt").write_bytes(
        (f"prefix{separator}\n" + _private_key_text(16) + "\n").encode("utf-8")
    )
    reader = RepositoryReader(tmp_path, force_python_search=force_python)
    if not force_python:
        assert reader._select_rg_executable() is not None
    tools = BrainstormReadOnlyTools(reader)
    matches = await tools.search("AAAAAAAA")
    assert matches and all(match.line_text == "[REDACTED]" for match in matches)


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
async def test_search_fails_closed_when_reader_prefix_cannot_bind_match(
    tmp_path: Path, force_python: bool
) -> None:
    (tmp_path / "notes.txt").write_text("safe\n" * 12 + _private_key_text(20), encoding="utf-8")
    reader = RepositoryReader(tmp_path, max_file_bytes=24, force_python_search=force_python)
    if not force_python:
        assert reader._select_rg_executable() is not None
    tools = BrainstormReadOnlyTools(reader)
    matches = await tools.search("AAAA")
    if force_python:
        assert not matches  # Python search scans the same bounded prefix.
    else:
        assert matches and all(match.line_text == "[REDACTED]" for match in matches)


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
@pytest.mark.parametrize("kind", ("PGP PRIVATE KEY BLOCK", "SSH2 ENCRYPTED PRIVATE KEY"))
@pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
async def test_multitoken_key_markers_are_hidden_in_all_read_surfaces(
    tmp_path: Path, force_python: bool, kind: str, line_ending: str
) -> None:
    body = "FAKE_KEY_BODY"
    content = ("safe\n" + _other_private_key_text(kind, body)).replace("\n", line_ending)
    for name in ("notes.txt", "AGENTS.md"):
        (tmp_path / name).write_bytes(content.encode("utf-8"))
    reader = RepositoryReader(tmp_path, force_python_search=force_python)
    if not force_python:
        assert reader._select_rg_executable() is not None
    tools = BrainstormReadOnlyTools(reader)
    assert (await tools.read_file("notes.txt")).content.startswith("safe" + line_ending)
    assert body not in (await tools.read_file("notes.txt")).content
    assert all(body not in item.content for item in await tools.read_instructions())
    assert all(item.line_text == "[REDACTED]" for item in await tools.search(body))


@pytest.mark.asyncio
async def test_unclosed_multitoken_key_is_hidden_before_byte_cut(tmp_path: Path) -> None:
    body = "FAKE_KEY_BODY" * 20
    (tmp_path / "notes.txt").write_text(
        _other_private_key_text("PGP PRIVATE KEY BLOCK", body, closed=False), encoding="utf-8"
    )
    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path, max_file_bytes=100), max_bytes=8)
    read = await tools.read_file("notes.txt")
    assert read.truncated and body not in read.content
    assert all(body not in item.line_text for item in await tools.search("FAKE_KEY"))


@pytest.mark.asyncio
async def test_multitoken_markers_follow_source_order_and_preserve_safe_suffix(
    tmp_path: Path,
) -> None:
    kind = "pgp private key block"
    begin = f"-----BEGIN {kind}-----"
    end = f"-----END {kind}-----"
    content = f"before\n{begin}\nFIRST_BODY\n{end} {begin}\nSECOND_BODY\n{end}\nafter"
    (tmp_path / "notes.txt").write_text(content, encoding="utf-8")
    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path, force_python_search=True))
    read = (await tools.read_file("notes.txt")).content
    assert read.startswith("before") and read.endswith("after")
    assert "FIRST_BODY" not in read and "SECOND_BODY" not in read
    for body in ("FIRST_BODY", "SECOND_BODY"):
        assert (await tools.search(body))[0].line_text == "[REDACTED]"
    assert (await tools.search("after"))[0].line_text == "after"


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
async def test_mismatched_end_marker_does_not_expose_open_key_body(
    tmp_path: Path, force_python: bool
) -> None:
    start = "-----BEGIN PGP PRIVATE KEY BLOCK-----"
    wrong_end = "-----END RSA PRIVATE KEY-----"
    (tmp_path / "notes.txt").write_text(f"{start}\n{wrong_end}\nFAKE_KEY_BODY\n", encoding="utf-8")
    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path, force_python_search=force_python))
    assert "FAKE_KEY_BODY" not in (await tools.read_file("notes.txt")).content
    assert (await tools.search("FAKE_KEY_BODY"))[0].line_text == "[REDACTED]"


@pytest.mark.asyncio
async def test_search_does_not_return_unbound_match_text(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("safe\n", encoding="utf-8")
    reader = RepositoryReader(tmp_path, force_python_search=True)
    original_search = reader.search

    def mismatched_search(literal: str, path: str = "."):
        from forge.application.ports.repository import SearchMatch

        assert literal == "FAKE_KEY_BODY"
        return (SearchMatch(path="notes.txt", line_number=1, line_text=literal),)

    with patch.object(reader, "search", mismatched_search):
        matches = await BrainstormReadOnlyTools(reader).search("FAKE_KEY_BODY")
    assert matches[0].line_text == "[REDACTED]"
    assert original_search("safe")


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
@pytest.mark.parametrize("inner_kind", ("RSA PRIVATE KEY", "PGP PRIVATE KEY BLOCK"))
async def test_nested_key_markers_do_not_release_outer_private_context(
    tmp_path: Path, force_python: bool, inner_kind: str
) -> None:
    outer_kind = "RSA PRIVATE KEY"
    body = "NESTED_FAKE_KEY_BODY"
    content = "\n".join(
        (
            f"-----BEGIN {outer_kind}-----",
            f"-----BEGIN {inner_kind}-----",
            f"-----END {outer_kind}-----",
            body,
            f"-----END {inner_kind}-----",
        )
    )
    (tmp_path / "notes.txt").write_text(content, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(content, encoding="utf-8")
    reader = RepositoryReader(tmp_path, force_python_search=force_python)
    if not force_python:
        assert reader._select_rg_executable() is not None
    tools = BrainstormReadOnlyTools(reader)
    assert body not in (await tools.read_file("notes.txt")).content
    assert all(body not in item.content for item in await tools.read_instructions())
    matches = await tools.search(body)
    assert len(matches) == 2 and all(item.line_text == "[REDACTED]" for item in matches)


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
async def test_credential_bearing_paths_never_escape_read_tools(
    tmp_path: Path, force_python: bool
) -> None:
    credential = "api_key=" + "syntheticvalue123456"
    private_path = "docs/" + credential + ".txt"
    (tmp_path / "docs").mkdir()
    (tmp_path / private_path).write_text("VISIBLE_MATCH", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("VISIBLE_MATCH", encoding="utf-8")
    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path, force_python_search=force_python))
    if not force_python:
        assert tools._reader._select_rg_executable() is not None
    for method in (tools.read_file, tools.list_files):
        with pytest.raises(RepositoryAccessDenied) as error:
            await method(private_path)
        assert credential not in str(error.value)
    with pytest.raises(RepositoryAccessDenied):
        await tools.search("VISIBLE_MATCH", private_path)
    with pytest.raises(RepositoryAccessDenied):
        await tools.read_instructions(private_path)
    assert {item.path for item in await tools.list_files("docs")} == set()
    assert {item.path for item in await tools.search("VISIBLE_MATCH")} == {"safe.txt"}
    assert (await tools.read_file("safe.txt")).content == "VISIBLE_MATCH"


@pytest.mark.asyncio
async def test_malicious_returned_repository_metadata_is_rejected() -> None:
    credential = "password=" + "syntheticvalue123456"

    class Reader:
        root = SimpleNamespace(path=Path("/tmp") / credential)

        def excludes_paths(self, paths):
            return False

        def list_files(self, path):
            return (RepositoryEntry(path="safe.txt", kind=credential, byte_count=1),)

        def read_file(self, path):
            return FileRead(
                path="safe.txt", content="safe", original_byte_count=credential, truncated=False
            )

        def search(self, literal, path):
            return (SearchMatch(path=credential, line_number=1, line_text=literal),)

        def read_instructions(self, target_path):
            return (
                InstructionDocument(
                    path=credential, content="safe", original_byte_count=1, truncated=False
                ),
            )

    tools = BrainstormReadOnlyTools(Reader())
    with pytest.raises(RepositoryAccessDenied):
        _ = tools.root
    for operation in (tools.list_files(), tools.read_file("safe.txt")):
        with pytest.raises(RepositoryAccessDenied) as error:
            await operation
        assert credential not in str(error.value)
    assert await tools.search("safe") == ()
    assert await tools.read_instructions() == ()
    with pytest.raises(RepositoryAccessDenied):
        await tools.excludes_paths((credential,))


@pytest.mark.asyncio
@pytest.mark.parametrize("force_python", (False, True))
@pytest.mark.parametrize("version", ("2", "3"))
async def test_putty_private_text_is_hidden_across_read_surfaces(
    tmp_path: Path, force_python: bool, version: str
) -> None:
    heading = "PuTTY-User-Key-File-" + version
    body = "SYNTHETIC_PRIVATE_BODY"
    content = f"safe\n{heading}: ssh-rsa\nEncryption: none\nPrivate-Lines: 1\n{body}\n"
    (tmp_path / "notes.txt").write_text(content, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(content, encoding="utf-8")
    (tmp_path / "sample.PPK").write_text(content, encoding="utf-8")
    reader = RepositoryReader(tmp_path, force_python_search=force_python)
    if not force_python:
        assert reader._select_rg_executable() is not None
    tools = BrainstormReadOnlyTools(reader, max_bytes=128)
    with pytest.raises(RepositoryAccessDenied):
        await tools.read_file("sample.PPK")
    assert "sample.PPK" not in {entry.path for entry in await tools.list_files()}
    with pytest.raises(RepositoryAccessDenied):
        await tools.search(body, "sample.PPK")
    read = await tools.read_file("notes.txt")
    assert read.content.startswith("safe") and body not in read.content
    assert all(body not in document.content for document in await tools.read_instructions())
    matches = await tools.search(body)
    assert matches and all(item.line_text == "[REDACTED]" for item in matches)


@pytest.mark.asyncio
async def test_truncated_putty_context_fails_closed_before_byte_cut(tmp_path: Path) -> None:
    heading = "PuTTY-User-Key-File-3"
    (tmp_path / "notes.txt").write_text(
        f"safe\n{heading}: ssh-ed25519\nPrivate-Lines: 1\n" + "SYNTHETIC_BODY" * 20,
        encoding="utf-8",
    )
    tools = BrainstormReadOnlyTools(RepositoryReader(tmp_path, max_file_bytes=80), max_bytes=12)
    read = await tools.read_file("notes.txt")
    assert read.truncated and read.content.startswith("safe")
    assert "SYNTHETIC" not in read.content
    assert all("SYNTHETIC" not in item.line_text for item in await tools.search("SYNTHETIC"))
