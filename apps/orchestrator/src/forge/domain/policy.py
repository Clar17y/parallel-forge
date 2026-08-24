"""Immutable project policy and command allow-list contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.paths import normalize_policy_paths, union_policy_paths
from forge.domain.review import FindingSeverity, ReviewFinding

_SHELL_META = re.compile(r"[\r\n;&|$`()<>`]")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REFERENCE = re.compile(r"^secret://[A-Za-z0-9][A-Za-z0-9._/-]*$")
_KNOWN_MERGE_METHODS = frozenset({"merge", "squash", "rebase"})
_SHELL_WRAPPERS = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "zsh",
    }
)
_SHELL_WRAPPER_FLAGS = frozenset(
    {"-c", "--command", "-command", "/c", "/k", "/command", "/encodedcommand", "-encodedcommand"}
)
_VALIDATION_KINDS = frozenset({"test", "lint", "typecheck", "build", "custom_named"})


class RunnerMode(StrEnum):
    """The execution boundary used for policy-controlled commands."""

    DOCKER = "docker"
    TRUSTED_HOST = "trusted_host"


class StepKind(StrEnum):
    """Closed set of operations that may be dispatched by a runner."""

    BOOTSTRAP = "bootstrap"
    INSTALL = "install"
    MIGRATION = "migration"
    SEED = "seed"
    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"
    CUSTOM = "custom_named"


class CommandSpec(BaseModel):
    """One exact argv vector in the project's command registry.

    Commands deliberately have no shell command text field.  The runner can
    execute only this validated argv vector and its explicitly allowed inputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: StepKind
    name: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(ge=1, le=7200)
    required: bool = True
    network_enabled: bool = False
    environment_keys: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command name must not be blank")
        _reject_shell_text(value, "command name")
        return value

    @field_validator("argv")
    @classmethod
    def argv_must_be_direct_and_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("argv must not be empty")
        if any(not argument or not argument.strip() for argument in value):
            raise ValueError("argv entries must not be blank")
        if any(_SHELL_META.search(argument) for argument in value):
            raise ValueError("argv must not contain shell metacharacters")

        executable = value[0].replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()
        if executable in _SHELL_WRAPPERS:
            raise ValueError("shell wrapper commands are not permitted")
        if any(argument.casefold() in _SHELL_WRAPPER_FLAGS for argument in value[1:]):
            raise ValueError("shell wrapper flags are not permitted")
        return value

    @field_validator("environment_keys")
    @classmethod
    def environment_keys_must_be_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("environment_keys must not contain duplicates")
        if any(not _ENVIRONMENT_KEY.fullmatch(key) for key in value):
            raise ValueError("environment_keys must contain environment variable names")
        return value


