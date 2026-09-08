"""Exact operator confirmation for a frozen run resource identity."""

from forge.domain.operation import canonical_digest
from forge.domain.run import RunSnapshot


def teardown_identity(run: RunSnapshot) -> dict[str, object]:
    """Public resource identity bound by the operator confirmation."""
    return {
        "run_id": str(run.id),
        "run_version": run.version,
        "project_id": str(run.project_id),
        "policy_version": run.policy_version,
        "worktree_path": run.worktree_path,
        "branch_name": run.branch_name,
        "base_ref": run.base_ref,
        "base_sha": run.base_sha,
        "database_state": run.database_state.value,
        "database_name": run.database_name,
        "database_role": run.database_role,
    }


def teardown_confirmation(run: RunSnapshot) -> str:
    return f"teardown:{run.id}:{canonical_digest(teardown_identity(run))}"
