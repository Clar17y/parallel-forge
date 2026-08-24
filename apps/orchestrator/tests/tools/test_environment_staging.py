from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import DatabaseBinding
from forge.domain.policy import DatabaseProvisioningPolicy, ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.database import DatabaseProvisioner
from pydantic import ValidationError


def test_environment_stager_exposes_a_redacted_plan_api() -> None:
    from forge.tools.environment import EnvironmentStager

    assert EnvironmentStager is not None


def test_environment_plan_does_not_expose_private_source_or_output_bytes() -> None:
    from forge.application.ports.worktrees import (
        _STAGING_PLAN_SEAL,
        EnvironmentFileEvidence,
        EnvironmentStagingPlan,
    )

    evidence = EnvironmentFileEvidence(
        path_digest="0" * 64,
        source_digest="1" * 64,
        output_digest="2" * 64,
        byte_count=7,
    )
    plan = EnvironmentStagingPlan(
        seal=_STAGING_PLAN_SEAL,
        token=object(),
        evidence=(evidence,),
    )

    assert not hasattr(plan, "_files")
    assert not hasattr(plan, "_token")
    assert plan.token is not None
    assert "SOURCE_SENTINEL" not in repr(plan)
    assert "OUTPUT_SENTINEL" not in repr(plan)


def test_worktree_capability_does_not_expose_handles_or_git_state() -> None:
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.domain.policy import RunnerMode
    from forge.tools.git import _CAPABILITY_SEAL, WorktreeCapability

    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(Path.cwd()),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )
    owner = object()
    capability = WorktreeCapability(
        seal=_CAPABILITY_SEAL,
        owner=owner,
        git=owner,  # type: ignore[arg-type]
        worktree=ManagedWorktree(
            identity=identity,
            path=Path.cwd() / ".worktrees" / "forge-test",
            base_sha="0" * 40,
        ),
        policy=policy,
        access=object(),
    )

    assert not hasattr(capability, "_access")
    assert not hasattr(capability, "_git")


def test_released_worktree_capability_cannot_be_reused(tmp_path) -> None:
    from forge.domain.policy import RunnerMode
    from forge.tools.git import ControlledGitError
    from test_git import _controlled, _source_repository

    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )

    with controlled.open_worktree_capability(worktree, policy) as capability:
        capability.revalidate()
    with pytest.raises(ControlledGitError):
        capability.revalidate()


def test_capability_rejects_identity_forgery_with_same_target_name(tmp_path) -> None:
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.domain.policy import RunnerMode
    from forge.tools.git import ControlledGitError
    from test_git import _controlled, _source_repository

    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    forged_identity = replace(identity, project_id=uuid4())
    forged = ManagedWorktree(identity=forged_identity, path=worktree.path, base_sha=base_sha)
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )

    with pytest.raises(ControlledGitError), controlled.open_worktree_capability(forged, policy):
        pass


def test_capability_rejects_worktree_head_drift(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.git import ControlledGitError
    from test_git import _controlled, _source_repository

    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "drift.txt").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree.path), "add", "drift.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree.path),
            "-c",
            "user.name=Forge",
            "-c",
            "user.email=forge@example.test",
            "commit",
            "-m",
            "drift",
        ],
        check=True,
        capture_output=True,
    )
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )

    with pytest.raises(ControlledGitError), controlled.open_worktree_capability(worktree, policy):
        pass


def test_active_binding_rejects_extra_environment_entries() -> None:
    from forge.domain.policy import DatabaseProvisioningPolicy
    from forge.tools.environment import EnvironmentStagingError, _validate_binding

    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", True)
    policy = DatabaseProvisioningPolicy(
        enabled=True,
        admin_url_secret_reference="secret://forge/admin",
        injected_environment_key="DATABASE_URL",
    )
    resource = DatabaseBinding(
        state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}",
        environment={
            "DATABASE_URL": "postgresql://scoped",
            "EXTRA": "sentinel",
        },
    )

    with pytest.raises(EnvironmentStagingError):
        _validate_binding(identity, type("Policy", (), {"database": policy})(), resource)


