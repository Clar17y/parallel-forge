from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from forge.application.ports.artifacts import ArtifactDescriptor
from forge.application.ports.tools import ToolCallRecord
from forge.application.services.tools import (
    _WRITE_RESULT_ARTIFACT_MAX_BYTES,
    ToolInvocationError,
    _write_record_matches_request,
    _write_replay_result,
    _write_request_digest,
    _write_tool_call_id,
)
from forge.domain.actor import AgentRole
from forge.domain.artifact import canonical_storage_pointer
from forge.domain.tool import ToolAuthorizationContext, ToolCallStatus, ToolName, ToolRequest


def test_write_call_id_is_the_trusted_invocation_id() -> None:
    invocation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    assert _write_tool_call_id(invocation_id) == invocation_id


def test_same_id_rejects_changed_raw_secret_even_when_redacted_evidence_matches() -> None:
    invocation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        worktree_id="forge-test",
        policy_version=1,
        agent_execution_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        step_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        invocation_id=invocation_id,
    )
    original = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "secret.txt", "content": "token=first-secret"},
    )
    changed = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "secret.txt", "content": "token=second-secret"},
    )
    normalized = {
        "path": "secret.txt",
        "content_digest": "a" * 64,
        "content_byte_count": 18,
    }
    record = ToolCallRecord(
        id=invocation_id,
        run_id=context.run_id,
        agent_execution_id=context.agent_execution_id,
        tool_name=ToolName.REPOSITORY_WRITE_FILE,
        normalized_arguments=normalized,
        authorized=True,
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(UTC),
        step_id=context.step_id,
        role=context.role,
        policy_version=context.policy_version,
        correlation_id=invocation_id,
        operation_intent_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        request_digest=_write_request_digest(original),
        resource_id=context.worktree_id,
        invocation_schema_version=1,
    )

    assert not _write_record_matches_request(
        record,
        context,
        changed.name,
        normalized,
        _write_request_digest(changed),
    )


def test_legacy_record_cannot_be_reused_as_a_schema_one_invocation() -> None:
    invocation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        worktree_id="forge-test",
        policy_version=1,
        agent_execution_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        step_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        invocation_id=invocation_id,
    )
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "same.txt", "content": "same"},
    )
    record = ToolCallRecord(
        id=invocation_id,
        run_id=context.run_id,
        agent_execution_id=context.agent_execution_id,
        tool_name=request.name,
        normalized_arguments={},
        authorized=True,
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(UTC),
        step_id=context.step_id,
        role=context.role,
        policy_version=context.policy_version,
        correlation_id=invocation_id,
        operation_intent_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
    )

    assert not _write_record_matches_request(
        record, context, request.name, {}, _write_request_digest(request)
    )


class _ReplayArtifacts:
    def __init__(self, descriptor: ArtifactDescriptor) -> None:
        self.descriptor = descriptor

    async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor:
        assert digest == self.descriptor.digest
        assert run_id == self.descriptor.run_id
        return self.descriptor


class _ReplayStore:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def verify(self, digest: str) -> bool:
        return hashlib.sha256(self.data).hexdigest() == digest

    async def open_bytes(self, digest: str) -> bytes:
        assert digest == hashlib.sha256(self.data).hexdigest()
        return self.data


class _UnreadableReplayStore(_ReplayStore):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.verify_calls = 0
        self.open_calls = 0

    async def verify(self, digest: str) -> bool:
        self.verify_calls += 1
        return await super().verify(digest)

    async def open_bytes(self, digest: str) -> bytes:
        self.open_calls += 1
        return await super().open_bytes(digest)


