from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import (
    DatabaseBinding,
    EnvironmentStagingInspection,
    ManagedWorktree,
)
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.validation import command_spec_digest
from forge.tools.developer_worktree import DeveloperWorktreeError, DeveloperWorktreeLifecycle
from forge.tools.worktree_manifest import WorktreeManifestStore


class Git:
    def __init__(self, root: Path, log: list[str]) -> None:
        self.repository_path = root
        self.log = log
        self.current: ManagedWorktree | None = None
        self.branch_retained = False

    def resolve_default_base_sha(self) -> str:
        return "a" * 40

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        return ManagedWorktree(
            identity=identity,
            path=self.repository_path / ".worktrees" / identity.worktree_name,
            base_sha=base_sha,
        )

    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree | None:
        expected = self.expected_worktree(identity, base_sha)
        if self.current is None and self.branch_retained:
            raise RuntimeError("retained branch prevents create-oriented inspection")
        return self.current if self.current == expected else None

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        self.log.append("worktree:create")
        self.current = self.expected_worktree(identity, base_sha)
        return self.current

    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        self.log.append("worktree:remove")
        assert self.current == worktree
        self.current = None
        self.branch_retained = True

    def verify_worktree_absent(self, worktree: ManagedWorktree) -> None:
        assert self.current is None

    def prune(self) -> None:
        self.log.append("worktree:prune")


class Database:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.binding: DatabaseBinding | None = None

    async def provision_standalone(self, identity, policy, *, policy_version):
        self.log.append("database:provision")
        self.binding = DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=identity.database_name,
            database_role=identity.database_role,
            secret_id=f"secret-{identity.worktree_name}",
            environment={"DATABASE_URL": "postgres://scoped-secret"},
        )
        return self.binding

    async def rematerialize_standalone(self, identity, policy, resource, *, policy_version):
        self.log.append("database:verify")
        assert self.binding is not None
        return self.binding

    async def teardown_standalone(self, identity, policy, resource, *, policy_version):
        self.log.append("database:remove")
        self.binding = None
        return DatabaseBinding(state=ResourceState.REMOVED)


class Stager:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.present = False

    def build_plan(self, worktree, policy, resource, *, policy_version=None):
        self.log.append("environment:plan")
        return SimpleNamespace(evidence=())

    def publish(self, worktree, policy, plan):
        self.log.append("environment:publish")
        self.present = True
        return ()

    def inspect(self, worktree, policy, plan):
        self.log.append("environment:inspect")
        return EnvironmentStagingInspection(present=self.present)


class Runner:
    async def run_terminal(self, request):
        raise AssertionError("no commands expected")


class Factory:
    def create(self, worktree, policy):
        return Runner()


def policy(root: Path, *, database: bool = False) -> ProjectPolicy:
    data = {
        "id": uuid4(),
        "version": 2,
        "repository_path": str(root.resolve()),
        "github_repository": "owner/repository",
        "default_branch": "main",
        "runner_mode": RunnerMode.TRUSTED_HOST,
        "trusted_project": True,
        "allowed_environment_files": (),
    }
    if database:
        data["database"] = {
            "enabled": True,
            "admin_url_secret_reference": "secret://environment/FORGE_TEST_ADMIN",
        }
    return ProjectPolicy.model_validate(data)


@pytest.mark.asyncio
async def test_setup_checkpoints_before_each_later_effect(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    database = Database(log)
    stager = Stager(log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=stager,
        runner_factory=Factory(),
        manifests=manifests,
    )
    selected = policy(root, database=True)

    worktree = await service.setup(selected, "feature/demo")

    assert worktree == git.current
    assert log == [
        "worktree:create",
        "database:provision",
        "environment:plan",
        "environment:publish",
        "environment:inspect",
    ]
    manifest = manifests.load(selected.id, "feature/demo")
    assert manifest.completed_checkpoints == (
        "manifest.created",
        "worktree.created",
        "database.active",
        "environment.staged",
        "setup.complete",
    )


@pytest.mark.asyncio
async def test_setup_resumes_without_repeating_completed_effects(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    database = Database(log)
    stager = Stager(log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=stager,
        runner_factory=Factory(),
        manifests=manifests,
    )
    selected = policy(root)
    await service.setup(selected, "feature/resume")
    log.clear()

    await service.setup(selected, "feature/resume")

    assert log == ["environment:plan", "environment:inspect"]


@pytest.mark.asyncio
async def test_teardown_is_worktree_first_and_retains_branch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    database = Database(log)
    stager = Stager(log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=stager,
        runner_factory=Factory(),
        manifests=manifests,
    )
    selected = policy(root, database=True)
    await service.setup(selected, "feature/remove")
    log.clear()

    await service.teardown(selected, "feature/remove")

    assert log == ["worktree:remove", "worktree:prune", "database:remove"]
    assert git.branch_retained
    assert not manifests.exists(selected.id, "feature/remove")


@pytest.mark.asyncio
async def test_teardown_retry_starts_after_verified_worktree_removal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    database = Database(log)
    stager = Stager(log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=stager,
        runner_factory=Factory(),
        manifests=manifests,
    )
    selected = policy(root, database=True)
    await service.setup(selected, "feature/retry")
    original = database.teardown_standalone

    async def fail(*args, **kwargs):
        log.append("database:failed")
        raise RuntimeError("secret diagnostic")

    database.teardown_standalone = fail
    with pytest.raises(RuntimeError, match="developer worktree operation failed"):
        await service.teardown(selected, "feature/retry")
    database.teardown_standalone = original
    log.clear()

    await service.teardown(selected, "feature/retry")

    assert log == ["database:remove"]


@pytest.mark.asyncio
async def test_teardown_recovers_when_removal_finished_before_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    database = Database(log)
    stager = Stager(log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=stager,
        runner_factory=Factory(),
        manifests=manifests,
    )
    selected = policy(root)
    await service.setup(selected, "feature/interrupted-removal")
    git.current = None
    git.branch_retained = True
    log.clear()

    await service.teardown(selected, "feature/interrupted-removal")

    assert log == ["worktree:prune"]
    assert not manifests.exists(selected.id, "feature/interrupted-removal")


@pytest.mark.asyncio
async def test_setup_does_not_repeat_command_with_ambiguous_started_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    log: list[str] = []
    git = Git(root, log)
    manifests = WorktreeManifestStore(tmp_path / "data")
    service = DeveloperWorktreeLifecycle(
        git=git,
        database=Database(log),
        environment_stager=Stager(log),
        runner_factory=Factory(),
        manifests=manifests,
    )
    command = CommandSpec(
        kind=StepKind.MIGRATION,
        name="migrate-once",
        argv=("forge-migrate",),
        timeout_seconds=30,
    )
    selected = policy(root).model_copy(update={"commands": (command,)})
    branch = "feature/ambiguous-command"
    await service.setup(selected, branch, bootstrap=False)
    manifest = manifests.load(selected.id, branch)
    started = f"setup.command-started:0:{command_spec_digest(command)}"
    checkpoints = tuple(
        checkpoint
        for checkpoint in manifest.completed_checkpoints
        if checkpoint != "setup.complete"
    )
    interrupted = manifest.model_copy(update={"completed_checkpoints": (*checkpoints, started)})
    manifests.save(interrupted)

    with pytest.raises(
        DeveloperWorktreeError,
        match="developer worktree operation failed",
    ):
        await service.setup(selected, branch)

    assert manifests.load(selected.id, branch) == interrupted