def test_unignored_destination_is_rejected_before_publication(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    (repository / "config" / "local.env").write_bytes(b"TOKEN=SOURCE\n")
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))

    with pytest.raises(EnvironmentStagingError):
        stager.publish(worktree, policy, plan)
    assert not (worktree.path / "config" / "local.env").exists()


def test_forged_plan_cannot_publish_arbitrary_bytes(tmp_path) -> None:
    import hashlib
    import subprocess

    from forge.application.ports.worktrees import (
        _STAGING_PLAN_SEAL,
        EnvironmentFileEvidence,
        EnvironmentStagingPlan,
    )
    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    (repository / "config" / "local.env").write_bytes(b"SAFE=SOURCE\n")
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    forged = b"FORGED_SENTINEL"
    evidence = EnvironmentFileEvidence(
        path_digest=hashlib.sha256(b"config/local.env").hexdigest(),
        source_digest=hashlib.sha256(b"SAFE=SOURCE\n").hexdigest(),
        output_digest=hashlib.sha256(forged).hexdigest(),
        byte_count=len(forged),
    )
    plan = EnvironmentStagingPlan(
        seal=_STAGING_PLAN_SEAL,
        token=object(),
        evidence=(evidence,),
    )

    with pytest.raises(EnvironmentStagingError):
        EnvironmentStager(controlled).publish(worktree, policy, plan)
    assert not (worktree.path / "config" / "local.env").exists()


def test_source_hard_link_is_rejected_without_destination_write(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    source = repository / "config" / "local.env"
    source.write_bytes(b"TOKEN=SOURCE\n")
    (repository / "config" / "local-copy.env").hardlink_to(source)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )

    with pytest.raises(EnvironmentStagingError):
        EnvironmentStager(controlled).build_plan(
            worktree, policy, DatabaseBinding(state=ResourceState.DISABLED)
        )
    assert not (worktree.path / "config" / "local.env").exists()


def test_policy_rejects_reserved_environment_component_before_staging() -> None:
    with pytest.raises(ValidationError):
        ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path="D:/Code/Parallel Forge",
            github_repository="owner/repository",
            default_branch="main",
            allowed_environment_files=(".forge/credentials.env",),
        )


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "config/../.env",
        "config//local.env",
        "config/local.env/",
        "config\\local.env",
        "/absolute.env",
        "C:/absolute.env",
        "\\\\server\\share\\secret.env",
        ".git/config",
        ".worktrees/foreign.env",
        ".forge/state.env",
        "node_modules/pkg.env",
        "env/local.env",
    ],
)
def test_policy_rejects_noncanonical_or_reserved_environment_paths(value: str) -> None:
    with pytest.raises(ValidationError):
        ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path="D:/Code/Parallel Forge",
            github_repository="owner/repository",
            default_branch="main",
            allowed_environment_files=(value,),
        )


@pytest.mark.parametrize(
    "value",
    [
        "config/local.env:secret",
        "config/local.env.",
        "config/local.env ",
        "CON",
        "config/PRN.txt",
        "config/NUL.env",
        "config/com1.log",
        "config/LPT9 ",
    ],
)
def test_policy_rejects_windows_aliases_before_source_read(value: str) -> None:
    with pytest.raises(ValidationError):
        ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path="D:/Code/Parallel Forge",
            github_repository="owner/repository",
            default_branch="main",
            allowed_environment_files=(value,),
        )


