"""Decode fixture reports only from a successful controlled command receipt."""

from __future__ import annotations

import json
from collections.abc import Mapping

from forge.application.ports.artifacts import ArtifactStore
from forge.evaluations.contracts import EvaluationCaseContract

REPORT_PREFIX = "FORGE_EVAL_REPORT_V1:"
_MAX_BYTES = 262_144


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate report key")
        result[key] = value
    return result


def _decode(data: bytes | str) -> dict[str, object]:
    result = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise TypeError("evidence must be an object")
    return result


async def read_check_evidence(
    case: EvaluationCaseContract,
    store: ArtifactStore,
    result: Mapping[str, object],
    *,
    command_name: str,
) -> tuple[dict[str, bool], dict[str, bool]] | None:
    """Missing, malformed, truncated or mismatched evidence earns no test credit.

    Reports are command output, not model output. The content-addressed receipt
    binds that output to the controlled invocation, while the report identifies
    the exact versioned fixture and declared check. Ordinary logs may surround
    one report; duplicate reports are ambiguous and rejected.
    """
    metadata = result.get("metadata")
    if (
        result.get("status") != "succeeded"
        or command_name not in case.required_checks
        or not isinstance(metadata, Mapping)
        or type(metadata.get("exit_code")) is not int
        or metadata.get("exit_code") != 0
        or metadata.get("timed_out") is not False
        or metadata.get("caller_cancelled") is not False
    ):
        return None
    receipt_digest = metadata.get("receipt_digest")
    stdout_digest = metadata.get("stdout_digest")
    artifacts = result.get("artifact_digests")
    if (
        not isinstance(receipt_digest, str)
        or not isinstance(stdout_digest, str)
        or not isinstance(artifacts, (list, tuple))
        or receipt_digest not in artifacts
    ):
        return None
    try:
        receipt = _decode(await store.open_bytes(receipt_digest, max_bytes=_MAX_BYTES))
        request = receipt.get("request_payload")
        if (
            type(receipt.get("receipt_version")) is not int
            or receipt.get("receipt_version") != 1
            or not result.get("tool_call_id")
            or receipt.get("tool_call_id") != result.get("tool_call_id")
            or receipt.get("stdout_digest") != stdout_digest
            or receipt.get("caller_cancelled") is not False
            or not isinstance(request, dict)
            or request.get("command_name") != command_name
        ):
            return None
        stdout = _decode(await store.open_bytes(stdout_digest, max_bytes=_MAX_BYTES))
        text = stdout.get("text")
        if (
            stdout.get("truncated") is not False
            or stdout.get("stream") != "stdout"
            or not isinstance(text, str)
        ):
            return None
        reports = [
            line[len(REPORT_PREFIX) :]
            for line in text.splitlines()
            if line.startswith(REPORT_PREFIX)
        ]
        if len(reports) != 1:
            return None
        report = _decode(reports[0])
        if (
            set(report)
            != {
                "report_version",
                "fixture_version",
                "case_key",
                "command_name",
                "tests",
                "assertions",
            }
            or type(report["report_version"]) is not int
            or report["report_version"] != 1
            or report["fixture_version"] != case.fixture_version
            or report["case_key"] != case.case_key
            or report["command_name"] != command_name
        ):
            return None
        scores: list[dict[str, bool]] = []
        for field, allowed in (
            ("tests", case.required_tests),
            ("assertions", case.required_assertions),
        ):
            values = report[field]
            if (
                not isinstance(values, dict)
                or not set(values).issubset(allowed)
                or any(type(value) is not bool for value in values.values())
            ):
                return None
            scores.append({key: bool(value) for key, value in values.items()})
        return scores[0], scores[1]
    except Exception:  # noqa: BLE001 - corrupt or unavailable artifacts cannot award credit
        return None
