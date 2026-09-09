"""Production delivery runtime composition."""

from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import DatabaseBinding, ManagedWorktree
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot
from forge.settings import Settings
from forge.worker.delivery_runtime import DeliveryRuntime, DeliveryRuntimeError


def _policy(*, repository: Path, mode: RunnerMode = RunnerMode.DOCKER) -> ProjectPolicy:
    return ProjectPolicy(
        id=uuid4(),
        version=2,
        repository_path=str(repository),
        github_repository="example/forge",
        default_branch="main",
        runner_mode=mode,
        trusted_project=mode is RunnerMode.TRUSTED_HOST,
        secret_paths=(".secret",),
        allowed_environment_files=(".env.local",),
    )


class _Git:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        return ManagedWorktree(
            identity=identity, path=Path("/managed").resolve() / identity.worktree_name, base_sha=base_sha
        )

    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        return self.expected_worktree(identity, base_sha)


def _worktree(policy: ProjectPolicy) -> ManagedWorktree:
    identity = WorktreeIdentity.for_run(policy.id, uuid4(), "forge/run", False)
    return _Git().expected_worktree(identity, "a" * 40)


def test_policy_bound_factories_use_fresh_matching_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import forge.worker.delivery_runtime as module

    gits: list[_Git] = []
    readers: list[dict[str, object]] = []
    writers: list[tuple[object, object, object]] = []
    runners: list[dict[str, object]] = []

    def make_git(*args: object, **kwargs: object) -> _Git:
        git = _Git(*args, **kwargs)
        gits.append(git)
        return git

    class Reader:
        def __init__(self, **kwargs: object) -> None:
            readers.append(kwargs)

    class Writer:
        def __init__(self, git: object, worktree: object, policy: object) -> None:
            writers.append((git, worktree, policy))

    class RunnerFactory:
        def __init__(self, git: object, **kwargs: object) -> None:
            runners.append({"git": git, **kwargs})

        def create(self, worktree: object, policy: object) -> tuple[object, object]:
            return worktree, policy

    monkeypatch.setattr(module, "ControlledGit", make_git)
    monkeypatch.setattr(module, "RepositoryReader", Reader)
    monkeypatch.setattr(module, "WorktreeRepositoryWriter", Writer)
    monkeypatch.setattr(module, "WorktreeRunnerFactory", RunnerFactory)
    monkeypatch.setattr(module.shutil, "which", lambda _: str(tmp_path / "git.exe"))
    (tmp_path / "git.exe").touch()

    runtime = DeliveryRuntime(
        Settings(data_root=tmp_path, runner_image="registry/runner@sha256:" + "a" * 64),
        object(),
        object(),
    )  # type: ignore[arg-type]
    first = _policy(repository=tmp_path.parent / "one")
    second = _policy(repository=tmp_path.parent / "two", mode=RunnerMode.TRUSTED_HOST)
    Path(first.repository_path).mkdir()
    Path(second.repository_path).mkdir()
    first_worktree, second_worktree = _worktree(first), _worktree(second)

    first_git = runtime.git(first)
    runtime.reader(first, first_worktree)
    runtime.writer(first, first_worktree, controlled_git=first_git)
    runtime.create(second_worktree, second)

    assert len(gits) == 3
    assert gits[0].kwargs["default_branch"] == "main"
    assert gits[0].kwargs["state_root"] == tmp_path / "git" / first.id.hex
    assert readers == [{"root": first_worktree.path, "secret_paths": first.effective_secret_paths}]
    assert writers == [(first_git, first_worktree, first)]
    assert runners[-1]["artifact_store"] is runtime._artifact_store
    assert runners[-1]["image_digest"] is None
    assert runners[-1]["audit"] is not None


@pytest.mark.asyncio
async def test_lifecycle_delegates_each_call_through_its_policy_bound_provisioner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    runtime = DeliveryRuntime(Settings(data_root=tmp_path), object(), object())  # type: ignore[arg-type]
    policy = _policy(repository=tmp_path / "repo", mode=RunnerMode.TRUSTED_HOST)
    calls: list[tuple[str, object, ProjectPolicy]] = []

    class Provisioner:
        async def prepare(self, value: object, actual: ProjectPolicy) -> str:
            calls.append(("prepare", value, actual))
            return "prepared"

        async def teardown(self, value: object, actual: ProjectPolicy) -> str:
            calls.append(("teardown", value, actual))
            return "torn-down"

        async def reconcile(self, value: object, actual: ProjectPolicy) -> str:
            calls.append(("reconcile", value, actual))
            return "reconciled"

    monkeypatch.setattr(runtime, "git", lambda _: _Git())
    monkeypatch.setattr(runtime, "_provisioner", lambda *_: Provisioner())
    run_id, intent_id = uuid4(), uuid4()
    assert await runtime.prepare(run_id, policy) == "prepared"
    assert await runtime.teardown(run_id, policy) == "torn-down"
    assert await runtime.reconcile(intent_id, policy) == "reconciled"
    assert calls == [
        ("prepare", run_id, policy),
        ("teardown", run_id, policy),
        ("reconcile", intent_id, policy),
    ]


