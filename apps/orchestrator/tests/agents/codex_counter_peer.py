"""Stateless fake primary: all continuation evidence comes from the app-server request.

This peer has no repository access or persisted step counter. It requests Forge
tools and derives the next bounded decision from the schema and durable context.
"""

import json
import sys
import tomllib
from uuid import uuid4

configuration_args = sys.argv[1:]
assert len(configuration_args) % 2 == 0 and set(configuration_args[::2]) == {"-c"}
configuration = tomllib.loads("\n".join(configuration_args[1::2]))
model, effort = configuration["model"], configuration["model_reasoning_effort"]
assert model == "gpt-6-astra" and effort == "low"
assert configuration["model_provider"] == "openai"
assert configuration["forced_login_method"] == "chatgpt"
assert configuration["web_search"] == "disabled" and configuration["notify"] == []
assert configuration["project_doc_max_bytes"] == 0
PATHS = ["src/counter.py", "tests/test_counter.py"]


def flatten(values, prefix=""):
    result = {}
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def receive(method):
    frame = json.loads(sys.stdin.readline())
    assert frame.get("method") == method
    return frame


def send(frame):
    print(json.dumps(frame), flush=True)


def respond(frame, result):
    send({"id": frame["id"], "result": result})


def record(value):
    """Read the versioned evidence encoding carried in the untrusted context."""
    if not isinstance(value, dict):
        return value
    if "record" in value:
        return record(value["record"])
    if "$record" in value:
        return {key: record(item) for key, item in value["fields"]}
    if "$tuple" in value:
        return [record(item) for item in value["$tuple"]]
    if "$uuid" in value:
        return value["$uuid"]
    if "$enum" in value:
        return value["value"]
    raise AssertionError("Unexpected fixture evidence shape")


respond(receive("initialize"), {"userAgent": "offline-counter/0.153.4"})
receive("initialized")
respond(
    receive("account/read"),
    {"account": {"type": "chatgpt", "email": "codex@example.invalid", "planType": "plus"}},
)
respond(
    receive("model/list"),
    {"data": [{"id": model, "supportedReasoningEfforts": [{"reasoningEffort": effort}]}]},
)
respond(receive("config/read"), {"config": {**configuration, "mcp_servers": {}}})
thread = receive("thread/start")
assert thread["params"]["environments"] == []
assert thread["params"]["allowProviderModelFallback"] is False
configured = dict(thread["params"]["config"])
assert configured == flatten(configuration)
dynamic = thread["params"]["dynamicTools"]
assert len(dynamic) == 1 and dynamic[0]["type"] == "namespace" and dynamic[0]["name"] == "forge"
advertised_tools = {tool["name"] for tool in dynamic[0]["tools"]}
respond(thread, {"thread": {"id": "counter-thread"}, "model": model})
turn = receive("turn/start")
params = turn["params"]
assert params["threadId"] == "counter-thread" and params["environments"] == []
assert params["model"] == model and params["effort"] == effort
context = params["additionalContext"]["forge_task"]
assert context["kind"] == "untrusted"
request = json.loads(context["value"])
task, evidence = request["task"], request["context"]
assert task["purpose"] == "primary"
assert task["route"]["effective"]["model"] == model
identity = {"threadId": "counter-thread", "turnId": "counter-turn"}
respond(turn, {"turn": {"id": "counter-turn"}})
call_number = 80


def call(name, arguments):
    global call_number
    wire_name = "forge_" + name.replace(".", "_").replace("-", "_")
    assert wire_name in advertised_tools
    call_number += 1
    send(
        {
            "id": call_number,
            "method": "item/tool/call",
            "params": {
                **identity,
                "callId": str(call_number),
                "namespace": "forge",
                "tool": wire_name,
                "arguments": arguments,
            },
        }
    )
    reply = json.loads(sys.stdin.readline())
    assert reply["id"] == call_number and reply["result"]["success"] is True
    receipt = json.loads(reply["result"]["contentItems"][0]["text"])
    assert receipt["status"] == "succeeded"
    return receipt


