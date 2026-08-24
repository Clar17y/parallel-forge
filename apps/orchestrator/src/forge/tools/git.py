"""A narrow, noninteractive Git read boundary for managed worktrees."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Sequence
from pathlib import Path

from forge.application.ports.repository import ProcessResult, RepositoryAccessDenied
from forge.application.ports.worktrees import (
    EnvironmentFileEvidence,
    EnvironmentStagingInspection,
    EnvironmentStagingPlan,
    GitCommit,
    GitDiff,
    GitStatus,
    ManagedWorktree,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_METADATA_BYTES = 4096
_MAX_METADATA_ENTRIES = 256
_MAX_BRANCH_LENGTH = 255
_MAX_COMMIT_MESSAGE_BYTES = 4096

_FORGE_NAME = "Forge"
_FORGE_EMAIL = "forge@example.test"


class ControlledGitError(RuntimeError):
    """A stable, redacted controlled-Git failure."""

    def __init__(self) -> None:
        super().__init__("controlled git operation failed")


_CAPABILITY_SEAL = object()


class WorktreeCapability:
    """Opaque owner-sealed operations for one retained managed worktree."""

    __slots__ = ("_access", "_git", "_live", "_owner", "_policy", "_sealed", "_worktree")

    def __init__(
        self,
        *,
        seal: object,
        owner: object,
        git: ControlledGit,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        access: object,
    ) -> None:
        if seal is not _CAPABILITY_SEAL:
            raise TypeError("worktree capability is internal")
        self._owner = owner
        self._git = git
        self._worktree = worktree
        self._policy = policy
        self._access = access
        self._live = True
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        try:
            sealed = object.__getattribute__(self, "_sealed")
        except AttributeError:
            sealed = False
        if sealed:
            raise AttributeError("worktree capability is immutable")
        object.__setattr__(self, name, value)

    def __getattribute__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError("worktree capability internals are private")
        return object.__getattribute__(self, name)

    def __repr__(self) -> str:
        live = object.__getattribute__(self, "_live")
        return "WorktreeCapability(live=True)" if live else "WorktreeCapability(live=False)"

    def revalidate(self) -> None:
        object.__getattribute__(self, "_require_live")()
        git = object.__getattribute__(self, "_git")
        access = object.__getattribute__(self, "_access")
        worktree = object.__getattribute__(self, "_worktree")
        policy = object.__getattribute__(self, "_policy")
        identity = worktree.identity
        if identity.run_id is None:
            raise ControlledGitError()
        try:
            expected = WorktreeIdentity.for_run(
                identity.project_id,
                identity.run_id,
                identity.branch,
                policy.database.enabled,
            )
        except TypeError, ValueError:
            raise ControlledGitError() from None
        if expected != identity:
            raise ControlledGitError()
        git._repository._verify_directory_access(access.normalized, access)
        git._validate_handle(worktree)
        if git._head_sha(worktree) != worktree.base_sha:
            raise ControlledGitError()
        git._verify_ancestor_sha(worktree, worktree.base_sha)

    def assert_ignored(self, relative_path: str) -> None:
        """Prove one policy-validated destination is ignored by Git."""

        object.__getattribute__(self, "_require_live")()
        policy = object.__getattribute__(self, "_policy")
        if relative_path not in policy.allowed_environment_files:
            raise ControlledGitError()
        self.revalidate()
        git = object.__getattribute__(self, "_git")
        worktree = object.__getattribute__(self, "_worktree")
        result = git._run(
            worktree.path,
            ("check-ignore", "--quiet", "--", relative_path),
            allow_return_codes=(0, 1),
        )
        if _return_code(result) != 0:
            raise ControlledGitError()
        self.revalidate()

    def publish(self, plan: EnvironmentStagingPlan) -> tuple[EnvironmentFileEvidence, ...]:
        object.__getattribute__(self, "_require_live")()
        from forge.tools.environment import publish_plan

        result = publish_plan(self, plan)
        return result

    def inspect(self, plan: EnvironmentStagingPlan) -> EnvironmentStagingInspection:
        object.__getattribute__(self, "_require_live")()
        from forge.tools.environment import inspect_plan

        return inspect_plan(self, plan)

    def _require_live(self) -> None:
        if not object.__getattribute__(self, "_live") or object.__getattribute__(
            self, "_owner"
        ) is not object.__getattribute__(self, "_git"):
            raise ControlledGitError()

    def _finish(self) -> None:
        object.__setattr__(self, "_live", False)


class ControlledGit:
    """Invoke a fixed Git executable against one exact managed repository.

    The constructor owns all process and configuration controls.  Public methods
    accept only a Forge-created ``ManagedWorktree`` handle; no method accepts an
    argv fragment, path, ref, or Git configuration override from a caller.
    """

    def __init__(
        self,
        repository: CanonicalRoot,
        *,
        default_branch: str,
        state_root: str | os.PathLike[str],
        git_executable: str | os.PathLike[str],
        runner: ProcessRunner | None = None,
    ) -> None:
        if not isinstance(repository, CanonicalRoot):
            raise TypeError("controlled git requires a canonical repository root")
        if runner is not None:
            runner_root = getattr(runner, "_root", None)
            if isinstance(runner_root, CanonicalRoot) and runner_root is not repository:
                raise TypeError("controlled git runner root does not match repository")
        _validate_branch(default_branch)
        self._repository = repository
        self._default_branch = default_branch
        self._managed_root = repository.path / ".worktrees"
        if os.path.lexists(self._managed_root):
            _reject_links(self._managed_root)
            if not self._managed_root.is_dir():
                raise ControlledGitError()

        self._git_executable = _resolve_git_executable(git_executable)
        self._staging_owner = object()
        self._state_root = _prepare_directory(Path(state_root))
        if _overlaps(self._state_root, repository.path):
            raise ControlledGitError()
        self._hooks_path = self._state_root / "hooks"
        self._global_config_path = self._state_root / "global.config"
        self._global_attributes_path = self._state_root / "global.attributes"
        _prepare_empty_directory(self._hooks_path)
        _prepare_empty_file(self._global_config_path)
        _prepare_empty_file(self._global_attributes_path)
        self._runner = runner or ProcessRunner(repository)

    @property
    def repository_path(self) -> Path:
        """Return the one canonical repository root owned by this adapter."""

        return self._repository.path

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        """Derive one exact managed handle without inspecting or mutating Git."""

        try:
            _validate_identity(identity)
            _validate_sha(base_sha)
            if _same_branch(identity.branch, self._default_branch):
                raise ControlledGitError()
            return ManagedWorktree(
                identity=identity,
                path=self._managed_root / identity.worktree_name,
                base_sha=base_sha,
            )
        except ControlledGitError:
            raise
        except TypeError, ValueError, OSError, RuntimeError:
            raise ControlledGitError() from None

    def inspect_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree | None:
        """Inspect one generated worktree and never repair or mutate it."""

        try:
            expected = self.expected_worktree(identity, base_sha)
            self._scan_local_config(self._repository.path)
            self._verify_branch_format(identity.branch)
            self._verify_managed_root_ignored()
            if os.path.lexists(self._managed_root):
                _reject_links(self._managed_root)
                if not self._managed_root.is_dir():
                    raise ControlledGitError()

            target_exists = os.path.lexists(expected.path)
            metadata = self._registration_metadata(identity)
            quarantine_metadata = self._registration_quarantine_metadata(identity)
            target_quarantine = self._managed_root / ".forge-quarantine" / identity.worktree_name
            if os.path.lexists(target_quarantine):
                _reject_links(target_quarantine)
                raise ControlledGitError()

            if target_exists:
                if metadata is None or quarantine_metadata is not None:
                    raise ControlledGitError()
                _reject_locked_registration(metadata)
                self._scan_local_config(expected.path)
                self._validate_handle(expected)
                self._head_sha(expected)
                if not self._is_ancestor(expected):
                    raise ControlledGitError()
                return expected

            if metadata is not None or quarantine_metadata is not None:
                raise ControlledGitError()
            if self._branch_exists_at(self._repository.path, identity.branch):
                raise ControlledGitError()
            return None
        except ControlledGitError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError, AttributeError:
            raise ControlledGitError() from None

    def create_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        """Create and verify one exact managed worktree without cleanup guesses."""

        if not isinstance(identity, WorktreeIdentity):
            raise ControlledGitError()
        _validate_sha(base_sha)
        try:
            _validate_branch(identity.branch)
        except TypeError, ValueError:
            raise ControlledGitError() from None
        if _same_branch(identity.branch, self._default_branch):
            raise ControlledGitError()
        self._scan_local_config(self._repository.path)
        self._verify_branch_format(identity.branch)
        resolved_base = self._parse_base_sha(base_sha)
        if resolved_base != base_sha:
            raise ControlledGitError()
        self._verify_managed_root_ignored()
        if self._branch_exists_at(self._repository.path, identity.branch):
            raise ControlledGitError()
        expected_metadata = self._registration_metadata(identity)
        if expected_metadata is not None:
            raise ControlledGitError()

        expected_path = self._managed_root / identity.worktree_name
        try:
            with self._repository._create_directory(".worktrees", identity.worktree_name) as access:
                if not self._repository._directory_access_is_empty(access):
                    raise ControlledGitError()
                self._run(
                    expected_path,
                    (
                        "worktree",
                        "add",
                        "-b",
                        identity.branch,
                        ".",
                        base_sha,
                    ),
                    omit_cwd_prefix=True,
                    git_directory=self._repository._git_directory_for_access(
                        f".worktrees/{identity.worktree_name}", access
                    ),
                )
                if not self._repository._directory_access_matches_path(access):
                    raise ControlledGitError()
                handle = ManagedWorktree(identity=identity, path=expected_path, base_sha=base_sha)
                self._validate_handle(handle)
                if self._head_sha(handle) != base_sha or not self._is_ancestor(handle):
                    raise ControlledGitError()
                return handle
        except ControlledGitError:
            raise
        except OSError, RuntimeError, TypeError, ValueError:
            raise ControlledGitError() from None

    def remove_worktree(self, worktree: ManagedWorktree) -> None:
        """Remove one exact registered worktree and retain its branch."""

        try:
            identity, expected_path = self._validate_handle_shape(worktree)
            metadata = self._registration_metadata(identity)
            target_exists = os.path.lexists(expected_path)
            if target_exists:
                if metadata is None:
                    raise ControlledGitError()
                self._remove_live_worktree(
                    worktree,
                    identity,
                    expected_path,
                    metadata.name,
                )
            elif metadata is not None:
                self._remove_stale_registration(
                    identity,
                    expected_path,
                    metadata.name,
                )
            else:
                self._remove_absent_worktree(identity, expected_path)
        except ControlledGitError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError, AttributeError:
            raise ControlledGitError() from None

    def _remove_live_worktree(
        self,
        worktree: ManagedWorktree,
        identity: WorktreeIdentity,
        expected_path: Path,
        preflight_basename: str,
    ) -> None:
        """Validate a live target, then quarantine it without reopening its path."""

        with self._repository._prepare_worktree_quarantine(
            identity.worktree_name, preflight_basename
        ) as access:
            metadata = self._registration_metadata(identity)
            if metadata is None or metadata.name != preflight_basename:
                raise ControlledGitError()
            if self._registration_quarantine_metadata(identity) is not None:
                raise ControlledGitError()
            _reject_links(expected_path)
            if not expected_path.is_dir():
                raise ControlledGitError()
            self._scan_local_config(self._repository.path)
            self._scan_local_config(expected_path)
            self._verify_branch_format(identity.branch)
            self._verify_registration(expected_path, identity)
            self._verify_current_branch(worktree)
            self._verify_branch_exists(identity.branch)

            self._repository._bind_worktree_quarantine(access)
            self._repository._quarantine_target(access)
            self._repository._delete_target_quarantine(access)
            self._repository._quarantine_registration(access)
            self._repository._delete_registration_quarantine(access)
            self._repository._verify_worktree_removal_state(access)
            self._verify_removal_absent(
                identity,
                expected_path,
                preflight_basename,
            )
            self._verify_branch_exists(identity.branch)

    def _remove_stale_registration(
        self,
        identity: WorktreeIdentity,
        expected_path: Path,
        preflight_basename: str,
    ) -> None:
        """Remove one exact stale registration while its target stays absent."""

        with self._repository._open_stale_registration_quarantine(
            identity.worktree_name, preflight_basename
        ) as access:
            metadata = self._registration_metadata(identity)
            if metadata is None or metadata.name != preflight_basename:
                raise ControlledGitError()
            if self._registration_quarantine_metadata(identity) is not None:
                raise ControlledGitError()
            self._verify_metadata_target(metadata, expected_path / ".git")
            self._scan_local_config(self._repository.path)
            self._verify_branch_format(identity.branch)
            self._verify_branch_exists(identity.branch)

            self._repository._quarantine_registration(access)
            self._repository._delete_registration_quarantine(access)
            self._verify_removal_absent(
                identity,
                expected_path,
                preflight_basename,
            )
            self._verify_branch_exists(identity.branch)

    def _remove_absent_worktree(
        self,
        identity: WorktreeIdentity,
        expected_path: Path,
    ) -> None:
        """Prove a fully absent handle is safe to treat as an idempotent success."""

        with self._repository._inspect_absent_worktree_removal(identity.worktree_name) as access:
            del access
            if self._registration_metadata(identity) is not None:
                raise ControlledGitError()
            if self._registration_quarantine_metadata(identity) is not None:
                raise ControlledGitError()
            self._scan_local_config(self._repository.path)
            self._verify_branch_format(identity.branch)
            self._verify_branch_exists(identity.branch)
            self._verify_removal_absent(identity, expected_path, None)
            self._verify_branch_exists(identity.branch)

    def _verify_removal_absent(
        self,
        identity: WorktreeIdentity,
        expected_path: Path,
        registration_basename: str | None,
    ) -> None:
        """Verify exact lifecycle evidence has disappeared without guessing paths."""

        if os.path.lexists(expected_path):
            raise ControlledGitError()
        if self._registration_metadata(identity) is not None:
            raise ControlledGitError()
        target_quarantine = self._managed_root / ".forge-quarantine" / identity.worktree_name
        if os.path.lexists(target_quarantine):
            raise ControlledGitError()
        if registration_basename is not None:
            registration_quarantine = (
                self._repository.path
                / ".git"
                / ".forge-worktree-quarantine"
                / registration_basename
            )
            if os.path.lexists(registration_quarantine):
                raise ControlledGitError()
        if self._registration_quarantine_metadata(identity) is not None:
            raise ControlledGitError()

    def prune(self) -> None:
        """Prune only stale Git worktree registration metadata."""

        self._scan_local_config(self._repository.path)
        self._run(self._repository.path, ("worktree", "prune", "--expire=now"))

    def status(self, worktree: ManagedWorktree) -> GitStatus:
        """Return bounded deterministic porcelain-v1 status output."""

        self._validate_handle(worktree, verify_branch=False)
        self._scan_local_config(worktree.path)
        self._verify_current_branch(worktree)
        result = self._run(
            worktree.path,
            ("status", "--porcelain=v1", "--branch", "--untracked-files=all", "-z", "--"),
        )
        return GitStatus(
            text=result.stdout,
            original_byte_count=_original_count(result, "stdout"),
            truncated=_truncated(result, "stdout"),
        )

    def diff(self, worktree: ManagedWorktree) -> GitDiff:
        """Return bounded binary-safe diff output against the worktree HEAD."""

        self._validate_handle(worktree, verify_branch=False)
        self._scan_local_config(worktree.path)
        self._verify_current_branch(worktree)
        result = self._run(
            worktree.path,
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                "--no-color",
                "HEAD",
                "--",
            ),
        )
        return GitDiff(
            text=result.stdout,
            original_byte_count=_original_count(result, "stdout"),
            truncated=_truncated(result, "stdout"),
        )

    def commit(self, worktree: ManagedWorktree, message: str) -> GitCommit:
        """Stage and create one verified local commit for an exact worktree."""

        try:
            commit_message = _validate_commit_message(message)
            identity, _expected = self._validate_handle_shape(worktree)
            self._assert_trusted_state()
            self._validate_handle(worktree)
            registration = self._registration_metadata(identity)
            if registration is None:
                raise ControlledGitError()
            with self._repository._open_managed_worktree(
                identity.worktree_name, registration.name
            ) as access:
                if not self._repository._directory_access_matches_path(access):
                    raise ControlledGitError()
                return self._commit_bound(worktree, commit_message)
        except ControlledGitError:
            raise
        except (
            OSError,
            RepositoryAccessDenied,
            RuntimeError,
            TypeError,
            ValueError,
            AttributeError,
        ):
            raise ControlledGitError() from None

    @contextlib.contextmanager
    def open_worktree_capability(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, *, read_only: bool = False
    ) -> Iterator[WorktreeCapability]:
        """Retain one exact Git registration and target for environment staging."""

        caller_failed = False
        try:
            if not isinstance(policy, ProjectPolicy):
                raise ControlledGitError()
            configured = Path(policy.repository_path)
            if configured.resolve(strict=True) != self._repository.path:
                raise ControlledGitError()
            self._validate_handle(worktree)
            if (worktree.identity.database_name is not None) != policy.database.enabled:
                raise ControlledGitError()
            registration = self._registration_metadata(worktree.identity)
            if registration is None:
                raise ControlledGitError()
            with self._repository._open_managed_worktree(
                worktree.identity.worktree_name,
                registration.name,
                create_lock=not read_only,
            ) as access:
                if not self._repository._directory_access_matches_path(access):
                    raise ControlledGitError()
                capability = WorktreeCapability(
                    seal=_CAPABILITY_SEAL,
                    owner=self,
                    git=self,
                    worktree=worktree,
                    policy=policy,
                    access=access,
                )
                operation_failed = False
                try:
                    capability.revalidate()
                    try:
                        yield capability
                    except BaseException:
                        caller_failed = True
                        raise
                finally:
                    try:
                        try:
                            capability.revalidate()
                        except Exception:
                            # Preserve the operation's stable failure category
                            # when release proof itself encounters a stale
                            # target; a successful operation still fails closed.
                            if not operation_failed:
                                raise
                    finally:
                        object.__getattribute__(capability, "_finish")()
        except ControlledGitError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
            if caller_failed:
                raise
            raise ControlledGitError() from None

    def _commit_bound(self, worktree: ManagedWorktree, message: str) -> GitCommit:
        self._validate_handle(worktree)
        self._scan_local_config(worktree.path)
        previous_sha = self._head_sha(worktree)
        self._verify_ancestor_sha(worktree, worktree.base_sha)

        self._run(worktree.path, ("add", "-A", "--"))
        self._require_staged_changes(worktree)

        # Revalidate the registration, safety configuration, branch, HEAD, and
        # base ancestry immediately before the mutation that creates the commit.
        self._validate_handle(worktree)
        self._scan_local_config(worktree.path)
        if self._head_sha(worktree) != previous_sha:
            raise ControlledGitError()
        self._verify_ancestor_sha(worktree, worktree.base_sha)
        self._require_staged_changes(worktree)

        result = self._run(
            worktree.path,
            ("commit", "--no-verify", "--no-gpg-sign", "-m", message, "--"),
        )
        _require_complete_result(result)

        new_sha = self._head_sha(worktree)
        if new_sha == previous_sha:
            raise ControlledGitError()
        self._validate_handle(worktree)
        self._verify_ancestor_sha(worktree, previous_sha)
        self._verify_ancestor_sha(worktree, worktree.base_sha)
        if self._head_sha(worktree) != new_sha:
            raise ControlledGitError()
        return GitCommit(previous_sha=previous_sha, new_sha=new_sha)

    def _require_staged_changes(self, worktree: ManagedWorktree) -> None:
        result = self._run(
            worktree.path,
            ("diff", "--cached", "--quiet", "--"),
            allow_return_codes=(0, 1),
        )
        _require_complete_result(result)
        if _return_code(result) != 1:
            raise ControlledGitError()

    def branch_exists(self, worktree: ManagedWorktree) -> bool:
        """Return whether the handle's exact branch exists locally."""

        self._validate_handle(worktree)
        result = self._run(
            worktree.path,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{worktree.identity.branch}"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def current_branch(self, worktree: ManagedWorktree) -> str:
        """Return the handle's recorded branch after exact-path validation."""

        self._validate_handle(worktree, verify_branch=False)
        return self._verify_current_branch(worktree)

    def head_sha(self, worktree: ManagedWorktree) -> str:
        """Return the lowercase, complete HEAD commit SHA."""

        self._validate_handle(worktree)
        return self._head_sha(worktree)

    def _head_sha(self, worktree: ManagedWorktree) -> str:
        result = self._run(worktree.path, ("rev-parse", "--verify", "HEAD^{commit}"))
        return _parse_sha(result)

    def is_ancestor(self, worktree: ManagedWorktree) -> bool:
        """Return whether an exact commit is an ancestor of the handle's HEAD."""

        self._validate_handle(worktree)
        return self._is_ancestor(worktree)

    def _is_ancestor(self, worktree: ManagedWorktree) -> bool:
        return self._has_ancestor(worktree, worktree.base_sha)

    def _verify_ancestor_sha(self, worktree: ManagedWorktree, ancestor: str) -> None:
        if not self._has_ancestor(worktree, ancestor):
            raise ControlledGitError()

    def _has_ancestor(self, worktree: ManagedWorktree, ancestor: str) -> bool:
        _validate_sha(ancestor)
        result = self._run(
            worktree.path,
            ("merge-base", "--is-ancestor", ancestor, "HEAD"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def _validate_handle_shape(self, worktree: ManagedWorktree) -> tuple[WorktreeIdentity, Path]:
        if not isinstance(worktree, ManagedWorktree):
            raise ControlledGitError()
        identity = worktree.identity
        if not isinstance(identity, WorktreeIdentity):
            raise ControlledGitError()
        try:
            _validate_branch(identity.branch)
        except TypeError, ValueError:
            raise ControlledGitError() from None
        if _same_branch(identity.branch, self._default_branch):
            raise ControlledGitError()
        _validate_worktree_component(identity.worktree_name)
        expected = self._managed_root / identity.worktree_name
        if _path_key(worktree.path) != _path_key(expected):
            raise ControlledGitError()
        _validate_sha(worktree.base_sha)
        return identity, expected

    def _validate_handle(
        self,
        worktree: ManagedWorktree,
        *,
        verify_branch: bool = True,
    ) -> None:
        identity, expected = self._validate_handle_shape(worktree)
        try:
            _reject_links(worktree.path)
            if not worktree.path.is_dir() or _path_key(worktree.path) != _path_key(expected):
                raise ControlledGitError()
            self._verify_registration(worktree.path, identity)
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None
        if verify_branch:
            self._verify_current_branch(worktree)

    def _verify_current_branch(self, worktree: ManagedWorktree) -> str:
        result = self._run(worktree.path, ("branch", "--show-current"))
        branch = _parse_single_line(result)
        if branch != worktree.identity.branch:
            raise ControlledGitError()
        return branch

    def _scan_local_config(self, worktree: Path) -> None:
        """Refuse local settings that could execute repository-controlled code."""

        result = self._run(
            worktree,
            ("config", "--local", "--no-includes", "--name-only", "--null", "--list"),
        )
        if _truncated(result, "stdout") or _truncated(result, "stderr"):
            raise ControlledGitError()
        output = result.stdout
        if not isinstance(output, str) or "\ufffd" in output:
            raise ControlledGitError()
        keys = output.split("\x00")
        if keys and keys[-1] == "":
            keys.pop()
        if any(not key or _unsafe_local_key(key) for key in keys):
            raise ControlledGitError()

    def _run(
        self,
        worktree: Path,
        arguments: Sequence[str],
        *,
        allow_return_codes: tuple[int, ...] = (0,),
        omit_cwd_prefix: bool = False,
        git_directory: str | None = None,
    ) -> ProcessResult:
        self._assert_trusted_state()
        normalized: str | None = None
        active_access = None
        try:
            relative = Path(worktree).relative_to(self._repository.path)
        except ValueError:
            pass
        else:
            normalized = self._repository.normalize(relative, allow_root=True)
            active_access = self._repository._active_directory_access(normalized)
        launch_worktree = worktree
        environment = self._environment(git_directory=git_directory)
        if active_access is not None:
            if normalized is None:
                raise ControlledGitError()
            bound_worktree = self._repository._launch_path_for_access(
                normalized, active_access, require_fd=True
            )
            launch_worktree = Path(bound_worktree)
            if active_access.registration_path is not None:
                environment = self._environment(
                    git_directory=self._repository._git_directory_for_access(
                        normalized, active_access
                    ),
                    git_common_directory=self._repository._git_common_directory_for_access(
                        normalized, active_access
                    ),
                    git_work_tree=self._repository._git_work_tree_for_access(
                        normalized, active_access
                    ),
                )
        argv = [
            *self._prefix(launch_worktree, include_cwd=not omit_cwd_prefix),
            *arguments,
        ]
        cwd = str(worktree)
        try:
            result = self._runner.run_argv(
                argv,
                cwd=cwd,
                environment=environment,
            )
        except OSError, RuntimeError, TypeError, ValueError:
            raise ControlledGitError() from None
        if not _valid_result(result):
            raise ControlledGitError()
        return_code = _return_code(result)
        if return_code not in allow_return_codes:
            raise ControlledGitError()
        if _timed_out(result) or return_code is None:
            raise ControlledGitError()
        return result

    def _prefix(self, worktree: Path, *, include_cwd: bool = True) -> tuple[str, ...]:
        prefix = [str(self._git_executable)]
        if include_cwd:
            prefix.extend(("-C", str(worktree)))
        prefix.extend(
            (
                "--no-pager",
                "-c",
                f"core.hooksPath={self._hooks_path}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                "-c",
                "credential.helper=",
                "-c",
                "credential.interactive=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "diff.external=",
                "-c",
                f"core.attributesFile={self._global_attributes_path}",
                "-c",
                f"user.name={_FORGE_NAME}",
                "-c",
                f"user.email={_FORGE_EMAIL}",
            )
        )
        return tuple(prefix)

    def _environment(
        self,
        *,
        git_directory: str | None = None,
        git_common_directory: str | None = None,
        git_work_tree: str | None = None,
    ) -> dict[str, str]:
        allowed_names = {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMP",
            "TEMP",
            "TMPDIR",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
        }
        environment: dict[str, str] = {}
        for key, value in os.environ.items():
            comparison = key.upper() if os.name == "nt" else key
            if comparison not in allowed_names:
                continue
            if not isinstance(value, str) or "\x00" in value:
                continue
            environment[key] = value
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": str(self._global_config_path),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_PAGER": "",
                "GIT_EDITOR": "",
            }
        )
        common_directory = (
            git_common_directory if git_common_directory is not None else git_directory
        )
        if git_directory is not None:
            environment["GIT_DIR"] = git_directory
        if common_directory is not None:
            environment["GIT_COMMON_DIR"] = common_directory
        if git_work_tree is not None:
            environment["GIT_WORK_TREE"] = git_work_tree
        return environment

    def _assert_trusted_state(self) -> None:
        try:
            _reject_links(self._state_root)
            _reject_links(self._hooks_path)
            if not self._hooks_path.is_dir() or any(self._hooks_path.iterdir()):
                raise ControlledGitError()
            for path in (self._global_config_path, self._global_attributes_path):
                _reject_links(path)
                if not path.is_file() or path.stat().st_size != 0:
                    raise ControlledGitError()
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None

    def _verify_branch_format(self, branch: str) -> None:
        self._run(self._repository.path, ("check-ref-format", "--branch", branch))

    def _parse_base_sha(self, base_sha: str) -> str:
        result = self._run(
            self._repository.path,
            ("rev-parse", "--verify", f"{base_sha}^{{commit}}"),
        )
        return _parse_sha(result)

    def _verify_managed_root_ignored(self) -> None:
        self._run(self._repository.path, ("check-ignore", "--quiet", "--", ".worktrees/"))

    def _branch_exists_at(self, worktree: Path, branch: str) -> bool:
        result = self._run(
            worktree,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
            allow_return_codes=(0, 1),
        )
        return _return_code(result) == 0

    def _verify_branch_exists(self, branch: str) -> None:
        if not self._branch_exists_at(self._repository.path, branch):
            raise ControlledGitError()

    def _registration_metadata(self, identity: WorktreeIdentity) -> Path | None:
        git_directory = self._repository.path / ".git"
        if os.path.lexists(git_directory):
            _reject_links(git_directory)
            if not git_directory.is_dir():
                raise ControlledGitError()
        metadata_root = git_directory / "worktrees"
        if not os.path.lexists(metadata_root):
            return None
        _reject_links(metadata_root)
        if not metadata_root.is_dir():
            raise ControlledGitError()
        expected_target = self._managed_root / identity.worktree_name / ".git"
        expected_target = _canonical_no_links_allow_missing(expected_target)
        matches: list[Path] = []
        try:
            for index, metadata in enumerate(metadata_root.iterdir()):
                if index >= _MAX_METADATA_ENTRIES:
                    raise ControlledGitError()
                _reject_links(metadata)
                metadata_stat = os.stat(metadata, follow_symlinks=False)
                if not stat.S_ISDIR(metadata_stat.st_mode):
                    raise ControlledGitError()
                target = _read_metadata_target(metadata)
                if _path_key(target) != _path_key(expected_target):
                    continue
                matches.append(_canonical_no_links(metadata))
                if len(matches) > 1:
                    raise ControlledGitError()
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None
        return matches[0] if matches else None

    def _registration_quarantine_metadata(self, identity: WorktreeIdentity) -> Path | None:
        """Find exact target proof in registration quarantine, leaving foreign entries alone."""

        git_directory = self._repository.path / ".git"
        if not os.path.lexists(git_directory):
            return None
        _reject_links(git_directory)
        if not git_directory.is_dir():
            raise ControlledGitError()
        quarantine_root = git_directory / ".forge-worktree-quarantine"
        if not os.path.lexists(quarantine_root):
            return None
        _reject_links(quarantine_root)
        if not quarantine_root.is_dir():
            raise ControlledGitError()

        expected_target = self._managed_root / identity.worktree_name / ".git"
        expected_target = _canonical_no_links_allow_missing(expected_target)
        matches: list[Path] = []
        try:
            for index, metadata in enumerate(quarantine_root.iterdir()):
                if index >= _MAX_METADATA_ENTRIES:
                    raise ControlledGitError()
                _reject_links(metadata)
                metadata_stat = os.stat(metadata, follow_symlinks=False)
                if not stat.S_ISDIR(metadata_stat.st_mode):
                    raise ControlledGitError()
                target = _read_metadata_target(metadata)
                if _path_key(target) != _path_key(expected_target):
                    continue
                matches.append(_canonical_no_links(metadata))
                if len(matches) > 1:
                    raise ControlledGitError()
        except ControlledGitError:
            raise
        except OSError, RuntimeError, ValueError:
            raise ControlledGitError() from None
        return matches[0] if matches else None

    def _verify_registration(self, worktree: Path, identity: WorktreeIdentity) -> None:
        git_marker = worktree / ".git"
        _reject_links(git_marker)
        if not git_marker.is_file():
            raise ControlledGitError()
        marker = _read_small_text(git_marker)
        if not marker.startswith("gitdir: ") or marker.count("\n") != 1:
            raise ControlledGitError()
        raw_metadata = marker.removesuffix("\n")[8:]
        metadata = Path(raw_metadata)
        if not metadata.is_absolute():
            metadata = worktree / metadata
        metadata = _canonical_no_links(metadata)
        expected = self._registration_metadata(identity)
        if expected is None or _path_key(metadata) != _path_key(expected):
            raise ControlledGitError()
        self._verify_metadata_target(metadata, git_marker)

    def _verify_metadata_target(self, metadata: Path, expected_target: Path) -> None:
        target = _read_metadata_target(metadata)
        expected_target = _canonical_no_links_allow_missing(expected_target)
        if _path_key(target) != _path_key(expected_target):
            raise ControlledGitError()


def _read_metadata_target(metadata: Path) -> Path:
    metadata_gitdir = metadata / "gitdir"
    _reject_links(metadata_gitdir)
    try:
        metadata_stat = os.stat(metadata_gitdir, follow_symlinks=False)
    except OSError, ValueError:
        raise ControlledGitError() from None
    if not stat.S_ISREG(metadata_stat.st_mode):
        raise ControlledGitError()
    record = _read_small_text(metadata_gitdir)
    if not record.endswith("\n") or record.count("\n") != 1:
        raise ControlledGitError()
    registered_target = record.removesuffix("\n")
    if not registered_target or "\r" in registered_target:
        raise ControlledGitError()
    target = Path(registered_target)
    if not target.is_absolute():
        target = metadata / target
    return _canonical_no_links_allow_missing(target)


def _resolve_git_executable(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(os.fspath(value))
    except TypeError, ValueError:
        raise ControlledGitError() from None
    if not path.is_absolute():
        raise ControlledGitError()
    try:
        _reject_links(path)
        resolved = path.resolve(strict=True)
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError, RuntimeError, ValueError:
        raise ControlledGitError() from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ControlledGitError()
    return resolved


def _prepare_directory(path: Path) -> Path:
    if not path.is_absolute() or not path.anchor:
        raise ControlledGitError()
    try:
        _ensure_directory(path)
        _reject_links(path)
        return path.resolve(strict=True)
    except OSError, RuntimeError, ValueError:
        raise ControlledGitError() from None


def _ensure_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ControlledGitError()
        current = parent
    _reject_links(current)
    if not current.is_dir():
        raise ControlledGitError()
    for candidate in reversed(missing):
        candidate.mkdir()
        _reject_links(candidate)
        if not candidate.is_dir():
            raise ControlledGitError()


def _prepare_empty_directory(path: Path) -> None:
    if os.path.lexists(path):
        _reject_links(path)
        if not path.is_dir() or any(path.iterdir()):
            raise ControlledGitError()
        return
    path.mkdir()
    _reject_links(path)


def _prepare_empty_file(path: Path) -> None:
    if os.path.lexists(path):
        _reject_links(path)
        if not path.is_file() or path.stat().st_size != 0:
            raise ControlledGitError()
        return
    try:
        with path.open("xb"):
            pass
    except OSError, ValueError:
        raise ControlledGitError() from None
    _reject_links(path)


def _reject_links(path: Path) -> None:
    current = Path(path.anchor)
    if not current:
        raise ControlledGitError()
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise ControlledGitError() from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ControlledGitError()


def _reject_existing_links(path: Path) -> None:
    current = Path(path.anchor)
    if not current:
        raise ControlledGitError()
    for component in path.parts[1:]:
        current /= component
        if not os.path.lexists(current):
            break
        try:
            metadata = os.lstat(current)
        except OSError, ValueError:
            raise ControlledGitError() from None
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ControlledGitError()


def _canonical_no_links(path: Path) -> Path:
    _reject_links(path)
    return path.resolve(strict=True)


def _canonical_no_links_allow_missing(path: Path) -> Path:
    _reject_existing_links(path)
    return path.resolve(strict=False)


def _read_small_text(path: Path) -> str:
    try:
        value = path.read_bytes()
    except OSError, ValueError:
        raise ControlledGitError() from None
    if len(value) > _MAX_METADATA_BYTES:
        raise ControlledGitError()
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ControlledGitError() from None


def _validate_branch(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_BRANCH_LENGTH
        or value.startswith(("-", "/"))
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or ".." in value
        or "@{" in value
        or value.endswith((".", "/"))
        or "//" in value
    ):
        raise ValueError("invalid branch")


def _same_branch(first: str, second: str) -> bool:
    return first.casefold() == second.casefold() if os.name == "nt" else first == second


def _validate_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ControlledGitError()


def _validate_identity(identity: object) -> None:
    if not isinstance(identity, WorktreeIdentity):
        raise ControlledGitError()
    try:
        if identity.run_id is None:
            expected = WorktreeIdentity.for_developer(
                identity.project_id,
                identity.branch,
                identity.database_name is not None,
            )
        else:
            expected = WorktreeIdentity.for_run(
                identity.project_id,
                identity.run_id,
                identity.branch,
                identity.database_name is not None,
            )
    except TypeError, ValueError:
        raise ControlledGitError() from None
    if expected != identity:
        raise ControlledGitError()


def _reject_locked_registration(metadata: Path) -> None:
    locked = metadata / "locked"
    if not os.path.lexists(locked):
        return
    try:
        _reject_links(locked)
        locked_stat = os.stat(locked, follow_symlinks=False)
    except OSError, RuntimeError, ValueError:
        raise ControlledGitError() from None
    if not stat.S_ISREG(locked_stat.st_mode):
        raise ControlledGitError()
    raise ControlledGitError()


def _validate_worktree_component(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(character in value for character in ("/", "\\", "\x00", ":"))
        or len(os.fsencode(value)) > 255
        or (os.name == "nt" and value.rstrip(" .") != value)
    ):
        raise ControlledGitError()


def _path_key(path: Path) -> str:
    value = str(path).replace("\\", "/").rstrip("/")
    if not value:
        value = "/"
    return value.casefold() if os.name == "nt" else value


def _overlaps(first: Path, second: Path) -> bool:
    first_key = _path_key(first)
    second_key = _path_key(second)
    return (
        first_key == second_key
        or first_key.startswith(second_key + "/")
        or second_key.startswith(first_key + "/")
    )


def _unsafe_local_key(key: str) -> bool:
    lowered = key.casefold()
    blocked_fragments = (
        "include",
        "hook",
        "filter",
        "fsmonitor",
        "untrackedcache",
        "external",
        "textconv",
        "credential",
        "pager",
        "editor",
        "askpass",
        "ssh",
        "proxy",
        "attributesfile",
        "diff.filter",
        "interactive.difffilter",
    )
    return any(fragment in lowered for fragment in blocked_fragments)


def _validate_commit_message(value: object) -> str:
    if not isinstance(value, str):
        raise ControlledGitError()
    if len(value.encode("utf-8")) > _MAX_COMMIT_MESSAGE_BYTES:
        raise ControlledGitError()
    if any(
        character == "\x7f"
        or ord(character) < 0x20
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    ):
        raise ControlledGitError()
    message = value.strip()
    if not message or len(message.encode("utf-8")) > _MAX_COMMIT_MESSAGE_BYTES:
        raise ControlledGitError()
    return message


def _require_complete_result(result: ProcessResult) -> None:
    if (
        _truncated(result, "stdout")
        or _truncated(result, "stderr")
        or "\ufffd" in result.stdout
        or "\ufffd" in result.stderr
    ):
        raise ControlledGitError()


def _valid_result(result: object) -> bool:
    return (
        hasattr(result, "stdout")
        and hasattr(result, "stderr")
        and isinstance(result.stdout, str)
        and isinstance(result.stderr, str)
    )


def _return_code(result: object) -> int | None:
    value = getattr(result, "return_code", getattr(result, "returncode", None))
    return value if type(value) is int or value is None else None


def _timed_out(result: object) -> bool:
    value = getattr(result, "timed_out", False)
    return value is True


def _truncated(result: object, stream: str) -> bool:
    value = getattr(result, f"{stream}_truncated", False)
    return value is True


def _original_count(result: object, stream: str) -> int:
    value = getattr(result, f"{stream}_original_byte_count", None)
    if type(value) is int and value >= 0:
        return value
    text = getattr(result, stream)
    return len(text.encode("utf-8"))


def _parse_sha(result: ProcessResult) -> str:
    if _truncated(result, "stdout") or _truncated(result, "stderr"):
        raise ControlledGitError()
    value = _parse_single_line(result)
    _validate_sha(value)
    return value


def _parse_single_line(result: ProcessResult) -> str:
    if _truncated(result, "stdout") or _truncated(result, "stderr"):
        raise ControlledGitError()
    output = result.stdout
    if not isinstance(output, str):
        raise ControlledGitError()
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise ControlledGitError()
    if any(ord(character) < 0x20 for character in lines[0]):
        raise ControlledGitError()
    return lines[0]


__all__ = ["ControlledGit", "ControlledGitError", "WorktreeCapability"]
