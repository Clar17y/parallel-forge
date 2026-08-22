from __future__ import annotations

from pathlib import Path

import pytest
from forge.application.ports.repository import (
    BinaryRepositoryFile,
    FileRead,
    RepositoryAccessDenied,
    RepositoryEncodingError,
    RepositoryEntry,
    SearchMatch,
)
from forge.tools.repository import RepositoryReader


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_list_files_is_sorted_bounded_and_skips_fixed_secret_and_configured_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "z.txt").write_bytes(b"zz")
    (root / "a.txt").write_bytes(b"a")
    (root / ".env").write_bytes(b"TOKEN=secret")
    (root / ".env.example").write_bytes(b"TOKEN=example")
    for excluded in (
        ".git",
        ".worktrees",
        ".forge-worktrees",
        "node_modules",
        ".venv",
        "venv",
        "env",
    ):
        (root / excluded).mkdir()
        (root / excluded / "hidden.txt").write_bytes(b"hidden")
    virtual = root / "tools"
    virtual.mkdir()
    (virtual / "pyvenv.cfg").write_bytes(b"home = hidden")
    (virtual / "hidden.py").write_bytes(b"hidden")
    (root / "managed").mkdir()
    (root / "managed" / "hidden.py").write_bytes(b"hidden")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "blob").write_bytes(b"hidden")

    reader = RepositoryReader(
        root,
        secret_paths=(".env",),
        managed_worktree_paths=("managed",),
        artifact_paths=("artifacts",),
    )

    assert reader.list_files() == (
        RepositoryEntry(path=".env.example", kind="file", byte_count=len(b"TOKEN=example")),
        RepositoryEntry(path="a.txt", kind="file", byte_count=1),
        RepositoryEntry(path="z.txt", kind="file", byte_count=2),
    )


