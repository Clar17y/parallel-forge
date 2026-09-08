"""A historical branch removal needs an exact successful operation receipt."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.branch_removal import branch_removal_recorded
from forge.domain.event import RunEvent
from forge.domain.operation import OperationIntent, OperationStatus
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot
from forge.tools.branch_removal import branch_removal_request


@pytest.mark.parametrize(
    "alteration", [None, "actor", "source", "outcome", "foreign_run", "request"]
)
def test_branch_removal_record_requires_bound_receipt(tmp_path, alteration):
    run = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        policy_version=1,
        branch_name="forge/task",
        base_sha="a" * 40,
    )
    identity = WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, False)
    request = branch_removal_request(
        ManagedWorktree(identity=identity, path=tmp_path, base_sha=run.base_sha),
        policy_version=1,
        source_command_id=uuid4(),
        expected_head="b" * 40,
    )
    outcome = {
        "source_command_id": request.request_payload["source_command_id"],
        "request_digest": request.request_digest,
        "branch": run.branch_name,
        "expected_head": "b" * 40,
        "removed": True,
    }
    intent = OperationIntent(
        run_id=run.id,
        kind=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
        status=OperationStatus.SUCCEEDED,
        completed_at=datetime.now(UTC),
        outcome=outcome,
        outcome_schema_version=1,
    )
    event = RunEvent(
        run_id=run.id,
        event_type="resource.branch_removed",
        run_version=run.version,
        actor_class="worker",
        payload={
            **{k: v for k, v in outcome.items() if k != "removed"},
            "operation_intent_id": str(intent.id),
        },
    )
    if alteration == "actor":
        event = replace(event, actor_class="operator", actor_id=uuid4())
    elif alteration == "source":
        event = replace(event, payload={**event.payload, "source_command_id": str(uuid4())})
    elif alteration == "outcome":
        intent = replace(intent, outcome={**outcome, "removed": 1})
    elif alteration == "foreign_run":
        intent = replace(intent, run_id=uuid4())
    elif alteration == "request":
        intent = replace(
            intent, request_payload={**intent.request_payload, "expected_head": "c" * 40}
        )
    assert branch_removal_recorded(run, event, intent) is (alteration is None)
