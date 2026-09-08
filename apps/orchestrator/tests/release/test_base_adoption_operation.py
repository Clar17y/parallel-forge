from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.operation import OperationStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.release.base_adoption import BaseAdoptionOperation
from forge.release.controller import ReleaseReconciliationRequired

from apps.orchestrator.tests.release.test_base_update import setup_update
from apps.orchestrator.tests.release.test_publication_operations import intent


class Adoption:
    def __init__(self):
        self.applied = False
        self.writes = 0

    async def adopt(self, *args):
        self.writes += 1
        self.applied = True
        raise RuntimeError("after adoption")

    async def inspect(self, *args):
        if not self.applied:
            raise ReleaseReconciliationRequired()


def prepared():
    _, _, record, remote = setup_update()
    updated = replace(record.pull_request, head_sha="e" * 40, base_sha="c" * 40)
    receipt = replace(
        intent(remote.request),
        status=OperationStatus.SUCCEEDED,
        outcome=asdict(updated),
        outcome_schema_version=1,
        completed_at=datetime.now(UTC),
        remote_resource_id=updated.node_id,
    )
    project = uuid4()
    identity = WorktreeIdentity.for_run(
        project, record.run_id, branch=updated.head_ref, database_enabled=False
    )
    tree = ManagedWorktree(
        identity=identity, path=Path.cwd() / "worktree", base_sha=record.pull_request.base_sha
    )
    policy = ProjectPolicy(
        id=project,
        version=1,
        repository_path=str(Path.cwd()),
        github_repository=updated.base_repository,
        default_branch="main",
    )
    return record, receipt, tree, policy


async def test_adoption_crash_reconciles_without_repeating_local_mutation():
    record, receipt, tree, policy = prepared()
    port = Adoption()
    operation = BaseAdoptionOperation(record, receipt, tree, policy, port)
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.reconcile(intent(operation.request))
    assert port.writes == 0
    with pytest.raises(RuntimeError, match="after adoption"):
        await operation.invoke(intent(operation.request))
    outcome = await operation.reconcile(intent(operation.request))
    assert outcome.payload == receipt.outcome and port.writes == 1


@pytest.mark.parametrize("defect", ["pending", "head", "policy", "outcome"])
def test_adoption_requires_bound_successful_remote_receipt(defect):
    record, receipt, tree, policy = prepared()
    if defect == "pending":
        receipt = replace(
            receipt,
            status=OperationStatus.PENDING,
            outcome=None,
            outcome_schema_version=None,
            completed_at=None,
        )
    elif defect == "head":
        record = replace(record, pull_request=replace(record.pull_request, head_sha="f" * 40))
    elif defect == "policy":
        policy = policy.model_copy(update={"version": 2})
    else:
        receipt = replace(receipt, outcome=dict(receipt.outcome) | {"node_id": "other"})
    with pytest.raises(ReleaseReconciliationRequired):
        BaseAdoptionOperation(record, receipt, tree, policy, Adoption())