def test_list_files_rejects_a_result_that_exceeds_the_entry_bound(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    for name in ("a.txt", "b.txt"):
        (root / name).write_bytes(b"x")

    reader = RepositoryReader(root, max_list_entries=1)

    with pytest.raises(RepositoryAccessDenied):
        reader.list_files()


def test_read_file_returns_exact_text_and_truthful_byte_metadata(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    source = "line one\r\nline two\n"
    (root / "README.md").write_bytes(source.encode("utf-8"))
    reader = RepositoryReader(root)

    result = reader.read_file("README.md")

    assert isinstance(result, FileRead)
    assert result.path == "README.md"
    assert result.content == source
    assert result.original_byte_count == len(source.encode("utf-8"))
    assert result.truncated is False


def test_read_file_truncates_at_a_valid_utf8_boundary_without_normalizing_text(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    source = "éééé"
    (root / "unicode.txt").write_bytes(source.encode("utf-8"))
    reader = RepositoryReader(root, max_file_bytes=5)

    result = reader.read_file("unicode.txt")

    assert result.content == "éé"
    assert result.original_byte_count == 8
    assert result.truncated is True
    assert len(result.content.encode("utf-8")) <= 5


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("binary.bin", b"prefix\x00suffix", BinaryRepositoryFile),
        ("invalid.txt", b"invalid\xffutf8", RepositoryEncodingError),
    ],
)
def test_read_file_rejects_binary_and_invalid_utf8_before_returning_content(
    tmp_path: Path, name: str, data: bytes, error: type[Exception]
) -> None:
    root = _repository(tmp_path)
    (root / name).write_bytes(data)
    reader = RepositoryReader(root)

    with pytest.raises(error):
        reader.read_file(name)


def test_read_file_denies_secret_and_configured_exclusion_before_opening_bytes(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / ".env").write_bytes(b"TOKEN=secret")
    (root / "managed").mkdir()
    (root / "managed" / "secret.txt").write_bytes(b"secret")
    reader = RepositoryReader(root, secret_paths=(".env",), managed_worktree_paths=("managed",))

    with pytest.raises(RepositoryAccessDenied, match="secret-designated path"):
        reader.read_file(".env")
    with pytest.raises(RepositoryAccessDenied):
        reader.read_file("managed/secret.txt")


def test_read_file_denies_files_below_a_virtual_environment_directory(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    virtual = root / "tools"
    virtual.mkdir()
    (virtual / "pyvenv.cfg").write_bytes(b"home = hidden")
    (virtual / "hidden.py").write_bytes(b"hidden")
    reader = RepositoryReader(root)

    with pytest.raises(RepositoryAccessDenied):
        reader.read_file("tools/hidden.py")


@pytest.mark.parametrize("value", [0, -1, True])
def test_reader_rejects_nonpositive_or_boolean_bounds(tmp_path: Path, value: object) -> None:
    root = _repository(tmp_path)

    with pytest.raises((TypeError, ValueError)):
        RepositoryReader(root, max_file_bytes=value)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        RepositoryReader(root, max_list_entries=value)  # type: ignore[arg-type]


def test_search_is_literal_and_returns_deterministic_line_matches(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "z.txt").write_text("other\n-needle.* [x]\nneedle\n", encoding="utf-8")
    (root / "a.txt").write_text("needle first\nneedle second\n", encoding="utf-8")
    reader = RepositoryReader(root, max_search_matches=100)

    result = reader.search("-needle.*")

    assert result == (SearchMatch(path="z.txt", line_number=2, line_text="-needle.* [x]"),)
    assert reader.search("not present") == ()


def test_search_enforces_global_match_cap_in_path_and_line_order(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    for name in ("c.txt", "a.txt", "b.txt"):
        (root / name).write_text("hit\n", encoding="utf-8")
    reader = RepositoryReader(root, max_search_matches=2)

    result = reader.search("hit")

    assert result == (
        SearchMatch(path="a.txt", line_number=1, line_text="hit"),
        SearchMatch(path="b.txt", line_number=1, line_text="hit"),
    )


def test_search_fails_closed_before_inspecting_candidates_over_byte_cap(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "a.txt").write_bytes(b"hit\n")
    (root / "b.txt").write_bytes(b"hit\n")
    reader = RepositoryReader(root, max_search_bytes=5)

    with pytest.raises(RepositoryAccessDenied):
        reader.search("hit")


def test_search_omits_exclusions_but_keeps_env_example(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "visible.txt").write_text("needle\n", encoding="utf-8")
    (root / ".env").write_text("needle\n", encoding="utf-8")
    (root / ".env.example").write_text("needle\n", encoding="utf-8")
    (root / ".hidden.txt").write_text("needle\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "hidden.txt").write_text("needle\n", encoding="utf-8")
    (root / "managed").mkdir()
    (root / "managed" / "hidden.txt").write_text("needle\n", encoding="utf-8")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "hidden.txt").write_text("needle\n", encoding="utf-8")
    virtual = root / "virtual"
    virtual.mkdir()
    (virtual / "pyvenv.cfg").write_text("home = hidden\n", encoding="utf-8")
    (virtual / "hidden.txt").write_text("needle\n", encoding="utf-8")
    reader = RepositoryReader(
        root,
        secret_paths=(".env",),
        managed_worktree_paths=("managed",),
        artifact_paths=("artifacts",),
    )

    result = reader.search("needle")

    assert tuple(match.path for match in result) == (".env.example", "visible.txt")


@pytest.mark.parametrize(
    ("name", "data"),
    [("binary.bin", b"needle\x00hidden"), ("invalid.txt", b"needle\xffhidden")],
)
def test_search_skips_binary_and_invalid_utf8_like_bounded_reader(
    tmp_path: Path, name: str, data: bytes
) -> None:
    root = _repository(tmp_path)
    (root / name).write_bytes(data)
    (root / "valid.txt").write_text("needle\n", encoding="utf-8")

    result = RepositoryReader(root).search("needle")

    assert result == (SearchMatch(path="valid.txt", line_number=1, line_text="needle"),)


@pytest.mark.parametrize("literal", ["", "contains\x00nul", "contains\udcff"])
def test_search_rejects_invalid_literal(tmp_path: Path, literal: str) -> None:
    root = _repository(tmp_path)

    with pytest.raises(ValueError):
        RepositoryReader(root).search(literal)


@pytest.mark.parametrize("name", ["max_search_matches", "max_search_bytes"])
def test_search_rejects_nonpositive_or_boolean_bounds(tmp_path: Path, name: str) -> None:
    root = _repository(tmp_path)

    with pytest.raises((TypeError, ValueError)):
        RepositoryReader(root, **{name: 0})
    with pytest.raises((TypeError, ValueError)):
        RepositoryReader(root, **{name: True})