def test_inspection_does_not_create_missing_mutation_lock(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    (repository / "config" / "local.env").write_bytes(b"SAFE=SOURCE\n")
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))
    lock = repository / ".git" / "forge-worktree.lock"
    metadata = repository / ".git" / "worktrees"
    registration_entries = tuple(sorted(path.name for path in metadata.iterdir()))
    lock.unlink()

    with pytest.raises(EnvironmentStagingError):
        stager.inspect(worktree, policy, plan)

    assert not lock.exists()
    assert tuple(sorted(path.name for path in metadata.iterdir())) == registration_entries


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX descriptor test")
def test_posix_inspection_rejects_same_size_destination_mutation(tmp_path, monkeypatch) -> None:
    import os

    from forge.tools import paths as path_tools
    from forge.tools.paths import RepositoryAccessDenied, _StagingParent

    destination = tmp_path / "destination.env"
    destination.write_bytes(b"ORIGINAL\n")
    destination.chmod(0o600)
    descriptor = os.open(destination, os.O_RDONLY)
    parent = _StagingParent(base=descriptor, handles=[], path=tmp_path, windows=None)
    original_mtime = destination.stat().st_mtime_ns

    def mutate_same_size(_descriptor: int, _maximum: int) -> bytes:
        destination.write_bytes(b"CHANGED!\n")
        os.utime(destination, ns=(original_mtime, original_mtime + 1_000_000))
        return b"ORIGINAL\n"

    monkeypatch.setattr(path_tools, "_read_staging_descriptor", mutate_same_size)
    try:
        with pytest.raises(RepositoryAccessDenied):
            path_tools._read_staging_posix(parent, destination.name, 1024, require_acl=False)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX owner test")
def test_posix_inspection_rejects_destination_with_foreign_owner(tmp_path, monkeypatch) -> None:
    import os

    from forge.tools import paths as path_tools
    from forge.tools.paths import RepositoryAccessDenied, _StagingParent

    destination = tmp_path / "destination.env"
    destination.write_bytes(b"SAFE\n")
    destination.chmod(0o600)
    descriptor = os.open(destination, os.O_RDONLY)
    parent = _StagingParent(base=descriptor, handles=[], path=tmp_path, windows=None)
    current_uid = os.getuid()
    monkeypatch.setattr(path_tools.os, "getuid", lambda: current_uid + 1)
    try:
        with pytest.raises(RepositoryAccessDenied):
            path_tools._read_staging_posix(parent, destination.name, 1024, require_acl=False)
    finally:
        os.close(descriptor)


def test_windows_directory_flush_failure_is_not_suppressed(tmp_path, monkeypatch) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    (repository / "config" / "local.env").write_bytes(b"SAFE=SOURCE\n")
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))
    api = controlled._repository._windows
    if api is None:
        pytest.skip("Windows native path API unavailable")

    def fail_flush(_handle: int) -> None:
        raise OSError(5, "flush sentinel")

    monkeypatch.setattr(api, "flush_secret_directory", fail_flush)
    with pytest.raises(EnvironmentStagingError):
        stager.publish(worktree, policy, plan)


