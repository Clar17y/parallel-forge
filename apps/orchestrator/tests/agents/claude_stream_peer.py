import json
import sys
import time

scenario = sys.argv[1]


def recv():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("unexpected stdin EOF")
    return json.loads(line)


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


init = recv()
assert init["type"] == "control_request" and init["request_id"] == "forge_initialize"
assert init["request"] == {"subtype": "initialize", "hooks": None, "skills": []}
send({"type":"control_request","request_id":"mcp-init","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":"init","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"fake-official","version":"1"}}}}})
r = recv()
assert r["response"]["request_id"] == "mcp-init"
send({"type":"control_request","request_id":"mcp-list","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}}})
r = recv()
assert [t["name"] for t in r["response"]["response"]["mcp_response"]["result"]["tools"]] == ["repository.read_file"]
if scenario == "tool_during_initialize":
    send({"type":"control_request","request_id":"early-call","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"repository.read_file","arguments":{"path":"README.md"}}}}})
    raise SystemExit(0)
if scenario == "wrong_initialize":
    send({"type":"control_response","response":{"subtype":"success","request_id":"foreign","response":{}}})
    raise SystemExit(0)
if scenario == "duplicate_request":
    send({"type":"control_request","request_id":"mcp-list","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}}})
    raise SystemExit(0)
send({"type":"control_response","response":{"subtype":"success","request_id":"forge_initialize","response":{"commands":[],"output_style":"default"}}})
user = recv()
assert user["type"] == "user" and user["session_id"] == sys.argv[sys.argv.index("--session-id") + 1]
if scenario == "eof":
    raise SystemExit(0)
if scenario == "malformed":
    print("{", flush=True)
    raise SystemExit(0)
if scenario == "wrong_session":
    send({"type":"result","subtype":"success","structured_output":{"decision":{}},"session_id":"foreign","is_error":False})
    raise SystemExit(0)
send({"type":"system","subtype":"init","session_id":user["session_id"]})
if scenario == "cancel":
    send({"type":"assistant","session_id":user["session_id"],"message":{"id":"msg-usage","role":"assistant","content":[],"usage":{"input_tokens":13,"output_tokens":5}}})
if scenario == "usage_repeat":
    for _ in range(2):
        send({"type":"assistant","session_id":user["session_id"],"message":{"id":"msg-same","role":"assistant","content":[],"usage":{"input_tokens":13,"output_tokens":5}}})
if scenario == "usage_sum":
    send({"type":"assistant","session_id":user["session_id"],"message":{"id":"msg-one","role":"assistant","content":[],"usage":{"input_tokens":6,"output_tokens":2}}})
    send({"type":"assistant","session_id":user["session_id"],"message":{"id":"msg-two","role":"assistant","content":[],"usage":{"input_tokens":7,"output_tokens":3}}})
send({"type":"control_request","request_id":"mcp-call","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"repository.read_file","arguments":{"path":"README.md"}}}}})
r = recv()
m = r["response"]["response"]["mcp_response"]
assert m["id"] == 7 and m["result"]["isError"] is False
assert json.loads(m["result"]["content"][0]["text"]) == {"evidence":{"lines":[1,2],"path":"README.md"},"status":"succeeded"}
if scenario == "cancel":
    time.sleep(30)
if scenario == "typed_ids":
    send({"type":"control_request","request_id":"mcp-call-string","request":{"subtype":"mcp_message","server_name":"forge","message":{"jsonrpc":"2.0","id":"7","method":"tools/call","params":{"name":"repository.read_file","arguments":{"path":"OTHER.md"}}}}})
    r = recv()
    assert r["response"]["response"]["mcp_response"]["id"] == "7"
if scenario in {"generation_error", "quota_status", "quota_exhausted", "authentication_status"}:
    status = {"generation_error": None, "quota_status": 429, "quota_exhausted": 429, "authentication_status": 401}[scenario]
    send({"type": "result", "subtype": "success", "is_error": True,
          "session_id": user["session_id"], "errors": ["You've hit your weekly limit" if scenario == "quota_exhausted" else "unable to generate output"],
          "api_error_status": status})
    raise SystemExit(0)
decision = {"kind":"handoff","status":"blocked","summary":"done","candidate_commit":None,"candidate_tree_digest":"","changed_paths":[],"check_results":[],"evidence_receipt_ids":[],"residual_concerns":[],"scope_request_paths":[]}
send({"type":"assistant","session_id":user["session_id"],"message":{"role":"assistant","content":[]}})
send({"type":"result","subtype":"success","structured_output":{"decision":decision},"session_id":user["session_id"],"duration_ms":4,"duration_api_ms":3,"num_turns":1,"usage":{"input_tokens":13,"output_tokens":5},"modelUsage":{},"total_cost_usd":0.0,"is_error":False,"stop_reason":"end_turn","errors":[],"uuid":"00000000-0000-0000-0000-000000000001","result":"done","permission_denials":[],"api_error_status":None,"terminal_reason":None,"origin":{"kind":"human"}})
