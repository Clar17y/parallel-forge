"""Fail-closed decoding of execution-bound late provider observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from pydantic import TypeAdapter

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.executions import ExecutionAdmission
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.agent_results import usage_attempts_bytes
from forge.domain.agent import (
    AgentFinishStatus,
    DeveloperOutput,
    ReviewOutput,
    validate_usage_durable_metadata,
    validated_usage_attempts,
)
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.plan import PlanOutput
from forge.observability.usage import UsageRecord


@dataclass(frozen=True, slots=True)
class RetainedOutcome:
    usage: UsageRecord
    output_artifact_id: UUID


async def load_retained_outcome(
    work: UnitOfWork,
    store: ArtifactStore,
    *,
    command_id: UUID,
    delivery_attempt: int,
    admission: ExecutionAdmission,
) -> RetainedOutcome:
    """Verify known result and measured usage without publishing the old result."""
    kind = admission.kind
    prefix = {"plan": "planning", "implement": "developer", "review": "review"}.get(kind)
    if prefix is None:
        raise CommandRecoveryRequired("retained outcome kind is invalid")

    async def read(producer: str) -> tuple[ArtifactDescriptor, dict[str, object]]:
        artifacts = await work.artifacts.get_by_producer(
            run_id=admission.run_id,
            producer_type=producer,
            producer_id=admission.agent_execution_id,
        )
        if len(artifacts) != 1 or artifacts[0].artifact_id is None:
            raise CommandRecoveryRequired("retained outcome evidence is unavailable or ambiguous")
        descriptor = artifacts[0]
        payload = await _payload(store, descriptor)
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("command_id") != str(command_id)
            or payload.get("execution_id") != str(admission.agent_execution_id)
        ):
            raise CommandRecoveryRequired("retained outcome binding differs")
        return descriptor, payload

    try:
        output_descriptor, output_payload = await read(f"{prefix}_late_result")
        if kind == "review":
            usage_payload = output_payload
            if set(output_payload) != {
                "schema_version",
                "command_id",
                "execution_id",
                "head_sha",
                "finish_status",
                "output",
                "usage",
                "attempts",
            }:
                raise ValueError("review receipt fields differ")
            raw_finish = output_payload["finish_status"]
            if not isinstance(raw_finish, str):
                raise TypeError("retained finish status is invalid")
            finish = AgentFinishStatus(raw_finish)
            head = output_payload["head_sha"]
            if (
                not isinstance(head, str)
                or len(head) != 40
                or any(c not in "0123456789abcdef" for c in head)
            ):
                raise ValueError("review head is invalid")
            output = output_payload["output"]
            if output is not None:
                ReviewOutput.model_validate(output)
            elif finish is AgentFinishStatus.SUCCEEDED:
                raise ValueError("successful review has no result")
        else:
            _, usage_payload = await read(f"{prefix}_late_usage")
            fields = {"schema_version", "command_id", "execution_id", "usage", "attempts"}
            if kind == "plan":
                fields.add("delivery_attempt")
                if (
                    type(usage_payload.get("delivery_attempt")) is not int
                    or usage_payload["delivery_attempt"] != delivery_attempt
                ):
                    raise ValueError("planning delivery differs")
            if set(usage_payload) != fields or set(output_payload) != {
                "schema_version",
                "command_id",
                "execution_id",
                "output_digest",
                "output",
            }:
                raise ValueError("retained receipt fields differ")
            output = output_payload["output"]
            if hashlib.sha256(_json(output)).hexdigest() != output_payload["output_digest"]:
                raise ValueError("retained result digest differs")
            if isinstance(output, dict) and set(output) == {"finish_status", "reason"}:
                if AgentFinishStatus(
                    output["finish_status"]
                ) is AgentFinishStatus.SUCCEEDED or not isinstance(output["reason"], str):
                    raise ValueError("retained failure is invalid")
            elif kind == "plan":
                PlanOutput.model_validate(output)
            else:
                DeveloperOutput.model_validate(output)
        raw_usage = usage_payload["usage"]
        if kind != "plan":
            if not isinstance(raw_usage, list) or len(raw_usage) != 1:
                raise ValueError("retained aggregate is invalid")
            raw_usage = raw_usage[0]
        usage = _usage(raw_usage, admission)
        raw_attempts = usage_payload["attempts"]
        if not isinstance(raw_attempts, list):
            raise TypeError("retained attempts are invalid")
        attempts = tuple(_usage(raw, admission) for raw in raw_attempts)
        validated_usage_attempts(usage, attempts)
        if output_descriptor.artifact_id is None:
            raise ValueError("retained output is unbound")
        return RetainedOutcome(usage, output_descriptor.artifact_id)
    except CommandRecoveryRequired:
        raise
    except Exception:  # noqa: BLE001 - malformed or unavailable evidence grants no recovery authority
        raise CommandRecoveryRequired("retained outcome evidence is invalid") from None


def _usage(raw: object, admission: ExecutionAdmission) -> UsageRecord:
    usage = TypeAdapter(UsageRecord).validate_python(raw)
    validate_usage_durable_metadata(usage)
    if (
        json.loads(usage_attempts_bytes((usage,)))["attempts"][0] != raw
        or usage.provider != admission.provider
        or usage.model != admission.model
        or usage.prompt_version != admission.instruction_version
        or usage.run_id not in {None, admission.run_id}
        or usage.agent_execution_id not in {None, admission.agent_execution_id}
    ):
        raise ValueError("retained usage identity differs")
    return usage


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


async def _payload(store: ArtifactStore, descriptor: ArtifactDescriptor) -> dict[str, object]:
    if (
        descriptor.media_type != "application/json"
        or descriptor.truncated
        or descriptor.schema_version != 1
    ):
        raise CommandRecoveryRequired("retained outcome artifact is invalid")
    wire = await store.open_bytes(descriptor.digest)
    payload = json.loads(wire)
    if (
        not isinstance(payload, dict)
        or descriptor.byte_count != len(wire)
        or hashlib.sha256(wire).hexdigest() != descriptor.digest
        or wire != _json(payload)
    ):
        raise CommandRecoveryRequired("retained outcome artifact differs")
    return payload


__all__ = ["RetainedOutcome", "load_retained_outcome"]
