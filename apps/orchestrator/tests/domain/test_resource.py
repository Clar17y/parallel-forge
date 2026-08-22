"""Unit coverage for resource identity and lifecycle value types."""

from uuid import UUID

import pytest
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState, SuspensionKind

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
RUN_ID = UUID("abcdefab-cdef-abcd-efab-cdefabcdefab")
OTHER_RUN_ID = UUID("fedcbafe-dcba-fedc-bafe-dcbafedcbafe")


def test_resource_identity_is_stable_and_collision_resistant() -> None:
    first = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch="feature/a", database_enabled=True)
    second = WorktreeIdentity.for_run(
        PROJECT_ID, OTHER_RUN_ID, branch="feature-a", database_enabled=True
    )

    assert first.database_name is not None
    assert first.database_name.startswith("forge_")
    assert first.database_name != second.database_name
    assert first.worktree_name != second.worktree_name


def test_database_identity_is_absent_when_project_disables_provisioning() -> None:
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="docs/readme", database_enabled=False
    )

    assert identity.database_name is None
    assert identity.database_role is None


def test_developer_identity_hashes_full_branch_after_sanitizing() -> None:
    first = WorktreeIdentity.for_developer(PROJECT_ID, branch="feature/a", database_enabled=True)
    second = WorktreeIdentity.for_developer(PROJECT_ID, branch="feature-a", database_enabled=True)

    assert first.worktree_name != second.worktree_name
    assert first.worktree_name[-12:] != second.worktree_name[-12:]
    assert first.database_name is not None
    assert first.database_name[-12:] == first.worktree_name[-12:]


@pytest.mark.parametrize("branch", ["", "  ", "x" * 513])
def test_identity_rejects_blank_or_overlong_branches(branch: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch=branch, database_enabled=False)


def test_resource_state_has_exact_database_lifecycle_values() -> None:
    assert {state.value for state in ResourceState} == {
        "DISABLED",
        "PROVISIONING",
        "ACTIVE",
        "FAILED",
        "REMOVED",
    }


@pytest.mark.parametrize(
    ("state", "database_name", "database_role", "secret_id"),
    [
        (ResourceState.DISABLED, "forge_db", None, None),
        (ResourceState.REMOVED, None, "forge_role", None),
        (ResourceState.ACTIVE, "forge_db", "forge_role", None),
    ],
)
def test_snapshot_rejects_invalid_resource_shapes(
    state: ResourceState,
    database_name: str | None,
    database_role: str | None,
    secret_id: str | None,
) -> None:
    with pytest.raises(ValueError, match="resource"):
        RunSnapshot(
            id=RUN_ID,
            project_id=PROJECT_ID,
            task_id=UUID("11111111-1111-1111-1111-111111111111"),
            database_state=state,
            database_name=database_name,
            database_role=database_role,
            secret_id=secret_id,
        )


def test_snapshot_resource_update_increments_once_without_workflow_mutation() -> None:
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=UUID("11111111-1111-1111-1111-111111111111"),
        state=RunState.AWAITING_HUMAN_INTERVENTION,
        version=3,
        suspended_state=RunState.PLANNING,
        suspension_kind=SuspensionKind.INTERVENTION,
    )

    changed = run.with_resource(
        worktree_path="/managed/forge",
        database_state=ResourceState.PROVISIONING,
        database_name="forge_db",
    )

    assert changed.version == 4
    assert changed.worktree_path == "/managed/forge"
    assert changed.database_state is ResourceState.PROVISIONING
    assert changed.database_name == "forge_db"
    assert changed.database_role is None
    assert changed.secret_id is None
    assert changed.state is run.state
    assert changed.suspended_state is run.suspended_state
    assert changed.suspension_kind is run.suspension_kind
    assert run.version == 3
