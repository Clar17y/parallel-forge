"""Strict credential-free evaluation case JSON contracts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.actor import AgentRole
from forge.domain.evaluation import (
    DEVELOPER_METRIC_VERSION,
    PLANNER_METRIC_VERSION,
    REVIEWER_METRIC_VERSION,
    SeededDefect,
)
from forge.domain.paths import normalize_policy_path
from forge.domain.policy import CommandSpec
from forge.domain.review import FindingSeverity
from forge.domain.tool import ToolName
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.errors import InvalidCaseContractError

_MAX_IDENTIFIER_LENGTH = 255
_MAX_TASK_LENGTH = 10_000
_MAX_COLLECTION_ITEMS = 100
_ALLOWED_DEFECT_FIELDS = frozenset(
    {"severity", "path", "start_line", "evidence_anchor", "missing_test"}
)


def _validate_non_blank_string(
    value: Any, field_name: str, max_length: int = _MAX_IDENTIFIER_LENGTH
) -> str:
    if type(value) is not str:
        raise InvalidCaseContractError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise InvalidCaseContractError(f"{field_name} must not be blank")
    if len(stripped) > max_length:
        raise InvalidCaseContractError(f"{field_name} exceeds maximum length of {max_length}")
    assert_credential_free(stripped, field_name)
    return stripped


def _validate_string_tuple(values: Any, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if type(values) not in (list, tuple):
        raise InvalidCaseContractError(f"{field_name} must be a sequence of strings")
    if len(values) > _MAX_COLLECTION_ITEMS:
        raise InvalidCaseContractError(
            f"{field_name} exceeds maximum count of {_MAX_COLLECTION_ITEMS}"
        )
    items: list[str] = []
    seen: set[str] = set()
    for item in values:
        cleaned = _validate_non_blank_string(item, f"{field_name} item")
        if cleaned in seen:
            raise InvalidCaseContractError(f"duplicate item in {field_name}: {cleaned}")
        seen.add(cleaned)
        items.append(cleaned)
    return tuple(items)


class EvaluationCaseContract(BaseModel):
    """Immutable, credential-free definition of an evaluation case."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    fixture_version: str
    case_key: str
    task: str
    role: AgentRole
    metric_version: str | None = None
    expected_components: tuple[str, ...] = ()
    expected_checks: tuple[str, ...] = ()
    expected_risks: tuple[str, ...] = ()
    expected_dependencies: tuple[str, ...] = ()
    expected_defects: Mapping[str, SeededDefect] = Field(
        default_factory=lambda: MappingProxyType({}),
        alias="seeded_defects",
    )
    allowed_paths: tuple[str, ...] = ()
    required_tests: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    check_commands: tuple[CommandSpec, ...] = ()
    required_assertions: tuple[str, ...] = ()
    max_cost_minor: int | None = None
    max_duration_ms: int | None = None
    prohibited_tools: tuple[str, ...] = ()
    repository_template: str | None = None
    base_directory: Path | None = None

    @field_validator("fixture_version", mode="before")
    @classmethod
    def _validate_fixture_version(cls, value: Any) -> str:
        return _validate_non_blank_string(value, "fixture_version", max_length=64)

    @field_validator("case_key", mode="before")
    @classmethod
    def _validate_case_key(cls, value: Any) -> str:
        return _validate_non_blank_string(value, "case_key", max_length=_MAX_IDENTIFIER_LENGTH)

    @field_validator("task", mode="before")
    @classmethod
    def _validate_task(cls, value: Any) -> str:
        return _validate_non_blank_string(value, "task", max_length=_MAX_TASK_LENGTH)

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: Any) -> AgentRole:
        if isinstance(value, AgentRole):
            return value
        if type(value) is str:
            val = value.strip().lower()
            if val in {r.value for r in AgentRole}:
                return AgentRole(val)
            raise InvalidCaseContractError("invalid agent role")
        raise InvalidCaseContractError("role must be a valid agent role string")

    @field_validator("metric_version", mode="before")
    @classmethod
    def _validate_metric_version(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _validate_non_blank_string(value, "metric_version", max_length=64)

    @field_validator(
        "expected_components",
        "expected_checks",
        "expected_risks",
        "expected_dependencies",
        "allowed_paths",
        "required_tests",
        "required_checks",
        "required_assertions",
        mode="before",
    )
    @classmethod
    def _validate_tuples(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _validate_string_tuple(value, info.field_name)

    @field_validator("prohibited_tools", mode="before")
    @classmethod
    def _validate_prohibited_tools(cls, value: Any) -> tuple[str, ...]:
        tools = _validate_string_tuple(value, "prohibited_tools")
        for tool in tools:
            try:
                ToolName(tool)
            except ValueError:
                raise InvalidCaseContractError(f"invalid prohibited tool: {tool}") from None
        return tools

    @field_validator("repository_template", mode="before")
    @classmethod
    def _validate_repository_template(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _validate_non_blank_string(
            value, "repository_template", max_length=_MAX_IDENTIFIER_LENGTH
        )

    @field_validator("expected_defects", mode="before")
    @classmethod
    def _validate_defects(cls, value: Any) -> Mapping[str, SeededDefect]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise InvalidCaseContractError(
                "expected_defects must be a mapping of defect key to defect"
            )
        result: dict[str, SeededDefect] = {}
        for key, defect_data in value.items():
            defect_key = _validate_non_blank_string(key, "defect key")
            if isinstance(defect_data, SeededDefect):
                result[defect_key] = defect_data
                continue
            if not isinstance(defect_data, Mapping):
                raise InvalidCaseContractError(f"defect {defect_key} must be a mapping")

            # Validate defect fields exactly
            unknown_fields = set(defect_data.keys()) - _ALLOWED_DEFECT_FIELDS
            if unknown_fields:
                raise InvalidCaseContractError(f"defect {defect_key} contains unknown fields")

            for required in ("severity", "path", "start_line", "evidence_anchor"):
                if required not in defect_data:
                    raise InvalidCaseContractError(
                        f"defect {defect_key} missing required field: {required}"
                    )

            severity_val = defect_data["severity"]
            if isinstance(severity_val, FindingSeverity):
                severity = severity_val
            elif type(severity_val) is str:
                try:
                    severity = FindingSeverity(severity_val.strip().lower())
                except ValueError:
                    raise InvalidCaseContractError(
                        f"defect {defect_key} has invalid severity"
                    ) from None
            else:
                raise InvalidCaseContractError(f"defect {defect_key} has invalid severity")

            path = _validate_non_blank_string(defect_data["path"], f"defect {defect_key} path")
            try:
                normalize_policy_path(path)
            except ValueError:
                raise InvalidCaseContractError(
                    f"defect {defect_key} path is not a valid relative repository path"
                )

            anchor = _validate_non_blank_string(
                defect_data["evidence_anchor"], f"defect {defect_key} evidence_anchor"
            )

            start_line = defect_data["start_line"]
            if type(start_line) is not int or start_line < 1:
                raise InvalidCaseContractError(
                    f"defect {defect_key} start_line must be a positive integer"
                )

            missing_test_raw = defect_data.get("missing_test", False)
            if type(missing_test_raw) is not bool:
                raise InvalidCaseContractError(
                    f"defect {defect_key} missing_test must be a boolean"
                )

            result[defect_key] = SeededDefect(
                severity=severity,
                path=path,
                start_line=start_line,
                evidence_anchor=anchor,
                missing_test=missing_test_raw,
            )
        return MappingProxyType(result)

    @field_validator("expected_defects", mode="after")
    @classmethod
    def _freeze_defects(cls, value: Any) -> Mapping[str, SeededDefect]:
        if isinstance(value, MappingProxyType):
            return value
        return MappingProxyType(dict(value))

    @field_validator("max_cost_minor", "max_duration_ms", mode="before")
    @classmethod
    def _validate_nonnegative_int(cls, value: Any, info: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise InvalidCaseContractError(f"{info.field_name} must be a nonnegative integer")
        return value

    @model_validator(mode="after")
    def _validate_role_specific_shape(self) -> Self:
        for command in self.check_commands:
            assert_credential_free(command.model_dump_json(), "check command")
        if self.check_commands and (
            len({command.name for command in self.check_commands}) != len(self.check_commands)
            or {command.name for command in self.check_commands} != set(self.required_checks)
        ):
            raise InvalidCaseContractError(
                "check commands must exactly match declared required checks"
            )
        # Assign default metric version if not explicitly set
        if self.metric_version is None:
            default_metric = {
                AgentRole.PLANNER: PLANNER_METRIC_VERSION,
                AgentRole.REVIEWER: REVIEWER_METRIC_VERSION,
                AgentRole.DEVELOPER: DEVELOPER_METRIC_VERSION,
            }.get(self.role, PLANNER_METRIC_VERSION)
            object.__setattr__(self, "metric_version", default_metric)

        if self.role == AgentRole.PLANNER:
            if self.expected_defects:
                raise InvalidCaseContractError(
                    "planner case contract cannot declare expected_defects"
                )
            if self.allowed_paths or self.required_tests or self.required_assertions:
                raise InvalidCaseContractError(
                    "planner case contract cannot declare developer assertions"
                )
        elif self.role == AgentRole.REVIEWER:
            if (
                self.expected_components
                or self.expected_checks
                or self.expected_risks
                or self.expected_dependencies
            ):
                raise InvalidCaseContractError(
                    "reviewer case contract cannot declare planner expectations"
                )
            if self.allowed_paths or self.required_tests or self.required_assertions:
                raise InvalidCaseContractError(
                    "reviewer case contract cannot declare developer assertions"
                )
        elif self.role == AgentRole.DEVELOPER:
            if self.expected_defects:
                raise InvalidCaseContractError(
                    "developer case contract cannot declare expected_defects"
                )
            if self.expected_risks or self.expected_dependencies:
                raise InvalidCaseContractError(
                    "developer case contract cannot declare planner risks/dependencies"
                )
        return self

    @property
    def seeded_defects(self) -> Mapping[str, SeededDefect]:
        """Alias for expected_defects to integrate with score_review."""
        return self.expected_defects


__all__ = ["EvaluationCaseContract"]
