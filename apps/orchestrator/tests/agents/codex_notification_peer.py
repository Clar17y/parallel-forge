"""Offline app-server peer following the pinned official notification ordering."""

import json
import sys
import tomllib

scenario, model, effort, reset_text = sys.argv[1:5]
reset_at = int(reset_text)
configuration_args = sys.argv[5:]
assert len(configuration_args) % 2 == 0 and set(configuration_args[::2]) == {"-c"}
configuration = tomllib.loads("\n".join(configuration_args[1::2]))
assert configuration["model_provider"] == "openai"
assert configuration["forced_login_method"] == "chatgpt"
assert configuration["model"] == model and configuration["model_reasoning_effort"] == effort
assert configuration["web_search"] == "disabled"
assert configuration["notify"] == [] and configuration["project_doc_max_bytes"] == 0


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


def limits(**changes):
    snapshot = {
        "limitId": "another-pool" if scenario == "unrelated_pool" else "codex",
        "primary": {"usedPercent": 100, "resetsAt": reset_at, "windowDurationMins": 300},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
    }
    snapshot.update(changes)
    send({"method": "account/rateLimits/updated", "params": {"rateLimits": snapshot}})


respond(receive("initialize"), {"userAgent": "offline-fake/0.153.4"})
receive("initialized")
account = receive("account/read")
limits()
respond(
    account,
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
thread_configuration = dict(thread["params"]["config"])
assert thread_configuration == flatten(configuration)
respond(thread, {"thread": {"id": "thread-actual"}, "model": model})
turn = receive("turn/start")
assert turn["params"]["model"] == model and turn["params"]["effort"] == effort
assert turn["params"]["threadId"] == "thread-actual" and turn["params"]["environments"] == []
identity = {"threadId": "thread-actual", "turnId": "turn-actual"}
code = (
    scenario
    if scenario
    in {"rateLimitExceeded", "unauthorized", "serverOverloaded", "sessionBudgetExceeded"}
    else "usageLimitExceeded"
)
error = {"message": "Provider terminal error", "codexErrorInfo": code}
notice = {
    "method": "error",
    "params": {**identity, "error": error, "willRetry": scenario == "retry"},
}
if scenario in {"before_ack", "wrong_ack"}:
    send(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-actual", "turn": {"id": "turn-actual"}},
        }
    )
    send(notice)
respond(turn, {"turn": {"id": "wrong-turn" if scenario == "wrong_ack" else "turn-actual"}})

if scenario in {"partial_tool", "plan"}:
    send(
        {
            "id": 80,
            "method": "item/tool/call",
            "params": {
                **identity,
                "callId": "read-once",
                "namespace": "forge",
                "tool": "forge_repository_read_file",
                "arguments": {"path": "README.md"},
            },
        }
    )
    receipt = json.loads(sys.stdin.readline())
    assert receipt["id"] == 80 and receipt["result"]["success"] is True

# Match handle_token_count_event: token counters, then sparse account telemetry.
send(
    {
        "method": "thread/tokenUsage/updated",
        "params": {
            **identity,
            "tokenUsage": {"total": {"inputTokens": 13, "outputTokens": 5, "cachedInputTokens": 2}},
        },
    }
)
limits(primary=None, credits=None)
if scenario == "global_request":
    send({"id": 81, "method": "account/rateLimits/updated", "params": {"rateLimits": {}}})
elif scenario == "malformed_global":
    send({"method": "account/rateLimits/updated", "params": {"rateLimits": None}})
else:
    if scenario == "foreign_error":
        notice["params"]["turnId"] = "old-turn"
    if scenario == "foreign_thread":
        notice["params"]["threadId"] = "another-thread"
    if scenario == "error_request":
        notice["id"] = 81
    if scenario == "missing_retry":
        notice["params"].pop("willRetry")
    if scenario not in {"global_success", "before_ack", "plan"}:
        send(notice)
    if scenario == "eof":
        raise SystemExit(0)
    if scenario == "hang":
        sys.stdin.readline()
        raise SystemExit(0)
    if scenario == "tool_after_error":
        send(
            {
                "id": 82,
                "method": "item/tool/call",
                "params": {
                    **identity,
                    "callId": "too-late",
                    "namespace": "forge",
                    "tool": "forge_repository_read_file",
                    "arguments": {"path": "README.md"},
                },
            }
        )
    success = scenario in {"retry", "global_success", "contradiction", "plan"}
    if success:
        output = {
            "decision": {
                "kind": "handoff",
                "status": "blocked",
                "summary": "offline result",
                "candidate_commit": None,
                "candidate_tree_digest": "",
                "changed_paths": [],
                "check_results": [],
                "evidence_receipt_ids": [],
                "residual_concerns": [],
                "scope_request_paths": [],
            }
        }
        if scenario == "plan":
            output = {
                "decision": {
                    "kind": "plan",
                    "plan": {
                        "summary": "One bounded change informed by the controlled read",
                        "assumptions": [],
                        "affected_components": ["apps"],
                        "steps": ["Implement and validate"],
                        "required_checks": ["unit"],
                        "risks": ["Regression"],
                        "security_considerations": [],
                        "dependency_changes": [],
                        "owned_paths": ["apps"],
                    },
                }
            }
        send(
            {
                "method": "item/completed",
                "params": {
                    **identity,
                    "item": {"id": "final", "type": "agentMessage", "text": json.dumps(output)},
                },
            }
        )
    status = "interrupted" if scenario == "interrupted" else "completed" if success else "failed"
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-actual",
                "turn": {
                    "id": "turn-actual",
                    "items": [],
                    "status": status,
                    "error": None if success else error,
                },
            },
        }
    )