class _FakeWindowsStagingApi:
    def __init__(self, root, failure: str | None = None) -> None:
        self.root = root
        self.failure = failure
        self.handles: dict[int, str] = {}
        self.next_handle = 10

    def _handle(self, name: str) -> int:
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = name
        return handle

    def create_secret_file(self, path, name: str) -> int:
        path.touch()
        return self._handle(name)

    def write_secret(self, handle: int, data: bytes) -> None:
        if self.failure == "write":
            raise OSError("write fault")
        (self.root / self.handles[handle]).write_bytes(data)

    def link_secret(self, source, target) -> None:
        if self.failure == "link":
            raise OSError("link fault")
        target.hardlink_to(source)

    def dispose_link(self, handle: int) -> None:
        name = self.handles.get(handle)
        if name is not None:
            (self.root / name).unlink(missing_ok=True)
        if self.failure == "cleanup":
            raise OSError("cleanup fault")

    def dispose(self, handle: int) -> None:
        self.dispose_link(handle)

    def close(self, handle: int) -> None:
        self.handles.pop(handle, None)

    def open_secret_file(self, _parent, name: str, **_kwargs) -> int | None:
        if not (self.root / name).exists():
            return None
        return self._handle(name)

    def flush_secret_directory(self, _handle: int) -> None:
        if self.failure == "flush":
            raise OSError("flush fault")


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows publication fault seams")
@pytest.mark.parametrize("failure", ("write", "link", "flush", "reopen", "cleanup"))
def test_windows_publication_faults_leave_no_temporary_entries(
    tmp_path, monkeypatch, failure: str
) -> None:
    from forge.tools import paths as path_tools
    from forge.tools.paths import RepositoryAccessDenied, _StagingParent

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside sentinel")
    api = _FakeWindowsStagingApi(tmp_path, None if failure == "reopen" else failure)
    parent = _StagingParent(base=1, handles=[], path=tmp_path, windows=api)
    if failure == "reopen":
        monkeypatch.setattr(
            path_tools,
            "_read_staging_windows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RepositoryAccessDenied("reopen fault")),
        )

    with pytest.raises(RepositoryAccessDenied):
        path_tools._publish_staging_windows(parent, "destination.env", b"payload", maximum=1024)

    assert not tuple(tmp_path.glob(".forge-env-*"))
    assert outside.read_bytes() == b"outside sentinel"


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="native Linux libacl")
def test_linux_docker_staging_publishes_and_inspects_real_uid_10001_acl(tmp_path) -> None:
    import os
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools import paths as path_tools
    from forge.tools.environment import EnvironmentStager
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    source = b"DOCKER=SOURCE\n"
    (repository / "config" / "local.env").write_bytes(source)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))

    assert stager.publish(worktree, policy, plan) == plan.evidence
    destination = worktree.path / "config" / "local.env"
    assert destination.read_bytes() == source
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        assert path_tools._verify_linux_staging_acl(descriptor)
        lib = path_tools._load_libacl()
        acl = lib.acl_get_fd(descriptor)
        assert acl
        try:
            acl_text = path_tools._acl_text(lib, acl)
        finally:
            lib.acl_free(acl)
        assert "user:10001:r--" in acl_text.splitlines()
    finally:
        os.close(descriptor)

    inspected = stager.inspect(worktree, policy, plan)
    assert inspected.present is True
    assert inspected.evidence == plan.evidence


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="native Linux libacl")
def test_linux_acl_verification_requires_exact_uid_10001_entries() -> None:
    import ctypes

    from forge.tools import paths as path_tools

    class _FakeLib:
        def __init__(self, text: bytes) -> None:
            self.buffer = ctypes.create_string_buffer(text)

        def acl_get_fd(self, _descriptor: int):
            return ctypes.c_void_p(1)

        def acl_to_text(self, _acl, length) -> int:
            length._obj.value = len(self.buffer.value)
            return ctypes.addressof(self.buffer)

        def acl_free(self, _value) -> int:
            return 0

    exact = _FakeLib(b"user::rw-\nuser:10001:r--\ngroup::---\nmask::r--\nother::---\n")
    wrong_uid = _FakeLib(b"user::rw-\nuser:10002:r--\ngroup::---\nmask::r--\nother::---\n")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(path_tools, "_load_libacl", lambda: exact)
        assert path_tools._verify_linux_staging_acl(1) is True
        monkeypatch.setattr(path_tools, "_load_libacl", lambda: wrong_uid)
        assert path_tools._verify_linux_staging_acl(1) is False
    finally:
        monkeypatch.undo()


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="native Linux libacl")
def test_linux_acl_unavailable_fails_closed_without_shell(monkeypatch) -> None:
    from forge.application.ports.repository import RepositoryAccessDenied
    from forge.tools import paths as path_tools

    def unavailable():
        raise RepositoryAccessDenied("acl unavailable")

    monkeypatch.setattr(path_tools, "_load_libacl", unavailable)
    assert path_tools._verify_linux_staging_acl(1) is False


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="native Linux libacl")
def test_linux_acl_application_failure_fails_closed(monkeypatch) -> None:
    import ctypes

    from forge.tools import paths as path_tools
    from forge.tools.paths import RepositoryAccessDenied

    class _ApplicationFailure:
        def acl_from_text(self, _text: bytes):
            return ctypes.c_void_p(1)

        def acl_set_fd(self, _descriptor: int, _acl) -> int:
            return -1

        def acl_free(self, _value) -> int:
            return 0

    monkeypatch.setattr(path_tools, "_load_libacl", lambda: _ApplicationFailure())
    with pytest.raises(RepositoryAccessDenied):
        path_tools._set_linux_staging_acl(1)


