from __future__ import annotations

import json
from pathlib import Path

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.contracts import EvaluationCaseContract


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "failed",
        "version",
        "receipt",
        "truncated",
        "duplicate",
        "nonboolean",
        "boolean_exit",
        "boolean_receipt_version",
    ],
)
async def test_only_current_successful_receipt_report_scores(
    tmp_path: Path, damage: str | None
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    case = EvaluationCaseContract(
        fixture_version="v1",
        case_key="developer/example",
        task="check",
        role=AgentRole.DEVELOPER,
        required_checks=("check",),
        required_tests=("test_app.py",),
        required_assertions=("greeting",),
    )
    report = {
        "report_version": 1,
        "fixture_version": "v1",
        "case_key": case.case_key,
        "command_name": "check",
        "tests": {"test_app.py": True},
        "assertions": {"greeting": True},
    }
    if damage == "version":
        report["fixture_version"] = "other"
    if damage == "nonboolean":
        report["tests"] = {"test_app.py": 1}
    line = "FORGE_EVAL_REPORT_V1:" + json.dumps(report)
    output = {
        "stream": "stdout",
        "truncated": damage == "truncated",
        "text": line + ("\n" + line if damage == "duplicate" else ""),
    }
    stdout = await store.put_bytes(json.dumps(output).encode(), media_type="application/json")
    receipt = {
        "receipt_version": 1,
        "tool_call_id": "call",
        "stdout_digest": stdout.digest,
        "caller_cancelled": False,
        "request_payload": {"command_name": "check"},
    }
    if damage == "receipt":
        receipt["tool_call_id"] = "other"
    if damage == "boolean_receipt_version":
        receipt["receipt_version"] = True
    saved = await store.put_bytes(json.dumps(receipt).encode(), media_type="application/json")
    result = {
        "status": "failed" if damage == "failed" else "succeeded",
        "tool_call_id": "call",
        "artifact_digests": [saved.digest],
        "metadata": {
            "receipt_digest": saved.digest,
            "stdout_digest": stdout.digest,
            "exit_code": False if damage == "boolean_exit" else 0,
            "timed_out": False,
            "caller_cancelled": False,
        },
    }
    actual = await read_check_evidence(case, store, result, command_name="check")
    assert actual == (({"test_app.py": True}, {"greeting": True}) if damage is None else None)
