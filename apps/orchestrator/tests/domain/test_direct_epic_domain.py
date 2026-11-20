"""Domain tests for direct-run epic decomposition."""

import json

import pytest
from forge.domain.direct import (
    DirectDecomposeTask,
    build_direct_decompose_prompt,
    parse_decompose_result,
)


def test_build_direct_decompose_prompt():
    prompt = build_direct_decompose_prompt("Build an epic feature", has_agents_md=True)
    assert "AGENTS.md" in prompt
    assert "DECOMPOSE: You are decomposing" in prompt
    assert "Build an epic feature" in prompt
    assert "tasks" in prompt


def test_parse_decompose_result_valid_json():
    raw = """
    Here is the decomposition:
    ```json
    {
      "status": "done",
      "summary": "Decomposed into two tasks",
      "tasks": [
        {"task": "Task 1: Add domain model", "shape": "quick"},
        {"task": "Task 2: Add worker execution", "shape": "planned"}
      ]
    }
    ```
    """
    result = parse_decompose_result(raw)
    assert result is not None
    assert result.status == "done"
    assert result.summary == "Decomposed into two tasks"
    assert len(result.tasks) == 2
    assert result.tasks[0] == DirectDecomposeTask(task="Task 1: Add domain model", shape="quick")
    assert result.tasks[1] == DirectDecomposeTask(task="Task 2: Add worker execution", shape="planned")


def test_parse_decompose_result_rejects_empty_tasks():
    raw = """
    ```json
    {
      "status": "done",
      "summary": "No tasks needed",
      "tasks": []
    }
    ```
    """
    assert parse_decompose_result(raw) is None


def test_parse_decompose_result_rejects_malformed_json():
    assert parse_decompose_result("not json at all") is None
    assert parse_decompose_result('{"status": "done"}') is None


def test_parse_decompose_result_handles_needs_decision():
    raw = """
    ```json
    {
      "status": "needs_decision",
      "summary": "Need clarification",
      "question": "Which architecture?",
      "options": ["Option A", "Option B"],
      "tasks": []
    }
    ```
    """
    result = parse_decompose_result(raw)
    assert result is not None
    assert result.status == "needs_decision"
    assert result.question == "Which architecture?"
    assert result.options == ("Option A", "Option B")


@pytest.mark.parametrize("child", [
    {"task": "missing shape"},
    {"task": "unknown field", "shape": "quick", "extra": True},
    {"task": "x" * 8001, "shape": "quick"},
])
def test_decomposition_rejects_schema_drift_or_truncation(child):
    raw = json.dumps({"status": "done", "summary": "tasks", "tasks": [child]})
    assert parse_decompose_result(raw) is None


@pytest.mark.parametrize("patch", [{"status": []}, {"tasks": [{"task": "x", "shape": []}]}])
def test_decomposition_rejects_wrong_json_types_without_raising(patch):
    payload = {"status": "done", "summary": "tasks", "tasks": [{"task": "x", "shape": "quick"}]}
    payload.update(patch)
    assert parse_decompose_result(json.dumps(payload)) is None


def _output(payload, representation):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    if representation == "fenced":
        return f"```json\n{raw}\n```"
    if representation == "event_text":
        return json.dumps({"type": "result", "result": f"```json\n{raw}\n```"})
    if representation == "event_object":
        return json.dumps({"response": payload})
    return raw


@pytest.mark.parametrize("representation", ["plain", "fenced", "event_text", "event_object"])
@pytest.mark.parametrize("final", [
    {"status": "blocked", "summary": "blocked", "tasks": []},
    {"status": "needs_decision", "summary": "choose", "question": "Which?", "options": ["a", "b"], "tasks": []},
    {"status": "blocked", "summary": "missing tasks"},
    {"status": "needs_decision", "summary": "missing tasks", "question": "Which?"},
    {"status": "done", "summary": "invalid", "tasks": []},
    {"type": "invalid", "summary": "missing status", "tasks": []},
    '{"status":"done", malformed}',
])
def test_decomposition_final_output_is_authoritative(representation, final):
    earlier = {"status": "done", "summary": "earlier", "tasks": [{"task": "must not launch", "shape": "quick"}]}
    result = parse_decompose_result(_output(earlier, representation) + "\n" + _output(final, representation))
    if isinstance(final, dict) and final.get("status") in {"blocked", "needs_decision"} and "tasks" in final:
        assert result is not None and result.status == final["status"]
    else:
        assert result is None


@pytest.mark.parametrize("representation", ["plain", "fenced", "event_text", "event_object"])
def test_decomposition_valid_outputs_follow_chronological_order(representation):
    payloads = [{"status": "done", "summary": label, "tasks": [{"task": label, "shape": "quick"}]} for label in ("first", "last")]
    result = parse_decompose_result("\n".join(_output(p, representation) for p in payloads))
    assert result is not None and result.tasks[0].task == "last"


@pytest.mark.parametrize("first,last", [("plain", "fenced"), ("fenced", "plain"), ("event_text", "event_object"), ("event_object", "event_text")])
def test_decomposition_mixed_output_order_and_metadata(first, last):
    earlier = {"status": "done", "summary": "first", "tasks": [{"task": "first", "shape": "quick"}]}
    final = {"status": "done", "summary": "last", "tasks": [{"task": "last", "shape": "planned"}]}
    result = parse_decompose_result(_output(earlier, first) + '\n{"type":"heartbeat"}\n' + _output(final, last) + '\n{"type":"completed"}')
    assert result is not None and result.tasks[0].task == "last"