def test_effective_secret_paths_is_ordered_union_and_rejects_platform_collisions() -> None:
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(Path.cwd()),
        github_repository="owner/repository",
        default_branch="main",
        secret_paths=(".env", "config/secret.env"),
        allowed_environment_files=("config/local.env", ".env"),
    )

    assert policy.effective_secret_paths == (
        ".env",
        "config/secret.env",
        "config/local.env",
    )
    if os.name == "nt":
        with pytest.raises(ValidationError):
            ProjectPolicy(
                id=uuid4(),
                version=1,
                repository_path=str(Path.cwd()),
                github_repository="owner/repository",
                default_branch="main",
                allowed_environment_files=("config/LOCAL.env", "config/local.env"),
            )
    else:
        accepted = ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path=str(Path.cwd()),
            github_repository="owner/repository",
            default_branch="main",
            allowed_environment_files=("config/LOCAL.env", "config/local.env"),
        )
        assert accepted.allowed_environment_files == ("config/LOCAL.env", "config/local.env")


def test_policy_rejects_cross_field_windows_case_aliases() -> None:
    if os.name != "nt":
        pytest.skip("Windows casefold policy")
    with pytest.raises(ValidationError):
        ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path=str(Path.cwd()),
            github_repository="owner/repository",
            default_branch="main",
            secret_paths=("config/Secret.env",),
            allowed_environment_files=("config/secret.env",),
        )


def test_dotenv_rewrite_replaces_preserves_line_endings_and_appends() -> None:
    from forge.tools.environment import _rewrite_dotenv

    source = "FIRST=one\r\nexport DATABASE_URL = old\r\nLAST=two\r\n"
    assert _rewrite_dotenv(source, "DATABASE_URL", "postgresql://scoped") == (
        b"FIRST=one\r\nexport DATABASE_URL = postgresql://scoped\r\nLAST=two\r\n"
    )
    assert _rewrite_dotenv("FIRST=one\r\n", "DATABASE_URL", "scoped") == (
        b"FIRST=one\r\nDATABASE_URL=scoped"
    )


def test_dotenv_rewrite_rejects_duplicate_active_assignments() -> None:
    from forge.tools.environment import EnvironmentStagingError, _rewrite_dotenv

    with pytest.raises(EnvironmentStagingError):
        _rewrite_dotenv("DATABASE_URL=one\nexport DATABASE_URL=two\n", "DATABASE_URL", "scoped")


@pytest.mark.asyncio
async def test_disabled_rematerialization_returns_empty_binding_without_dependencies() -> None:
    provisioner = DatabaseProvisioner(
        operation_executor=object(),
        operation_repository=object(),
        admin_secret_resolver=object(),
        secret_store=object(),
        password_source=object(),
        connection_factory=object(),
    )
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)

    result = await provisioner.rematerialize_active(
        identity,
        DatabaseProvisioningPolicy(enabled=False),
        DatabaseBinding(state=ResourceState.DISABLED),
        policy_version=1,
    )

    assert result == DatabaseBinding(state=ResourceState.DISABLED)


@pytest.mark.asyncio
async def test_disabled_rematerialization_rejects_nonempty_environment_before_dependencies() -> (
    None
):
    class _Exploding:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"dependency touched: {name}")

    provisioner = DatabaseProvisioner(
        operation_executor=_Exploding(),
        operation_repository=_Exploding(),
        admin_secret_resolver=_Exploding(),
        secret_store=_Exploding(),
        password_source=_Exploding(),
        connection_factory=_Exploding(),
    )
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)

    from forge.tools.database import DatabaseIntegrityError

    with pytest.raises(DatabaseIntegrityError):
        await provisioner.rematerialize_active(
            identity,
            DatabaseProvisioningPolicy(enabled=False),
            DatabaseBinding(state=ResourceState.DISABLED, environment={"LEAK": "sentinel"}),
            policy_version=1,
        )