def _replay_inputs(
    *, extra_json: str = ""
) -> tuple[
    ToolCallRecord, ToolAuthorizationContext, dict[str, object], str, _ReplayArtifacts, _ReplayStore
]:
    call_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    operation_id = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        worktree_id="forge-test",
        policy_version=1,
        agent_execution_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        step_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        invocation_id=call_id,
    )
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "same.txt", "content": "same"},
    )
    normalized = {
        "path": "same.txt",
        "content_digest": hashlib.sha256(b"same").hexdigest(),
        "content_byte_count": 4,
    }
    request_digest = _write_request_digest(request)
    result = {
        "path": "same.txt",
        "output_digest": normalized["content_digest"],
        "byte_count": 4,
        "created": True,
        "previous_digest": None,
        "reconciled": False,
    }
    artifact = {
        "operation_intent_id": str(operation_id),
        "producer_id": str(call_id),
        "request_digest": request_digest,
        "resource_id": context.worktree_id,
        "result": result,
        "schema_version": 1,
        "status": ToolCallStatus.SUCCEEDED.value,
        "tool_name": ToolName.REPOSITORY_WRITE_FILE.value,
    }
    canonical_data = json.dumps(artifact, separators=(",", ":"), sort_keys=True).encode()
    data = canonical_data[:-1] + extra_json.encode() + b"}" if extra_json else canonical_data
    digest = hashlib.sha256(data).hexdigest()
    record_metadata = dict(result)
    record_metadata.update(
        request_digest=request_digest,
        resource_id=context.worktree_id,
        invocation_schema_version=1,
        result_status=ToolCallStatus.SUCCEEDED.value,
        authorized=True,
        started_at=datetime.now(UTC).isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        artifact_digests=[digest],
    )
    record = ToolCallRecord(
        id=call_id,
        run_id=context.run_id,
        agent_execution_id=context.agent_execution_id,
        tool_name=ToolName.REPOSITORY_WRITE_FILE,
        normalized_arguments=normalized,
        authorized=True,
        status=ToolCallStatus.SUCCEEDED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        result_metadata=record_metadata,
        step_id=context.step_id,
        role=context.role,
        policy_version=context.policy_version,
        artifact_digests=(digest,),
        correlation_id=call_id,
        operation_intent_id=operation_id,
        request_digest=request_digest,
        resource_id=context.worktree_id,
        invocation_schema_version=1,
        result_metadata_schema_version=1,
    )
    descriptor = ArtifactDescriptor(
        digest=digest,
        media_type="application/json",
        byte_count=len(data),
        storage_path=Path(canonical_storage_pointer(digest)),
        producer_type="controlled_tool",
        producer_id=call_id,
        run_id=context.run_id,
        metadata={
            "operation_intent_id": str(operation_id),
            "producer_id": str(call_id),
            "request_digest": request_digest,
            "resource_id": context.worktree_id,
            "result_schema_version": 1,
            "tool_name": ToolName.REPOSITORY_WRITE_FILE.value,
            "invocation_schema_version": 1,
        },
    )
    return (
        record,
        context,
        normalized,
        request_digest,
        _ReplayArtifacts(descriptor),
        _ReplayStore(data),
    )


@pytest.mark.parametrize(
    "suffix",
    (',"unexpected":true', ',"status":"succeeded"'),
)
async def test_replay_rejects_noncanonical_artifact_bytes_with_matching_digest(
    suffix: str,
) -> None:
    record, context, normalized, request_digest, artifacts, store = _replay_inputs(
        extra_json=suffix
    )

    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            record,
            context,
            normalized,
            request_digest,
            artifacts=artifacts,  # type: ignore[arg-type]
            artifact_store=store,
        )


async def test_replay_accepts_the_canonical_valid_fixture() -> None:
    record, context, normalized, request_digest, artifacts, store = _replay_inputs()

    result = await _write_replay_result(
        record,
        context,
        normalized,
        request_digest,
        artifacts=artifacts,  # type: ignore[arg-type]
        artifact_store=store,
    )

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.tool_call_id == record.id


async def test_replay_rejects_oversized_descriptor_before_reading_blob() -> None:
    record, context, normalized, request_digest, artifacts, store = _replay_inputs()
    unreadable_store = _UnreadableReplayStore(store.data)
    artifacts.descriptor = replace(
        artifacts.descriptor,
        byte_count=_WRITE_RESULT_ARTIFACT_MAX_BYTES + 1,
        original_byte_count=_WRITE_RESULT_ARTIFACT_MAX_BYTES + 1,
    )

    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            record,
            context,
            normalized,
            request_digest,
            artifacts=artifacts,  # type: ignore[arg-type]
            artifact_store=unreadable_store,
        )

    assert unreadable_store.verify_calls == 0
    assert unreadable_store.open_calls == 0


@pytest.mark.parametrize("field", ("result_schema_version", "invocation_schema_version"))
@pytest.mark.parametrize("value", (True, 1.0, None))
async def test_replay_rejects_schema_metadata_aliases_or_missing_values(
    field: str,
    value: object | None,
) -> None:
    record, context, normalized, request_digest, artifacts, store = _replay_inputs()
    metadata = dict(artifacts.descriptor.metadata)
    if value is None:
        metadata.pop(field)
    else:
        metadata[field] = value
    artifacts.descriptor = replace(artifacts.descriptor, metadata=metadata)

    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            record,
            context,
            normalized,
            request_digest,
            artifacts=artifacts,  # type: ignore[arg-type]
            artifact_store=store,
        )


@pytest.mark.parametrize("variant", ("schema", "truncated"))
async def test_replay_rejects_noncanonical_descriptor_shape(variant: str) -> None:
    record, context, normalized, request_digest, artifacts, store = _replay_inputs()
    if variant == "schema":
        artifacts.descriptor = replace(artifacts.descriptor, schema_version=2)
    else:
        artifacts.descriptor = replace(
            artifacts.descriptor,
            truncated=True,
            original_byte_count=artifacts.descriptor.byte_count + 1,
            truncation_policy="head_tail",
        )

    with pytest.raises(ToolInvocationError):
        await _write_replay_result(
            record,
            context,
            normalized,
            request_digest,
            artifacts=artifacts,  # type: ignore[arg-type]
            artifact_store=store,
        )