def _provider_wire(text, provider):
    if provider == "antigravity":
        return {"event": "result", "result": {"status": "SUCCESS", "response": text}}
    if provider == "claude":
        return {"type": "result", "subtype": "success", "is_error": False, "result": text}
    if provider == "codex":
        return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    if provider == "muse":
        return {"payload_type": "run.terminal.completed", "payload": {"kind": "run_terminal", "terminal": "completed", "text": text}}
    return {"event": "result", "status": "SUCCESS", "result": text}


@pytest.mark.parametrize("provider", ["antigravity", "claude", "codex", "muse", "legacy"])
@pytest.mark.parametrize("outcome", ["done", "blocked", "needs_decision", "missing-tasks", "malformed"])
def test_decomposition_actual_wire_finality_and_quoted_fences(provider, outcome):
    task = 'Keep ```json examples and escaped "quotes" and ``` delimiters in task prose.'
    payload = {"status": outcome, "summary": "final output", "tasks": []}
    if outcome == "done":
        payload["tasks"] = [{"task": task, "shape": "quick"}]
    elif outcome == "needs_decision":
        payload["question"] = "Which?"
    elif outcome == "missing-tasks":
        payload = {"status": "blocked", "summary": "missing tasks"}
    raw = json.dumps(payload, indent=2) if outcome != "malformed" else '{"status":"done", malformed}'
    wire = json.dumps(_provider_wire(f"```json\n{raw}\n```", provider))
    earlier = json.dumps({"status": "done", "summary": "earlier", "tasks": [{"task": "must not replay", "shape": "quick"}]})
    result = parse_decompose_result(earlier + "\n" + wire + '\n{"type":"completed"}')
    if outcome in {"missing-tasks", "malformed"}:
        assert result is None
    else:
        assert result is not None and result.status == outcome
        if outcome == "done":
            assert result.tasks[0].task == task


@pytest.mark.parametrize("framed", [True, False])
def test_decomposition_literal_fence_inside_pretty_json_string(framed):
    payload = {"status": "done", "summary": "example", "tasks": [{"task": "Keep literal ```json and ``` examples", "shape": "quick"}]}
    raw = json.dumps(payload, indent=2)
    result = parse_decompose_result(f"```json\n{raw}\n```" if framed else raw)
    assert result is not None and result.tasks[0].task == payload["tasks"][0]["task"]


@pytest.mark.parametrize("event", [
    {"event": "result", "status": "FAILED", "result": "ignored"},
    {"event": "result", "result": {"status": "FAILED", "response": "ignored"}},
    {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "ignored"},
    {"type": "error", "message": "failed"},
    {"payload_type": "run.terminal.failed", "payload": {"kind": "run_terminal", "terminal": "failed", "text": "ignored"}},
    {"type": "item.completed", "item": {"type": "agent_message"}},
])
def test_decomposition_transport_failure_or_missing_final_payload_supersedes_success(event):
    earlier = json.dumps({"status": "done", "summary": "earlier", "tasks": [{"task": "must not launch", "shape": "quick"}]})
    assert parse_decompose_result(earlier + "\n" + json.dumps(event)) is None


@pytest.mark.parametrize("event", [
    {"event": "progress", "status": "SUCCESS"},
    {"type": "item.completed", "item": {"type": "command_execution", "text": "not an answer"}},
    {"payload_type": "run.progress", "payload": {"text": "not an answer"}},
    {"type": "progress", "text": "not an answer"},
])
def test_decomposition_ordinary_transport_metadata_does_not_replace_answer(event):
    answer = json.dumps({"status": "done", "summary": "answer", "tasks": [{"task": "do work", "shape": "quick"}]})
    result = parse_decompose_result(answer + "\n" + json.dumps(event))
    assert result is not None and result.status == "done"


def test_decomposition_fenced_schema_invalid_metadata_never_recovers_prior_success():
    earlier = json.dumps({"status": "done", "summary": "earlier", "tasks": [{"task": "must not launch", "shape": "quick"}]})
    assert parse_decompose_result(earlier + '\n```json\n{"type":"completed"}\n```') is None


@pytest.mark.parametrize("value", [None, False, True, "not a role object", 42, -1500.5, '{"status":"done","summary":"nested","tasks":[]}', {}, []])
def test_decomposition_standalone_json_primitives_supersede_prior_success(value):
    earlier = json.dumps({"status": "done", "summary": "earlier", "tasks": [{"task": "must not launch", "shape": "quick"}]})
    assert parse_decompose_result(earlier + "\n" + json.dumps(value)) is None


@pytest.mark.parametrize("prose", ["normal prose", "null hypothesis", "42 reasons", "- note", '"quoted phrase" is prose'])
def test_decomposition_ordinary_prose_is_not_a_json_record(prose):
    answer = json.dumps({"status": "done", "summary": "answer", "tasks": [{"task": "do work", "shape": "quick"}]})
    result = parse_decompose_result(answer + "\n" + prose)
    assert result is not None and result.status == "done"
