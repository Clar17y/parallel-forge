from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from forge.application.ports.jev import JevResult
from forge.application.services.semantic_search import (
    candidates_current,
    collect_candidates,
    iter_line_chunks,
    select_candidates,
)
from forge.application.services.tools import ControlledToolService, _bound_semantic_metadata
from forge.domain.event import thaw_payload
from forge.domain.policy import JevPolicy, ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.tools.repository import RepositoryReader
from test_planner_tools import PROJECT_ID, RUN_ID, TASK_ID, _context, _project, _UnitOfWork


def test_semantic_candidates_find_nonliteral_source_and_exclude_secrets(tmp_path: Path) -> None:
    (tmp_path / "worker.py").write_text(
        "def exponential_backoff(attempt):\n    return 2 ** attempt\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    reader = RepositoryReader(tmp_path, secret_paths=(".env",))

    candidates, complete = collect_candidates(reader, ".", max_candidates=8)

    assert complete is True
    assert len(candidates) == 1
    assert candidates[0].path == "worker.py"
    assert "exponential_backoff" in candidates[0].excerpt
    selected = select_candidates(candidates, {"c0": {"score": 3, "confidence": 1.0}}, top_k=3)
    assert selected[0]["path"] == "worker.py"
    assert selected[0]["score"] == 3


def test_semantic_candidates_exclude_hidden_paths_before_read_and_egress(tmp_path: Path) -> None:
    (tmp_path / ".npmrc").write_text("token=private\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("SECRET=private\n", encoding="utf-8")
    (tmp_path / ".llm-output").mkdir()
    (tmp_path / ".llm-output" / "session.txt").write_text("private\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("EXAMPLE=public\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("def visible(): pass\n", encoding="utf-8")

    candidates, complete = collect_candidates(RepositoryReader(tmp_path), ".", max_candidates=8)

    assert complete is True
    assert {item.path for item in candidates} == {".env.example", "visible.py"}


@pytest.mark.asyncio
async def test_semantic_request_contains_only_visible_source(tmp_path: Path) -> None:
    (tmp_path / ".npmrc").write_text("PRIVATE_TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("PRIVATE_KEY=secret\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("def exponential_backoff(): pass\n", encoding="utf-8")
    jev = _Jev(_work(tmp_path))

    await _controlled(tmp_path, jev).invoke(
        _context(), ToolRequest(name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
                                arguments={"query": "retry delay"})
    )

    assert [item["path"] for item in jev.requests[0].state["candidates"]] == ["visible.py"]


def test_unknown_scores_remain_visible(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(f"def {name[0]}():\n    pass\n", encoding="utf-8")
    candidates, _ = collect_candidates(RepositoryReader(tmp_path), ".", max_candidates=8)

    selected = select_candidates(candidates, {"c0": {"score": 3, "confidence": 0.8}}, top_k=1)

    assert [item["path"] for item in selected] == ["a.py", "b.py", "c.py"]
    assert selected[1]["score"] is None and selected[2]["score"] is None


def test_invalid_confidence_stays_unknown_after_score_parsing_is_shared(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("useful = True\n", encoding="utf-8")
    candidates, _ = collect_candidates(RepositoryReader(tmp_path), ".", max_candidates=8)

    selected = select_candidates(
        candidates, {"c0": {"score": 3, "confidence": 1.1}}, top_k=1
    )

    assert selected[0]["score"] is None
    assert selected[0]["confidence"] is None


def test_late_behavior_has_original_line_number_and_partial_coverage(tmp_path: Path) -> None:
    (tmp_path / "late.py").write_text(
        "\n".join(f"padding_{index} = {index}" for index in range(80))
        + "\ndef exponential_backoff(attempt):\n    return 2 ** attempt\n",
        encoding="utf-8",
    )
    reader = RepositoryReader(tmp_path)

    all_candidates, complete = collect_candidates(reader, ".", max_candidates=96)
    assert complete is True
    assert any(
        candidate.line_number + candidate.excerpt.splitlines().index(
            "def exponential_backoff(attempt):"
        ) == 81
        for candidate in all_candidates
        if "def exponential_backoff(attempt):" in candidate.excerpt
    )
    _, capped = collect_candidates(reader, ".", max_candidates=1)
    assert capped is False


def test_freshness_reads_each_distinct_source_once(tmp_path: Path) -> None:
    (tmp_path / "many.py").write_text("\n".join(f"line_{i}" for i in range(80)), encoding="utf-8")
    (tmp_path / "other.py").write_text("other\n", encoding="utf-8")

    class CountingReader(RepositoryReader):
        reads = 0

        def read_file(self, path):  # type: ignore[no-untyped-def]
            self.reads += 1
            return super().read_file(path)

    reader = CountingReader(tmp_path)
    candidates, _ = collect_candidates(reader, ".", max_candidates=96)
    reader.reads = 0

    assert len(candidates) > 1
    assert candidates_current(reader, candidates)
    assert reader.reads == 2
    assert not candidates_current(
        reader, (candidates[0], replace(candidates[1], file_hash="0" * 64))
    )


@pytest.mark.parametrize("max_chars", [0, 1, 120, 200, 360, 600, 1000, 5000])
def test_semantic_metadata_trimming_matches_drop_from_end(max_chars: int) -> None:
    metadata = {
        "candidates": [{"path": f"file-{i}.py", "excerpt": "x" * (i * 19), "score": None}
                       for i in range(9)],
        "search": {"mode": "on", "status": "ranked", "scored": True,
                   "coverage_complete": True, "returned_count": 9},
    }
    expected = json.loads(json.dumps(metadata))
    original = len(expected["candidates"])
    while expected["candidates"]:
        if len(expected["candidates"]) != original:
            expected["search"].update({"output_truncated": True,
                                       "unshown_count": original - len(expected["candidates"]),
                                       "coverage_complete": False,
                                       "returned_count": len(expected["candidates"])})
        if len(json.dumps(expected, ensure_ascii=False)) <= max_chars:
            break
        expected["candidates"].pop()
    if len(expected["candidates"]) != original:
        expected["search"].update({"output_truncated": True,
                                   "unshown_count": original - len(expected["candidates"]),
                                   "coverage_complete": False,
                                   "returned_count": len(expected["candidates"])})

    assert _bound_semantic_metadata(metadata, max_chars) == expected


def test_empty_semantic_result_keeps_its_original_flags_when_over_limit() -> None:
    metadata = {"candidates": [], "search": {"status": "off", "coverage_complete": True}}
    before = json.loads(json.dumps(metadata))

    assert _bound_semantic_metadata(metadata, 1) == before


def test_semantic_metadata_trimming_serializes_logarithmically(monkeypatch) -> None:
    import forge.application.services.tools as tools_module

    metadata = {"candidates": [{"excerpt": "x" * 30} for _ in range(96)],
                "search": {"coverage_complete": True}}
    original_dumps = json.dumps
    calls = 0

    def counted_dumps(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(tools_module.json, "dumps", counted_dumps)
    _bound_semantic_metadata(metadata, 1)
    assert calls <= 10


def test_shared_line_chunks_preserve_blank_lines_long_lines_and_line_numbers() -> None:
    chunks = list(iter_line_chunks("first\n\n" + "x" * 20 + "\nlast\n", 8))
    assert chunks == [
        (1, "first\n\n", False),
        (3, "xxxxxxxx", True),
        (4, "last\n", False),
    ]


@pytest.mark.asyncio
async def test_semantic_dispatch_fallback_marks_repository_content_untrusted(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("value = True\n", encoding="utf-8")
    service = _controlled(tmp_path, _Jev(_work(tmp_path)))
    authorization = SimpleNamespace(
        tool_name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
        arguments={"path": "."},
    )

    result = await service._dispatch(authorization)

    assert result.metadata["search"]["status"] == "unavailable"
    assert result.metadata["search"]["untrusted_repository_content"] is True


class _TrackedWork(_UnitOfWork):
    active = False

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        self.active = True
        return await super().__aenter__()

    async def __aexit__(self, *args: object) -> None:
        self.active = False
        await super().__aexit__(*args)


class _Jev:
    def __init__(self, work: _TrackedWork, *, status: str = "ranked", change: Path | None = None,
                 all_zero: bool = False) -> None:
        self.work = work
        self.status = status
        self.change = change
        self.all_zero = all_zero
        self.requests: list[object] = []

    async def evaluate(self, request, *, policy):  # type: ignore[no-untyped-def]
        assert self.work.active is False
        self.requests.append(request)
        if self.change is not None:
            self.change.write_text("changed after scoring\n", encoding="utf-8")
        excerpts = request.state.get("candidates", request.state.get("matches", []))
        answers = {
            f"c{i}": {"score": 3.0 if not self.all_zero and "exponential_backoff" in repr(item) else 0.0,
                     "confidence": 0.9}
            for i, item in enumerate(excerpts)
        }
        return JevResult(status=self.status, answers=answers, actual_model="jev-latest")


def _controlled(tmp_path: Path, jev: _Jev, *, mode: str = "on",
                max_result_chars: int = 12000) -> ControlledToolService:
    project = _project(tmp_path)
    assert project.policy is not None
    policy = ProjectPolicy.model_validate(project.policy.document).model_copy(
        update={"jev": JevPolicy(mode=mode, allow_remote=True, top_k=1,
                                  max_result_chars=max_result_chars)}
    )
    project = replace(project, policy=replace(project.policy, document=policy.model_dump(mode="json")))
    jev.work.projects.project = project
    return ControlledToolService(
        lambda: jev.work,
        repository_reader=RepositoryReader(tmp_path, secret_paths=(".env",)),
        jev_service=jev,  # type: ignore[arg-type]
    )


def _work(tmp_path: Path) -> _TrackedWork:
    return _TrackedWork(
        RunSnapshot(id=RUN_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                    state=RunState.PLANNING, policy_version=1),
        _project(tmp_path),
    )


@pytest.mark.asyncio
async def test_controlled_semantic_tool_finds_late_nonliteral_source_outside_uow(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("unrelated\n", encoding="utf-8")
    (tmp_path / "z.py").write_text("padding\n" * 50 + "def exponential_backoff():\n    pass\n", encoding="utf-8")
    work = _work(tmp_path)
    jev = _Jev(work)
    service = _controlled(tmp_path, jev)

    result = await service.invoke(_context(), ToolRequest(
        name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
        arguments={"query": "How does retry delay grow?", "path": "."},
    ))

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["search"]["scored"] is True
    assert result.metadata["candidates"][0]["path"] == "z.py"
    assert any("item 0" in question["instructions"] for question in jev.requests[0].questions.values())


@pytest.mark.asyncio
async def test_semantic_outage_unknown_and_source_change_keep_unscored_evidence(tmp_path: Path) -> None:
    source = tmp_path / "worker.py"
    source.write_text("def exponential_backoff():\n    pass\n", encoding="utf-8")
    for jev in (_Jev(_work(tmp_path), status="unavailable"),
                _Jev(_work(tmp_path), status="unknown"), _Jev(_work(tmp_path), change=source)):
        service = _controlled(tmp_path, jev)
        result = await service.invoke(_context(), ToolRequest(
            name=ToolName.REPOSITORY_SEARCH_SEMANTIC, arguments={"query": "retry timing"}
        ))
        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.metadata["search"]["scored"] is False
        assert result.metadata["candidates"]
        if jev.change is not None:
            assert result.metadata["search"]["status"] == "stale_source"
            break


@pytest.mark.asyncio
async def test_semantic_scope_denial_does_not_call_jev(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    jev = _Jev(_work(tmp_path))
    result = await _controlled(tmp_path, jev).invoke(_context(), ToolRequest(
        name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
        arguments={"query": "credential", "path": ".env"},
    ))
    assert result.status is ToolCallStatus.FAILED
    assert jev.requests == []


@pytest.mark.asyncio
async def test_semantic_all_zero_preserves_evidence_and_marks_below_floor(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(f"def {name[0]}(): pass\n", encoding="utf-8")
    jev = _Jev(_work(tmp_path), all_zero=True)

    result = await _controlled(tmp_path, jev).invoke(_context(), ToolRequest(
        name=ToolName.REPOSITORY_SEARCH_SEMANTIC, arguments={"query": "missing behavior"}
    ))

    assert [item["path"] for item in result.metadata["candidates"]] == ["a.py", "b.py"]
    assert result.metadata["search"]["status"] == "below_floor"
    assert result.metadata["search"]["scored"] is False
    assert result.metadata["search"]["absence_unknown"] is True


@pytest.mark.asyncio
async def test_semantic_shadow_has_same_bounded_result_as_off(tmp_path: Path) -> None:
    for index in range(16):
        (tmp_path / f"file{index:02}.py").write_text(
            f"def function_{index}():\n    return 'exponential_backoff'\n", encoding="utf-8"
        )
    results = []
    for mode in ("off", "shadow"):
        jev = _Jev(_work(tmp_path))
        result = await _controlled(tmp_path, jev, mode=mode, max_result_chars=1200).invoke(
            _context(), ToolRequest(name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
                                    arguments={"query": "retry delay"})
        )
        assert len(jev.requests) == (1 if mode == "shadow" else 0)
        results.append(result.metadata)

    assert results[0] == results[1]
    assert results[0]["search"]["unshown_count"] > 0
    assert len(json.dumps(thaw_payload(results[0]), ensure_ascii=False)) <= 1200


@pytest.mark.asyncio
async def test_semantic_positive_selection_reports_omitted_evidence(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("unrelated\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def exponential_backoff(): pass\n", encoding="utf-8")
    result = await _controlled(tmp_path, _Jev(_work(tmp_path))).invoke(
        _context(), ToolRequest(name=ToolName.REPOSITORY_SEARCH_SEMANTIC,
                                arguments={"query": "retry delay"})
    )
    assert [item["path"] for item in result.metadata["candidates"]] == ["b.py"]
    assert result.metadata["search"]["omitted_count"] == 1
    assert result.metadata["search"]["absence_unknown"] is True


@pytest.mark.asyncio
async def test_literal_all_zero_preserves_baseline_matches(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("retry = True\n", encoding="utf-8")
    result = await _controlled(tmp_path, _Jev(_work(tmp_path), all_zero=True)).invoke(
        _context(), ToolRequest(name=ToolName.REPOSITORY_SEARCH,
                                arguments={"literal": "retry", "path": "."})
    )
    assert [item["path"] for item in result.metadata["matches"]] == ["a.py", "b.py"]
    assert result.metadata["ranking"]["status"] == "below_floor"
    assert result.metadata["ranking"]["applied"] is False
    assert result.metadata["ranking"]["absence_unknown"] is True


@pytest.mark.asyncio
async def test_explicit_policy_literal_shadow_equals_off(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("retry = True\n", encoding="utf-8")
    results = []
    for mode in ("off", "shadow"):
        jev = _Jev(_work(tmp_path))
        result = await _controlled(tmp_path, jev, mode=mode).invoke(
            _context(), ToolRequest(name=ToolName.REPOSITORY_SEARCH,
                                    arguments={"literal": "retry", "path": "."})
        )
        assert len(jev.requests) == (1 if mode == "shadow" else 0)
        results.append(result.metadata)
    assert results[0] == results[1]