@pytest.mark.asyncio
async def test_enabled_rematerialization_reuses_proof_without_mutating_operations(
    tmp_path,
) -> None:
    from forge.application.services.recovery import OperationExecutor
    from test_database_provisioner import (
        _enabled_policy,
        _identity,
        _MemoryOperationRepository,
        _provisioner,
    )

    events: list[str] = []
    identity = _identity()
    policy = _enabled_policy()
    repository = _MemoryOperationRepository()
    provisioner, resolver, source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=policy,
        events=events,
        executor=OperationExecutor(repository),
        operation_repository=repository,
    )
    binding = await provisioner.provision(identity, policy, policy_version=7)
    operation_count = len(repository.by_id)
    resolver_calls = list(resolver.calls)
    statements = len(connection.statements)

    rematerialized = await provisioner.rematerialize_active(
        identity,
        policy,
        binding,
        policy_version=7,
    )

    assert rematerialized.state is ResourceState.ACTIVE
    assert rematerialized.database_name == binding.database_name
    assert rematerialized.database_role == binding.database_role
    assert rematerialized.secret_id == binding.secret_id
    assert tuple(rematerialized.environment) == (policy.injected_environment_key,)
    assert len(repository.by_id) == operation_count
    assert resolver.calls == resolver_calls + [policy.admin_url_secret_reference]
    assert len(connection.statements) > statements
    assert "postgresql://" not in repr(rematerialized)
    assert "postgresql://" not in repr(rematerialized.environment)
    assert source.value.decode(errors="ignore") not in repr(rematerialized)


@pytest.mark.asyncio
async def test_rematerialization_sanitizes_secret_store_exception_context(tmp_path) -> None:
    from test_database_provisioner import _enabled_policy, _identity, _provisioner

    class _SentinelSecretStore:
        def exists(self, secret_id: str) -> bool:
            del secret_id
            return True

        def read(self, secret_id: str) -> bytes:
            del secret_id
            raise RuntimeError("PASSWORD_SENTINEL")

    events: list[str] = []
    identity = _identity()
    policy = _enabled_policy()
    provisioner, _resolver, _source, _connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=policy,
        events=events,
    )
    binding = await provisioner.provision(identity, policy, policy_version=7)
    provisioner._secret_store = _SentinelSecretStore()

    from forge.tools.database import DatabaseProvisionerError

    with pytest.raises(DatabaseProvisionerError) as error:
        await provisioner.rematerialize_active(identity, policy, binding, policy_version=7)
    assert "PASSWORD_SENTINEL" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_staging_copies_ignored_source_bytes_without_exposing_them(tmp_path) -> None:
    from forge.application.ports.worktrees import _STAGING_PLAN_SEAL, EnvironmentStagingPlan
    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager
    from test_git import _controlled, _source_repository

    repository, base_sha = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    source = b"TOKEN=SOURCE_SENTINEL\n"
    (repository / "config" / "local.env").write_bytes(source)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=3,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(
        worktree,
        policy,
        DatabaseBinding(state=ResourceState.DISABLED),
        policy_version=3,
    )

    assert plan.file_count == 1
    assert plan.evidence[0].byte_count == len(source)
    assert "SOURCE_SENTINEL" not in repr(plan)

    original_token = plan.token
    object.__setattr__(plan, "_token", object())
    from forge.tools.environment import EnvironmentStagingError

    with pytest.raises(EnvironmentStagingError):
        stager.publish(worktree, policy, plan)
    assert not (worktree.path / "config" / "local.env").exists()

    copied = EnvironmentStagingPlan(
        seal=_STAGING_PLAN_SEAL,
        token=original_token,
        evidence=plan.evidence,
    )
    with pytest.raises(EnvironmentStagingError):
        EnvironmentStager(controlled).publish(worktree, policy, copied)


def test_reflective_plan_payload_mutation_is_rejected_before_write(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    (repository / "config" / "local.env").write_bytes(b"SAFE=SOURCE\n")
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env",),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))
    record = next(iter(object.__getattribute__(stager, "_plans").values()))
    assert "SAFE=SOURCE" not in repr(plan)
    with pytest.raises(AttributeError):
        object.__getattribute__(plan, "_files")
    object.__setattr__(record.files[0], "output", b"FORGED_SENTINEL")

    with pytest.raises(EnvironmentStagingError):
        stager.publish(worktree, policy, plan)
    assert not (worktree.path / "config" / "local.env").exists()


def test_staging_publishes_and_inspects_exact_destination_idempotently(tmp_path) -> None:
    import subprocess

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager
    from test_git import _controlled, _source_repository

    repository, _ = _source_repository(tmp_path)
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore env files"],
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "config").mkdir()
    source = b"TOKEN=SOURCE_SENTINEL\n"
    (repository / "config" / "local.env").write_bytes(source)
    second = b"SECOND=SOURCE\n"
    (repository / "config" / "second.env").write_bytes(second)
    identity = WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/staging", False)
    controlled = _controlled(repository, tmp_path / "state")
    worktree = controlled.create_worktree(identity, base_sha)
    (worktree.path / "config").mkdir()
    policy = ProjectPolicy(
        id=uuid4(),
        version=3,
        repository_path=str(repository),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        allowed_environment_files=("config/local.env", "config/second.env"),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(worktree, policy, DatabaseBinding(state=ResourceState.DISABLED))

    destination = worktree.path / "config" / "local.env"
    second_destination = worktree.path / "config" / "second.env"
    destination.write_bytes(source)
    from forge.tools.environment import EnvironmentReconciliationRequired

    with pytest.raises(EnvironmentReconciliationRequired):
        stager.publish(worktree, policy, plan)
    assert not second_destination.exists()
    destination.unlink()

    assert stager.publish(worktree, policy, plan) == plan.evidence
    assert destination.read_bytes() == source
    assert second_destination.read_bytes() == second
    second_destination.unlink()
    assert not second_destination.exists()
    from forge.tools.environment import EnvironmentReconciliationRequired

    with pytest.raises(EnvironmentReconciliationRequired):
        stager.inspect(worktree, policy, plan)
    with pytest.raises(EnvironmentReconciliationRequired):
        stager.publish(worktree, policy, plan)
    assert destination.read_bytes() == source
    assert not second_destination.exists()
    assert not tuple((worktree.path / "config").glob(".forge-env-*"))
    different = b"DIFFERENT_DESTINATION"
    destination.write_bytes(different)
    from forge.tools.environment import EnvironmentStagingError

    with pytest.raises(EnvironmentStagingError):
        stager.publish(worktree, policy, plan)
    assert destination.read_bytes() == different


def _assert_staging_error_is_fully_redacted(error: BaseException) -> None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert "SECRET_CHAIN_SENTINEL" not in repr(current)
        assert "URL_CHAIN_SENTINEL" not in repr(current)
        assert current.__cause__ is None
        assert current.__context__ is None
        current = current.__cause__ or current.__context__


def test_environment_stager_wrappers_drop_raw_exception_context() -> None:
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace

    from forge.domain.policy import RunnerMode
    from forge.tools.environment import EnvironmentStager, EnvironmentStagingError

    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(Path.cwd()),
        github_repository="owner/repository",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )

    @contextmanager
    def fail_open(*_args, **_kwargs):
        raise OSError("SECRET_CHAIN_SENTINEL URL_CHAIN_SENTINEL")
        yield  # pragma: no cover

    controlled = SimpleNamespace(
        repository_path=Path.cwd(),
        open_worktree_capability=fail_open,
    )
    stager = EnvironmentStager(controlled)

    for operation in (
        lambda: stager.build_plan(object(), policy, DatabaseBinding(state=ResourceState.DISABLED)),
        lambda: stager.publish(object(), policy, object()),
        lambda: stager.inspect(object(), policy, object()),
    ):
        with pytest.raises(EnvironmentStagingError) as error:
            operation()
        _assert_staging_error_is_fully_redacted(error.value)