def decide():
    kinds = {
        item["properties"]["kind"]["const"]
        for item in params["outputSchema"]["properties"]["decision"]["anyOf"]
    }
    if "plan" in kinds:
        assert kinds == {"plan"}
        source = call("repository.read_file", {"path": PATHS[0]})
        assert "value + 2" in source["metadata"]["content"]
        return {
            "kind": "plan",
            "plan": {
                "summary": "Correct counter increment and cover -1",
                "assumptions": [],
                "affected_components": PATHS,
                "steps": ["Delegate the bounded repair and self-review, then accept and validate"],
                "required_checks": ["unit"],
                "risks": ["Counter regression"],
                "security_considerations": [],
                "dependency_changes": [],
                "owned_paths": PATHS,
            },
        }
    if not evidence["known_tasks"]:
        return {
            "kind": "delegate",
            "children": [
                {
                    "task_id": str(uuid4()),
                    "purpose": "routine_implementation",
                    "dependency_task_ids": [],
                    "owned_paths": PATHS,
                    "typed_acceptance": [
                        {
                            "criterion_id": "counter",
                            "description": "Increment adds one including -1",
                            "required_check_names": ["unit"],
                        }
                    ],
                    "named_checks": ["unit"],
                    "untrusted_context_refs": [],
                    "budget": {**task["budget"], "max_provider_attempts": 2, "max_repairs": 1},
                    "max_repairs": 1,
                }
            ],
            "rationale": "One approved routine worker owns the bounded change, checks and repair",
        }
    outcomes = [item for item in evidence["task_outcomes"] if item["task_id"] != task["task_id"]]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    handoff = record(outcome["recorded_handoff"])
    assert handoff["status"] == "completed" and set(handoff["changed_paths"]) == set(PATHS)
    assert [check["exit_code"] for check in handoff["check_results"]] == [1, 0]
    if outcome["acceptance"] is None:
        return {
            "kind": "accept",
            "task_id": outcome["task_id"],
            "candidate_commit": handoff["candidate_commit"],
            "candidate_tree_digest": handoff["candidate_tree_digest"],
            "evidence_receipt_ids": handoff["evidence_receipt_ids"],
            "rationale": "Accept the recovered worker handoff with failed and repaired check evidence",
        }
    selection = evidence["review_selection"]
    if selection is None:
        checkpoint = call("git.commit", {"message": "fix: correct counter increment and boundary"})
        checked = call("build.run_named_check", {"command_name": "unit"})
        assert checked["metadata"]["exit_code"] == 0
        snapshot = call("git.diff", {"scope": "snapshot"})
        return {
            "kind": "review_selection",
            "candidate_commit": checkpoint["metadata"]["new_sha"],
            "candidate_tree_digest": snapshot["metadata"]["candidate_tree_digest"],
            "review_required": False,
            "reviewer_route": None,
            "no_review_reason": "Bounded counter correction with worker repair and authoritative checks",
        }
    chosen = record(selection["decision"])
    assert chosen["review_required"] is False and chosen["no_review_reason"]
    snapshot = call("git.diff", {"scope": "snapshot"})
    assert snapshot["metadata"]["candidate_tree_digest"] == chosen["candidate_tree_digest"]
    return {
        "kind": "accept",
        "task_id": task["task_id"],
        "candidate_commit": chosen["candidate_commit"],
        "candidate_tree_digest": chosen["candidate_tree_digest"],
        "evidence_receipt_ids": [
            *(check["receipt_id"] for check in handoff["check_results"] if check["passed"]),
            snapshot["operation_id"],
        ],
        "rationale": "Accept the exact checkpoint under the recorded review decision and retain human gates",
    }


decision = decide()
send(
    {
        "method": "thread/tokenUsage/updated",
        "params": {
            **identity,
            "tokenUsage": {
                "total": {"inputTokens": 31, "outputTokens": 17, "cachedInputTokens": 0}
            },
        },
    }
)
send(
    {
        "method": "item/completed",
        "params": {
            **identity,
            "item": {
                "id": "final",
                "type": "agentMessage",
                "text": json.dumps({"decision": decision}),
            },
        },
    }
)
send(
    {
        "method": "turn/completed",
        "params": {
            "threadId": "counter-thread",
            "turn": {"id": "counter-turn", "items": [], "status": "completed", "error": None},
        },
    }
)
