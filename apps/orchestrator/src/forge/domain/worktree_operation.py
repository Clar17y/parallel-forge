"""Canonical durable worktree creation identity shared by writers and readers."""

import hashlib

from forge.domain.operation import OperationRequest, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot

WORKTREE_PROTOCOL_VERSION = 1


def worktree_creation_request(
    run: RunSnapshot,
    identity: WorktreeIdentity,
    policy: ProjectPolicy,
) -> OperationRequest:
    payload: dict[str, object] = {
        "project_id": str(run.project_id),
        "run_id": str(run.id),
        "policy_version": policy.version,
        "branch_digest": hashlib.sha256(identity.branch.encode("utf-8")).hexdigest(),
        "worktree_name": identity.worktree_name,
        "base_sha": run.base_sha,
        "database_state": (
            ResourceState.ACTIVE.value if policy.database.enabled else ResourceState.DISABLED.value
        ),
    }
    return OperationRequest(
        run_id=run.id,
        kind="worktree.create",
        idempotency_key=(
            f"forge-worktree-v{WORKTREE_PROTOCOL_VERSION}:worktree.create:"
            f"{run.project_id.hex}:{run.id.hex}:{policy.version}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )
