"""Immutable identities and lifecycle values for isolated run resources."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ResourceState(StrEnum):
    """The database lifecycle values stored on a run."""

    DISABLED = "DISABLED"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    REMOVED = "REMOVED"


_MAX_BRANCH_LENGTH = 512
_MAX_POSTGRES_IDENTIFIER_BYTES = 63
_MAX_WORKTREE_NAME_LENGTH = 128
_MAX_SECRET_ID_LENGTH = 512
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_WORKTREE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


@dataclass(frozen=True, slots=True)
class WorktreeIdentity:
    """Stable names for one run's managed worktree and optional database.

    Run identities use the first twelve hexadecimal characters of the
    persisted project and run UUIDs.  ``for_developer`` is the standalone
    path without a persisted run UUID: its project key still comes from the
    persisted project UUID, while the branch hash supplies the disambiguating
    suffix.
    """

    project_id: UUID
    run_id: UUID | None
    branch: str
    worktree_name: str
    database_name: str | None
    database_role: str | None

    def __post_init__(self) -> None:
        _validate_uuid(self.project_id, "project id")
        if self.run_id is not None:
            _validate_uuid(self.run_id, "run id")
        _validate_branch(self.branch)
        _validate_worktree_name(self.worktree_name)

        if (self.database_name is None) != (self.database_role is None):
            raise ValueError("database name and role must be present together")
        if self.database_name is not None:
            _validate_postgres_identifier(self.database_name, "database name")
            _validate_postgres_identifier(self.database_role or "", "database role")

    @classmethod
    def for_run(
        cls,
        project_id: UUID,
        run_id: UUID,
        branch: str,
        database_enabled: bool,
    ) -> WorktreeIdentity:
        """Build the deterministic identity for a persisted run."""

        _validate_uuid(project_id, "project id")
        _validate_uuid(run_id, "run id")
        _validate_branch(branch)
        _validate_database_enabled(database_enabled)
        project_key = project_id.hex[:12]
        run_key = run_id.hex[:12]
        return cls(
            project_id=project_id,
            run_id=run_id,
            branch=branch,
            worktree_name=f"forge-{project_key}-{run_key}",
            database_name=f"forge_{project_key}_{run_key}" if database_enabled else None,
            database_role=f"forge_run_{run_key}" if database_enabled else None,
        )

    @classmethod
    def for_developer(
        cls,
        project_id: UUID,
        branch: str,
        database_enabled: bool = False,
    ) -> WorktreeIdentity:
        """Build a stable identity for a developer worktree without a run.

        The complete branch text is hashed before any display sanitization;
        therefore branches that sanitize to the same slug still receive
        different exact resource names.
        """

        _validate_uuid(project_id, "project id")
        _validate_branch(branch)
        _validate_database_enabled(database_enabled)
        project_key = project_id.hex[:12]
        branch_key = hashlib.sha256(branch.encode("utf-8")).hexdigest()[-12:]
        branch_slug = _sanitize_branch(branch)
        database_slug = branch_slug.replace("-", "_")
        worktree_name = f"forge-{project_key}-{branch_slug}-{branch_key}"
        database_name = (
            f"forge_{project_key}_{database_slug}_{branch_key}" if database_enabled else None
        )
        database_role = (
            f"forge_run_{project_key}_{database_slug}_{branch_key}" if database_enabled else None
        )
        return cls(
            project_id=project_id,
            run_id=None,
            branch=branch,
            worktree_name=worktree_name,
            database_name=database_name,
            database_role=database_role,
        )


def _validate_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")


def _validate_branch(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("branch must be a string")
    if not value or not value.strip() or value != value.strip():
        raise ValueError("branch must be nonblank and trimmed")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("branch contains forbidden control characters")
    if len(value) > _MAX_BRANCH_LENGTH:
        raise ValueError(f"branch must contain at most {_MAX_BRANCH_LENGTH} characters")


def _validate_database_enabled(value: object) -> None:
    if type(value) is not bool:
        raise TypeError("database_enabled must be a boolean")


def _validate_worktree_name(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("worktree name must be nonblank and trimmed")
    if "\x00" in value or len(value) > _MAX_WORKTREE_NAME_LENGTH:
        raise ValueError(
            f"worktree name must contain at most {_MAX_WORKTREE_NAME_LENGTH} characters"
        )
    if _WORKTREE_NAME.fullmatch(value) is None:
        raise ValueError("worktree name must be one safe lowercase filename component")


def _validate_postgres_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonblank PostgreSQL identifier")
    if len(value.encode("utf-8")) > _MAX_POSTGRES_IDENTIFIER_BYTES:
        raise ValueError(f"{name} must be at most {_MAX_POSTGRES_IDENTIFIER_BYTES} bytes")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is not a safe PostgreSQL identifier")


def validate_resource_shape(
    database_state: ResourceState,
    database_name: str | None,
    database_role: str | None,
    secret_id: str | None,
) -> None:
    """Enforce the persisted run resource invariant at the domain boundary."""

    if not isinstance(database_state, ResourceState):
        raise TypeError("database state must be a ResourceState")
    for value, name in (
        (database_name, "database name"),
        (database_role, "database role"),
    ):
        if value is not None:
            _validate_postgres_identifier(value, name)
    if secret_id is not None:
        if not secret_id or secret_id != secret_id.strip() or "\x00" in secret_id:
            raise ValueError("secret id must be nonblank and trimmed")
        if len(secret_id) > _MAX_SECRET_ID_LENGTH:
            raise ValueError(f"secret id must contain at most {_MAX_SECRET_ID_LENGTH} characters")

    database_identity = (database_name, database_role, secret_id)
    if database_state in {ResourceState.DISABLED, ResourceState.REMOVED}:
        if any(value is not None for value in database_identity):
            raise ValueError(f"resource state {database_state.value} requires a null resource")
    elif database_state is ResourceState.ACTIVE and any(
        value is None for value in database_identity
    ):
        raise ValueError("resource state ACTIVE requires database name, role, and secret id")


def _sanitize_branch(branch: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", branch.casefold()).strip("-")
    return (slug or "branch")[:24]


__all__ = ["ResourceState", "WorktreeIdentity", "validate_resource_shape"]
