"""Direct-run domain: result parsing, prompt and per-client argv (Slice A)."""

from types import SimpleNamespace

from forge.domain.direct import (
    build_direct_launch,
    build_direct_prompt,
    parse_direct_result,
)


def _installation(client: str):
    return SimpleNamespace(
        client=client, executable=f"/opt/{client}/bin", model="model-1", effort="high"
    )


def test_parse_fenced_result() -> None:
    result = parse_direct_result(
        'working\n```json\n{"status":"needs_decision","summary":"s","question":"q?",'
        '"options":["a","b"]}\n```\n'
    )
    assert result.status == "needs_decision"
    assert result.question == "q?"
    assert result.options == ("a", "b")
    assert result.degraded is False


def test_parse_result_inside_antigravity_result_event() -> None:
    import json

    response = (
        'Created it.\n\n```json\n{"status": "needs_decision", "summary": "s", '
        '"question": "pick?", "options": ["x", "y"]}\n```\n'
    )
    line = json.dumps(
        {"event": "result", "result": {"status": "SUCCESS", "response": response}}
    )
    result = parse_direct_result('{"event":"step_update"}\n' + line)
    assert result.status == "needs_decision"
    assert result.question == "pick?"
    assert result.options == ("x", "y")
    assert result.degraded is False


def test_parse_unparseable_is_degraded_done() -> None:
    result = parse_direct_result("just some prose without json")
    assert result.status == "done"
    assert result.degraded is True
    assert "just some prose" in result.summary


def test_prompt_bounds_and_contract() -> None:
    prompt = build_direct_prompt("Do the thing", has_agents_md=True)
    assert "Do the thing" in prompt
    assert "AGENTS.md" in prompt
    assert "needs_decision" in prompt
    assert "work only inside this worktree" in prompt.lower()


def test_argv_per_client(tmp_path) -> None:
    wt = tmp_path / "wt"
    common = {
        "worktree": wt,
        "prompt": "PROMPT",
        "prompt_file": tmp_path / "p.txt",
        "schema_file": tmp_path / "s.json",
        "final_file": tmp_path / "f.txt",
        "max_model_steps": 2000,
    }
    muse = build_direct_launch(_installation("muse_code"), **common)
    assert muse.argv[:2] == ("/opt/muse_code/bin", "exec")
    assert "--yolo" in muse.argv and "--max-model-steps" in muse.argv
    assert str(tmp_path / "s.json") in muse.argv
    assert muse.stdin_data is None

    agy = build_direct_launch(_installation("antigravity_cli"), **common)
    assert "--dangerously-skip-permissions" in agy.argv
    assert "--print" not in agy.argv
    assert agy.stdin_data is not None and b"PROMPT" in agy.stdin_data
    assert b'"event": "user"' in agy.stdin_data or b'"event":"user"' in agy.stdin_data

    codex = build_direct_launch(_installation("codex_app_server"), **common)
    assert codex.argv[1:3] == ("exec", "--json")
    assert "-C" in codex.argv and str(wt) in codex.argv
    assert codex.argv[-1] == "-"
    assert codex.stdin_data == b"PROMPT"

    claude = build_direct_launch(_installation("claude_code"), **common)
    assert claude.argv[1] == "-p"
    assert "bypassPermissions" in claude.argv
    assert claude.stdin_data == b"PROMPT"