class AgentModelPolicy(BaseModel):
    """Budget and provider limits for one Forge agent role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(default="google", min_length=1)
    model: str = Field(default="gemini-3.5-flash", min_length=1)
    max_input_tokens: int = Field(default=100_000, ge=1)
    max_output_tokens: int = Field(default=16_000, ge=1)
    max_tool_calls: int = Field(default=100, ge=0)
    max_duration_seconds: int = Field(default=1800, ge=1)
    max_cost_minor: int = Field(default=1000, ge=0)

    @field_validator("provider", "model")
    @classmethod
    def model_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model policy text must not be blank")
        return value


class DatabaseProvisioningPolicy(BaseModel):
    """Opt-in database provisioning without storing an administrator secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    admin_url_secret_reference: str | None = None
    injected_environment_key: str = "DATABASE_URL"

    @field_validator("admin_url_secret_reference")
    @classmethod
    def secret_reference_must_be_server_side(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_REFERENCE.fullmatch(value):
            raise ValueError("admin_url_secret_reference must be a secret:// reference")
        return value

    @field_validator("injected_environment_key")
    @classmethod
    def injected_environment_key_must_be_name(cls, value: str) -> str:
        if not _ENVIRONMENT_KEY.fullmatch(value):
            raise ValueError("injected_environment_key must be an environment variable name")
        return value

    @model_validator(mode="after")
    def require_opt_in_reference_pair(self) -> DatabaseProvisioningPolicy:
        if self.enabled and self.admin_url_secret_reference is None:
            raise ValueError("enabled database provisioning requires an admin secret reference")
        if not self.enabled and self.admin_url_secret_reference is not None:
            raise ValueError("database secret reference requires enabled provisioning")
        return self


class _RequiredChecks(tuple[CommandSpec, ...]):
    """Tuple result that supports both property and method-style callers."""

    def __new__(cls, values: Iterable[CommandSpec] = ()) -> Self:
        return super().__new__(cls, values)

    def __call__(self) -> tuple[CommandSpec, ...]:
        return tuple(self)


class ProjectPolicy(BaseModel):
    """Versioned, immutable project execution and release policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    version: int = Field(ge=1)
    repository_path: str = Field(min_length=1)
    github_repository: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    runner_mode: RunnerMode = RunnerMode.DOCKER
    trusted_project: bool = False
    local_remediation_limit: int = Field(default=3, ge=0, le=20)
    remote_remediation_limit: int = Field(default=3, ge=0, le=20)
    planner_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    developer_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    reviewer_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    database: DatabaseProvisioningPolicy = Field(default_factory=DatabaseProvisioningPolicy)
    commands: tuple[CommandSpec, ...] = ()
    allowed_environment_files: tuple[str, ...] = ()
    secret_paths: tuple[str, ...] = (".env", ".env.local")
    allowed_merge_methods: tuple[str, ...] = ("squash",)
    publication_blocking_severities: frozenset[FindingSeverity] = frozenset(
        {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
    )
    merge_blocking_severities: frozenset[FindingSeverity] = frozenset(
        {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
    )

    @field_validator("repository_path")
    @classmethod
    def repository_path_must_be_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("repository_path must be absolute")
        return str(Path(value))

    @field_validator("github_repository", "default_branch")
    @classmethod
    def identity_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("policy identity fields must not be blank")
        return value

    @field_validator("allowed_environment_files", "secret_paths")
    @classmethod
    def policy_paths_must_be_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_policy_paths(value)

    @field_validator("allowed_merge_methods")
    @classmethod
    def merge_methods_must_be_allowlisted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one merge method must be allowed")
        if len(set(value)) != len(value):
            raise ValueError("allowed merge methods must not contain duplicates")
        if any(method not in _KNOWN_MERGE_METHODS for method in value):
            raise ValueError("allowed_merge_methods contains an unlisted merge method")
        return value

    @field_validator("publication_blocking_severities", "merge_blocking_severities")
    @classmethod
    def blocking_severities_must_be_known(
        cls, value: frozenset[FindingSeverity]
    ) -> frozenset[FindingSeverity]:
        return value

    @model_validator(mode="after")
    def validate_trust_and_commands(self) -> ProjectPolicy:
        if self.runner_mode is RunnerMode.TRUSTED_HOST and not self.trusted_project:
            raise ValueError("trusted_host runner requires trusted_project")

        names = [command.name for command in self.commands]
        if len(set(names)) != len(names):
            raise ValueError("duplicate command names are not permitted")
        union_policy_paths(self.secret_paths, self.allowed_environment_files)
        return self

    def commands_for(self, kind: StepKind) -> tuple[CommandSpec, ...]:
        """Return every registered command for one closed step kind."""

        normalized = kind if isinstance(kind, StepKind) else StepKind(kind)
        return tuple(command for command in self.commands if command.kind is normalized)

    @property
    def required_checks(self) -> _RequiredChecks:
        """Required validation commands used as approval evidence."""

        return _RequiredChecks(
            command
            for command in self.commands
            if command.required and command.kind.value in _VALIDATION_KINDS
        )

    @property
    def effective_secret_paths(self) -> tuple[str, ...]:
        """Configured secret paths followed by allowed environment files."""

        return union_policy_paths(self.secret_paths, self.allowed_environment_files)

    def blocks_publication(self, findings: Iterable[ReviewFinding]) -> bool:
        """Whether unresolved findings block PR publication under this policy."""

        return self._blocks(findings, self.publication_blocking_severities)

    def blocks_merge(self, findings: Iterable[ReviewFinding]) -> bool:
        """Whether unresolved findings block merging under this policy."""

        return self._blocks(findings, self.merge_blocking_severities)

    @staticmethod
    def _blocks(findings: Iterable[ReviewFinding], severities: frozenset[FindingSeverity]) -> bool:
        return any(
            not finding.is_resolved and finding.severity in severities for finding in findings
        )


def _reject_shell_text(value: str, field_name: str) -> None:
    if _SHELL_META.search(value):
        raise ValueError(f"{field_name} must not contain shell metacharacters")


__all__ = [
    "AgentModelPolicy",
    "CommandSpec",
    "DatabaseProvisioningPolicy",
    "ProjectPolicy",
    "RunnerMode",
    "StepKind",
]
