"""Capability-bound protected environment-file staging."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, cast
from uuid import UUID
from weakref import WeakKeyDictionary

from forge.application.ports.worktrees import (
    _STAGING_PLAN_SEAL,
    DatabaseBinding,
    EnvironmentFileEvidence,
    EnvironmentStagingInspection,
    EnvironmentStagingPlan,
)
from forge.domain.paths import normalize_policy_paths
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.resource import ResourceState
from forge.tools.paths import (
    CanonicalRoot,
    RepositoryAccessDenied,
    _open_staging_parent,
    _publish_staging,
    _read_staging,
    _staging_digests,
)

if TYPE_CHECKING:
    from forge.tools.git import WorktreeCapability


_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_FILE_COUNT = 128
_READ_CHUNK_BYTES = 64 * 1024
_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT_PREFIX = re.compile(r"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?")
_ERROR = "environment staging failed"
_RECONCILIATION = "environment staging requires reconciliation"
_INTEGRITY = "environment staging identity is invalid"


class EnvironmentStagingError(RuntimeError):
    """Stable, redacted environment staging failure."""

    def __init__(self, message: str = _ERROR) -> None:
        super().__init__(message)
        self.__cause__ = None
        self.__context__ = None
        self.__traceback__ = None


class EnvironmentReconciliationRequired(EnvironmentStagingError):
    """The destination set is partial, unsafe, or otherwise ambiguous."""

    def __init__(self) -> None:
        super().__init__(_RECONCILIATION)


@dataclass(frozen=True, slots=True, kw_only=True)
class _StagedFile:
    path: str
    source: bytes
    output: bytes
    evidence: EnvironmentFileEvidence


@dataclass(frozen=True, slots=True, weakref_slot=True, kw_only=True)
class _PlanRecord:
    token: object
    stager_owner: object
    git_owner: object
    identity: object
    policy_id: UUID
    policy_version: int
    files: tuple[_StagedFile, ...]
    evidence: tuple[EnvironmentFileEvidence, ...]


_PLAN_RECORDS: WeakKeyDictionary[EnvironmentStagingPlan, _PlanRecord] = WeakKeyDictionary()
_PLAN_RECORDS_LOCK = RLock()


class EnvironmentStager:
    """Build, publish, and inspect one exact worktree-local environment plan."""

    def __init__(self, controlled_git: object) -> None:
        if not hasattr(controlled_git, "open_worktree_capability") or not hasattr(
            controlled_git, "repository_path"
        ):
            raise TypeError("environment staging requires controlled Git")
        self._git = controlled_git

    def build_plan(
        self,
        worktree: object,
        policy: ProjectPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int | None = None,
    ) -> EnvironmentStagingPlan:
        version = _validate_policy(policy, policy_version)
        if not hasattr(worktree, "identity"):
            raise EnvironmentStagingError(_INTEGRITY)
        validation_failed = False
        try:
            paths = normalize_policy_paths(policy.allowed_environment_files)
            if paths != tuple(policy.allowed_environment_files):
                raise EnvironmentStagingError(_INTEGRITY)
            _validate_binding(worktree.identity, policy, resource)
            root = CanonicalRoot(policy.repository_path)
            if root.path != Path(self._git.repository_path):
                raise EnvironmentStagingError(_INTEGRITY)
        except EnvironmentStagingError:
            raise
        except OSError, RepositoryAccessDenied, TypeError, ValueError, RuntimeError:
            validation_failed = True
        if validation_failed:
            raise EnvironmentStagingError(_INTEGRITY)

        files: list[_StagedFile] = []
        total = 0
        staging_failed = False
        try:
            if len(paths) > _MAX_FILE_COUNT:
                raise EnvironmentStagingError(_ERROR)
            with self._git.open_worktree_capability(worktree, policy) as capability:
                for relative_path in paths:
                    capability.revalidate()
                    source = _read_source(root, relative_path)
                    output = _transform(source, resource, policy)
                    if len(output) > _MAX_FILE_BYTES:
                        raise EnvironmentStagingError(_ERROR)
                    total += len(output)
                    if total > _MAX_TOTAL_BYTES:
                        raise EnvironmentStagingError(_ERROR)
                    path_digest, source_digest, output_digest = _staging_digests(
                        relative_path, source, output
                    )
                    files.append(
                        _StagedFile(
                            path=relative_path,
                            source=source,
                            output=output,
                            evidence=EnvironmentFileEvidence(
                                path_digest=path_digest,
                                source_digest=source_digest,
                                output_digest=output_digest,
                                byte_count=len(output),
                            ),
                        )
                    )
                    capability.revalidate()
        except EnvironmentStagingError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
            staging_failed = True
        if staging_failed:
            raise EnvironmentStagingError(_ERROR)
        evidence = tuple(item.evidence for item in files)
        token = object()
        plan = EnvironmentStagingPlan(
            seal=_STAGING_PLAN_SEAL,
            token=token,
            evidence=evidence,
        )
        record = _PlanRecord(
            token=token,
            stager_owner=self,
            git_owner=self._git,
            identity=worktree.identity,
            policy_id=policy.id,
            policy_version=version,
            files=tuple(files),
            evidence=evidence,
        )
        with _PLAN_RECORDS_LOCK:
            _PLAN_RECORDS[plan] = record
        return plan

    def publish(
        self,
        worktree: object,
        policy: ProjectPolicy,
        plan: EnvironmentStagingPlan,
    ) -> tuple[EnvironmentFileEvidence, ...]:
        _validate_policy(policy, None)
        _assert_plan_stager(plan, self)
        publish_failed = False
        try:
            with self._git.open_worktree_capability(worktree, policy) as capability:
                typed_capability = cast("WorktreeCapability", capability)
                return typed_capability.publish(plan)
        except EnvironmentStagingError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
            publish_failed = True
        if publish_failed:
            raise EnvironmentStagingError(_ERROR)
        raise AssertionError("environment publication failure path was not reached")

    def inspect(
        self,
        worktree: object,
        policy: ProjectPolicy,
        plan: EnvironmentStagingPlan,
    ) -> EnvironmentStagingInspection:
        _validate_policy(policy, None)
        _assert_plan_stager(plan, self)
        inspect_failed = False
        try:
            with self._git.open_worktree_capability(worktree, policy, read_only=True) as capability:
                typed_capability = cast("WorktreeCapability", capability)
                return typed_capability.inspect(plan)
        except EnvironmentStagingError:
            raise
        except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
            inspect_failed = True
        if inspect_failed:
            raise EnvironmentStagingError(_ERROR)
        raise AssertionError("environment inspection failure path was not reached")


WorktreeEnvironmentStager = EnvironmentStager
EnvironmentFileStager = EnvironmentStager


def _assert_plan_stager(plan: EnvironmentStagingPlan, stager: EnvironmentStager) -> None:
    if not isinstance(plan, EnvironmentStagingPlan):
        raise EnvironmentStagingError(_INTEGRITY)
    with _PLAN_RECORDS_LOCK:
        record = _PLAN_RECORDS.get(plan)
    if record is None or record.stager_owner is not stager:
        raise EnvironmentStagingError(_INTEGRITY)


def _publish_plan(
    capability: WorktreeCapability,
    plan: EnvironmentStagingPlan,
) -> tuple[EnvironmentFileEvidence, ...]:
    policy = object.__getattribute__(capability, "_policy")
    git = object.__getattribute__(capability, "_git")
    publish_failed = False
    try:
        record = _resolve_plan(capability, plan)
        files = record.files
        _verify_plan_record(record)
        require_acl = policy.runner_mode is RunnerMode.DOCKER
        capability.revalidate()
        present = _inspect_destination_set(capability, files, require_acl=require_acl)
        if present:
            return record.evidence
        # Destination inspection can cross an attacker-controlled mutation
        # boundary. Re-verify the owner-sealed record after that read and
        # immediately before the first publication write.
        _verify_plan_record(record)
        for item in files:
            parts = tuple(item.path.split("/"))
            with _open_staging_parent(
                git._repository,
                object.__getattribute__(capability, "_access"),
                parts[:-1],
            ) as parent:
                capability.assert_ignored(item.path)
                capability.revalidate()
                _verify_plan_record(record)
                _publish_staging(
                    parent,
                    parts[-1],
                    item.output,
                    require_acl=require_acl,
                    maximum=_MAX_FILE_BYTES,
                )
            capability.revalidate()
        return record.evidence
    except EnvironmentStagingError:
        raise
    except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
        publish_failed = True
    if publish_failed:
        raise EnvironmentStagingError(_ERROR)
    raise AssertionError("environment publication failure path was not reached")


def _inspect_plan(
    capability: WorktreeCapability,
    plan: EnvironmentStagingPlan,
) -> EnvironmentStagingInspection:
    policy = object.__getattribute__(capability, "_policy")
    inspect_failed = False
    try:
        record = _resolve_plan(capability, plan)
        files = record.files
        _verify_plan_record(record)
        require_acl = policy.runner_mode is RunnerMode.DOCKER
        present_count = _inspect_destination_set(capability, files, require_acl=require_acl)
        if not files:
            return EnvironmentStagingInspection(present=True, evidence=plan.evidence)
        if not present_count:
            return EnvironmentStagingInspection(present=False)
        return EnvironmentStagingInspection(present=True, evidence=record.evidence)
    except EnvironmentStagingError:
        raise
    except OSError, RepositoryAccessDenied, RuntimeError, TypeError, ValueError:
        inspect_failed = True
    if inspect_failed:
        raise EnvironmentStagingError(_ERROR)
    raise AssertionError("environment inspection failure path was not reached")


def _resolve_plan(
    capability: WorktreeCapability,
    plan: EnvironmentStagingPlan,
) -> _PlanRecord:
    if not isinstance(plan, EnvironmentStagingPlan):
        raise EnvironmentStagingError(_INTEGRITY)
    worktree = object.__getattribute__(capability, "_worktree")
    policy = object.__getattribute__(capability, "_policy")
    git = object.__getattribute__(capability, "_git")
    with _PLAN_RECORDS_LOCK:
        record = _PLAN_RECORDS.get(plan)
    if (
        record is None
        or record.token is not plan.token
        or record.git_owner is not git
        or record.identity != worktree.identity
        or record.policy_id != policy.id
        or record.policy_version != policy.version
        or tuple(plan.evidence) != record.evidence
    ):
        raise EnvironmentStagingError(_INTEGRITY)
    return record


def _verify_plan_record(record: _PlanRecord) -> None:
    if len(record.files) != len(record.evidence):
        raise EnvironmentStagingError(_INTEGRITY)
    for item, evidence in zip(record.files, record.evidence, strict=True):
        if item.evidence != evidence:
            raise EnvironmentStagingError(_INTEGRITY)
        if _staging_digests(item.path, item.source, item.output) != (
            evidence.path_digest,
            evidence.source_digest,
            evidence.output_digest,
        ):
            raise EnvironmentStagingError(_INTEGRITY)


def _inspect_destination_set(
    capability: WorktreeCapability,
    files: tuple[_StagedFile, ...],
    *,
    require_acl: bool,
) -> bool:
    present_count = 0
    git = object.__getattribute__(capability, "_git")
    for item in files:
        capability.assert_ignored(item.path)
        capability.revalidate()
        parts = tuple(item.path.split("/"))
        destination_failed = False
        try:
            with _open_staging_parent(
                git._repository,
                object.__getattribute__(capability, "_access"),
                parts[:-1],
            ) as parent:
                present, content = _read_staging(
                    parent,
                    parts[-1],
                    _MAX_FILE_BYTES,
                    require_acl=require_acl,
                )
        except OSError, RepositoryAccessDenied, ValueError:
            destination_failed = True
        if destination_failed:
            raise EnvironmentReconciliationRequired()
        capability.revalidate()
        if not present:
            continue
        if content != item.output:
            raise EnvironmentReconciliationRequired()
        present_count += 1
    if 0 < present_count < len(files):
        raise EnvironmentReconciliationRequired()
    return present_count == len(files)


def _validate_policy(policy: ProjectPolicy, policy_version: int | None) -> int:
    if not isinstance(policy, ProjectPolicy):
        raise EnvironmentStagingError(_INTEGRITY)
    if type(policy.version) is not int or policy.version < 1:
        raise EnvironmentStagingError(_INTEGRITY)
    if policy_version is not None and policy_version != policy.version:
        raise EnvironmentStagingError(_INTEGRITY)
    return policy.version


def _validate_binding(identity: object, policy: ProjectPolicy, resource: DatabaseBinding) -> None:
    if not isinstance(resource, DatabaseBinding):
        raise EnvironmentStagingError(_INTEGRITY)
    if not hasattr(identity, "database_name"):
        raise EnvironmentStagingError(_INTEGRITY)
    enabled = policy.database.enabled
    if (getattr(identity, "database_name", None) is not None) != enabled:
        raise EnvironmentStagingError(_INTEGRITY)
    if not enabled:
        if resource.state is not ResourceState.DISABLED or resource.environment:
            raise EnvironmentStagingError(_INTEGRITY)
        if any(
            value is not None
            for value in (resource.database_name, resource.database_role, resource.secret_id)
        ):
            raise EnvironmentStagingError(_INTEGRITY)
        return
    expected_secret = _expected_secret_id(identity)
    if resource.state is not ResourceState.ACTIVE:
        raise EnvironmentStagingError(_INTEGRITY)
    if (
        resource.database_name != getattr(identity, "database_name", None)
        or resource.database_role != getattr(identity, "database_role", None)
        or resource.secret_id != expected_secret
        or tuple(resource.environment) != (policy.database.injected_environment_key,)
    ):
        raise EnvironmentStagingError(_INTEGRITY)
    value = resource.environment[policy.database.injected_environment_key]
    if not isinstance(value, str) or not value:
        raise EnvironmentStagingError(_INTEGRITY)


def _expected_secret_id(identity: object) -> str:
    project_id = getattr(identity, "project_id", None)
    run_id = getattr(identity, "run_id", None)
    if project_id is None or run_id is None:
        raise EnvironmentStagingError(_INTEGRITY)
    return f"forge_db_{project_id.hex}_{run_id.hex}"


def _read_source(root: CanonicalRoot, relative_path: str) -> bytes:
    read_failed = False
    try:
        with root.open_read(relative_path) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or int(before.st_nlink) != 1:
                raise EnvironmentStagingError(_ERROR)
            chunks: list[bytes] = []
            total = 0
            while total <= _MAX_FILE_BYTES:
                chunk = stream.read(min(_READ_CHUNK_BYTES, _MAX_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_FILE_BYTES:
                    raise EnvironmentStagingError(_ERROR)
            after = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(after.st_mode)
                or int(after.st_nlink) != 1
                or (int(before.st_dev), int(before.st_ino))
                != (int(after.st_dev), int(after.st_ino))
                or int(before.st_size) != int(after.st_size)
                or int(getattr(before, "st_mtime_ns", 0)) != int(getattr(after, "st_mtime_ns", 0))
                or int(getattr(before, "st_ctime_ns", 0)) != int(getattr(after, "st_ctime_ns", 0))
            ):
                raise EnvironmentStagingError(_ERROR)
            return b"".join(chunks)
    except EnvironmentStagingError:
        raise
    except OSError, ValueError, RepositoryAccessDenied:
        read_failed = True
    if read_failed:
        raise EnvironmentStagingError(_ERROR)
    raise AssertionError("environment source-read failure path was not reached")


def _transform(source: bytes, resource: DatabaseBinding, policy: ProjectPolicy) -> bytes:
    if not policy.database.enabled:
        return source
    decode_failed = False
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
        text = ""
    if decode_failed:
        raise EnvironmentStagingError(_ERROR)
    if "\x00" in text:
        raise EnvironmentStagingError(_ERROR)
    key = policy.database.injected_environment_key
    value = resource.environment[key]
    if not _DOTENV_KEY.fullmatch(key) or not isinstance(value, str):
        raise EnvironmentStagingError(_INTEGRITY)
    return _rewrite_dotenv(text, key, value)


def _rewrite_dotenv(text: str, key: str, value: str) -> bytes:
    lines = text.splitlines(keepends=True)
    rewritten: list[str] = []
    matches = 0
    assignment = re.compile(rf"^(?P<prefix>[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=[ \t]*)")
    for line in lines:
        body, ending = _split_line_ending(line)
        match = assignment.match(body)
        if match is None:
            rewritten.append(line)
            continue
        matches += 1
        if matches > 1:
            raise EnvironmentStagingError(_ERROR)
        rewritten.append(f"{match.group('prefix')}{value}{ending}")
    if matches == 0:
        ending = _line_ending(text)
        prefix = "" if not text or text.endswith(("\n", "\r")) else ending
        rewritten.append(f"{prefix}{key}={value}")
    result = "".join(rewritten).encode("utf-8")
    if len(result) > _MAX_FILE_BYTES:
        raise EnvironmentStagingError(_ERROR)
    return result


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return "\n"


__all__ = [
    "EnvironmentFileStager",
    "EnvironmentReconciliationRequired",
    "EnvironmentStager",
    "EnvironmentStagingError",
    "WorktreeEnvironmentStager",
]