@pytest.mark.asyncio
async def test_environment_is_transient_and_rejects_invalid_binding_before_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = DeliveryRuntime(Settings(data_root=tmp_path), object(), object())  # type: ignore[arg-type]
    policy = _policy(repository=tmp_path / "repo", mode=RunnerMode.TRUSTED_HOST)
    worktree = _worktree(policy)
    run = RunSnapshot(
        id=worktree.identity.run_id,
        project_id=policy.id,
        task_id=uuid4(),
        policy_version=policy.version,
        branch_name=worktree.identity.branch,
        worktree_path=str(worktree.path),
        base_sha=worktree.base_sha,
    )
    monkeypatch.setattr(runtime, "git", lambda _: _Git())

    assert await runtime.environment(run, policy, worktree) == {}

    called = False

    def forbidden_database(_: ProjectPolicy) -> object:
        nonlocal called
        called = True
        raise AssertionError("invalid authority must not materialize a database")

    monkeypatch.setattr(runtime, "_database", forbidden_database)
    with pytest.raises(DeliveryRuntimeError):
        await runtime.environment(
            run,
            policy,
            ManagedWorktree(
                identity=worktree.identity,
                path=worktree.path,
                base_sha="b" * 40,
            ),
        )
    assert not called


@pytest.mark.asyncio
async def test_active_environment_rematerializes_only_matching_database_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = DeliveryRuntime(Settings(data_root=tmp_path), object(), object())  # type: ignore[arg-type]
    values = _policy(repository=tmp_path / "repo", mode=RunnerMode.TRUSTED_HOST).model_dump()
    values["database"] = {
        "enabled": True,
        "admin_url_secret_reference": "secret://environment/POSTGRES_ADMIN",
    }
    policy = ProjectPolicy.model_validate(values)
    worktree = _worktree(policy)
    identity = WorktreeIdentity.for_run(
        policy.id, worktree.identity.run_id, worktree.identity.branch, True
    )
    worktree = _Git().expected_worktree(identity, worktree.base_sha)
    run = RunSnapshot(
        id=identity.run_id,
        project_id=policy.id,
        task_id=uuid4(),
        policy_version=policy.version,
        branch_name=identity.branch,
        worktree_path=str(worktree.path),
        base_sha=worktree.base_sha,
        database_state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
    )
    calls: list[tuple[object, object, object, int]] = []

    class Database:
        async def rematerialize_active(self, *args: object, policy_version: int) -> DatabaseBinding:
            calls.append((*args, policy_version))
            return DatabaseBinding(
                state=ResourceState.ACTIVE,
                database_name=identity.database_name,
                database_role=identity.database_role,
                secret_id=run.secret_id,
                environment={"DATABASE_URL": "secret"},
            )

    monkeypatch.setattr(runtime, "git", lambda _: _Git())
    monkeypatch.setattr(runtime, "_database", lambda _: Database())
    assert await runtime.environment(run, policy, worktree) == {"DATABASE_URL": "secret"}
    assert calls and calls[0][0] == identity and calls[0][3] == policy.version


@pytest.mark.asyncio
async def test_trusted_host_audit_is_fail_closed_when_durable_append_fails(tmp_path: Path) -> None:
    from forge.worker.delivery_runtime import _TrustedHostAudit

    policy = _policy(repository=tmp_path / "repo", mode=RunnerMode.TRUSTED_HOST)
    worktree = _worktree(policy)
    run = RunSnapshot(
        id=worktree.identity.run_id,
        project_id=policy.id,
        task_id=uuid4(),
        policy_version=policy.version,
        branch_name=worktree.identity.branch,
        worktree_path=str(worktree.path),
        base_sha=worktree.base_sha,
    )

    class Work:
        class Runs:
            async def get(self, _: object) -> RunSnapshot:
                return run

        class Projects:
            async def get_policy(self, *_: object) -> SimpleNamespace:
                return SimpleNamespace(document=policy.model_dump(mode="json"))

        class Events:
            async def append(self, _: object) -> None:
                raise RuntimeError("down")

        runs = Runs()
        projects = Projects()
        events = Events()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            raise AssertionError("must not commit after failed append")

    audit = _TrustedHostAudit(lambda: Work(), worktree, policy)
    with pytest.raises(RuntimeError, match="down"):
        await audit.record(
            "runner.trusted_host.attempt",
            priority="high",
            payload={
                "command_kind": "test",
                "command_name": "check",
                "network_containment": False,
                "policy_version": policy.version,
                "runner_mode": "trusted_host",
                "unsandboxed": True,
            },
        )
