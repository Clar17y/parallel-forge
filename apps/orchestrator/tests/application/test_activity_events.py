"""Tests for safe bounded presentation metadata in tool_call.completed events."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest
from forge.application.services.tools import _is_safe_relative_path, _safe_presentation_metadata
from forge.domain.tool import (
    ToolCallStatus,
    ToolName,
    ToolRequest,
    ToolResult,
)

from apps.orchestrator.tests.application.test_tool_audit import (
    _context,
    _service,
)


@pytest.mark.asyncio
async def test_tool_event_includes_safe_path_subject_and_omits_raw_content(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Hello World\nSome sensitive content that should not leak.", encoding="utf-8")

    service, work, _ = _service(tmp_path)
    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)
    assert result.status is ToolCallStatus.SUCCEEDED

    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.event_type == "tool_call.completed"

    assert "path" in event.payload, f"Expected 'path' in event.payload, got keys: {list(event.payload.keys())}"
    assert event.payload["path"] == "README.md"
    assert "byte_count" in event.payload
    assert event.payload["byte_count"] > 0
    # Raw content must NEVER leak into the event payload
    assert "content" not in event.payload
    assert "Some sensitive content" not in str(event.payload)


@pytest.mark.asyncio
async def test_tool_event_includes_list_and_search_counts_and_omits_matches_and_entries(
    tmp_path: Path,
) -> None:
    (tmp_path / "file1.txt").write_text("apple banana\n", encoding="utf-8")
    (tmp_path / "file2.txt").write_text("banana cherry secret_token_xyz\n", encoding="utf-8")

    service, work, _ = _service(tmp_path)
    context = _context()

    # List files
    list_req = ToolRequest(name=ToolName.REPOSITORY_LIST_FILES, arguments={"path": "."})
    list_res = await service.invoke(context, list_req)
    assert list_res.status is ToolCallStatus.SUCCEEDED

    list_event = work.events.events[0]
    assert list_event.payload["tool_name"] == ToolName.REPOSITORY_LIST_FILES.value
    assert list_event.payload["entry_count"] >= 2
    assert "entries" not in list_event.payload

    # Search
    search_req = ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "banana", "path": "."})
    search_res = await service.invoke(context, search_req)
    assert search_res.status is ToolCallStatus.SUCCEEDED

    search_event = work.events.events[1]
    assert search_event.payload["tool_name"] == ToolName.REPOSITORY_SEARCH.value
    assert search_event.payload["match_count"] >= 2
    assert "matches" not in search_event.payload
    assert "ranking" not in search_event.payload
    assert "secret_token_xyz" not in str(search_event.payload)


def test_safe_presentation_metadata_strictly_rejects_unsafe_paths_and_leaks() -> None:
    assert _is_safe_relative_path("src/index.ts") is True
    assert _is_safe_relative_path("README.md") is True
    assert _is_safe_relative_path("/etc/passwd") is False
    assert _is_safe_relative_path("C:\\Windows\\System32") is False
    assert _is_safe_relative_path("../secret.env") is False
    assert _is_safe_relative_path("src/../../secret.env") is False
    assert _is_safe_relative_path("") is False
    assert _is_safe_relative_path(123) is False
    assert _is_safe_relative_path(" weird.txt ") is False
    assert _is_safe_relative_path("line\nbreak.txt") is False
    assert _is_safe_relative_path("a" * 513) is False

    # Path traversal attempt in REPOSITORY_READ_FILE
    traversal_result = ToolResult(
        tool_name=ToolName.REPOSITORY_READ_FILE,
        status=ToolCallStatus.SUCCEEDED,
        metadata=MappingProxyType({"path": "../secret.env", "content": "SUPER_SECRET", "original_byte_count": 12}),
    )
    meta = _safe_presentation_metadata(traversal_result)
    assert "path" not in meta
    assert "content" not in meta
    assert "SUPER_SECRET" not in str(meta)
    assert meta["byte_count"] == 12


def test_safe_presentation_metadata_for_named_check_and_git_commit() -> None:
    # Named check with exit code
    check_passed = ToolResult(
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        status=ToolCallStatus.SUCCEEDED,
        metadata=MappingProxyType({
            "command_name": "unit",
            "exit_code": 0,
            "timed_out": False,
            "caller_cancelled": False,
            "stdout_text": "PASSED 42 tests",
            "stderr_text": "",
        }),
    )
    meta_passed = _safe_presentation_metadata(check_passed)
    assert "command_name" not in meta_passed
    assert meta_passed["exit_code"] == 0
    assert meta_passed["timed_out"] is False
    assert meta_passed["caller_cancelled"] is False
    assert "stdout_text" not in meta_passed
    assert "PASSED 42 tests" not in str(meta_passed)

    # Git commit
    sha = "abcdef0123456789abcdef0123456789abcdef01"
    commit_res = ToolResult(
        tool_name=ToolName.GIT_COMMIT,
        status=ToolCallStatus.SUCCEEDED,
        metadata=MappingProxyType({
            "new_sha": sha,
            "previous_sha": "0000000000000000000000000000000000000000",
            "tree_sha": "1111111111111111111111111111111111111111",
        }),
    )
    meta_commit = _safe_presentation_metadata(commit_res)
    assert meta_commit["commit_sha"] == sha
    assert "previous_sha" not in meta_commit
    assert "tree_sha" not in meta_commit


def test_presentation_counts_reject_strings_bytes_and_boolean() -> None:
    for tool_name, metadata, count_name in (
        (ToolName.REPOSITORY_LIST_FILES, {"entries": "secret"}, "entry_count"),
        (ToolName.REPOSITORY_SEARCH, {"matches": "secret", "ranking": {"match_count": True}}, "match_count"),
        (ToolName.REPOSITORY_READ_INSTRUCTIONS, {"documents": "secret"}, "document_count"),
        (ToolName.GIT_DIFF, {"changed_paths": "secret"}, "changed_path_count"),
    ):
        result = ToolResult(tool_name=tool_name, status=ToolCallStatus.SUCCEEDED, metadata=metadata)
        assert count_name not in _safe_presentation_metadata(result)
