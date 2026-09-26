"""Frozen grader credit understands versioned subscription command receipts."""

import json
from types import SimpleNamespace

import pytest
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import get_counter_service_case


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize(
    "change", [None, "drift", "unknown", "authority", "metadata", "truncated", "duplicate"]
)
async def test_grader_requires_stable_subscription_receipt_and_complete_output(version, change):
    case = get_counter_service_case()
    report = {
        "report_version": 1,
        "fixture_version": case.fixture_version,
        "case_key": case.case_key,
        "command_name": "unit",
        "tests": dict.fromkeys(case.required_tests, True),
        "assertions": dict.fromkeys(case.required_assertions, True),
    }
    receipt = {
        "receipt_version": version,
        "tool_call_id": "check",
        "stdout_digest": "stdout",
        "caller_cancelled": False,
        "request_payload": {"command_name": "unit"},
    }
    metadata = {
        "receipt_digest": "receipt",
        "stdout_digest": "stdout",
        "exit_code": 0,
        "timed_out": False,
        "caller_cancelled": False,
    }
    if version > 1:
        receipt["request_payload"]["authority_schema_version"] = 2
        for key in ("candidate_tree_digest_before", "candidate_tree_digest_after"):
            receipt[key] = metadata[key] = "a" * 64
    if change == "drift":
        receipt["candidate_tree_digest_after"] = "b" * 64
    elif change == "unknown":
        receipt["candidate_tree_digest_before"] = None
    elif change == "authority":
        receipt["request_payload"]["authority_schema_version"] = True
    elif change == "metadata":
        metadata["candidate_tree_digest_before"] = "b" * 64
    line = "FORGE_EVAL_REPORT_V1:" + json.dumps(report)
    stdout = {
        "stream": "stdout",
        "truncated": change == "truncated",
        "text": line + ("\n" + line if change == "duplicate" else ""),
    }
    blobs = {"receipt": json.dumps(receipt).encode(), "stdout": json.dumps(stdout).encode()}

    async def read(digest, **kwargs):
        return blobs[digest]

    result = await read_check_evidence(
        case,
        SimpleNamespace(open_bytes=read),
        {
            "status": "succeeded",
            "tool_call_id": "check",
            "metadata": metadata,
            "artifact_digests": ["receipt"],
        },
        command_name="unit",
    )
    if change in {"truncated", "duplicate"} or (version > 1 and change is not None):
        assert result is None
    else:
        assert result == (report["tests"], report["assertions"])
