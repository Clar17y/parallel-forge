"""Immutable structured plan contract produced by Planner agents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.domain.paths import normalize_policy_paths

_MAX_SUMMARY_LENGTH = 10_000
_MAX_ITEM_LENGTH = 5_000
_MAX_COLLECTION_SIZE = 100


def _validate_non_blank_text(value: str, field_name: str, *, max_length: int = 10_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length}")
    return value


def _validate_string_tuple(
    values: Sequence[str],
    field_name: str,
    *,
    reject_duplicates: bool = True,
    max_length: int = 5_000,
    max_items: int = 100,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field_name} must be a sequence of strings")
    if len(values) > max_items:
        raise ValueError(f"{field_name} exceeds maximum count of {max_items}")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        cleaned = _validate_non_blank_text(item, f"{field_name}[{index}]", max_length=max_length)
        if reject_duplicates:
            if cleaned in seen:
                raise ValueError(f"{field_name} must not contain duplicate entries")
            seen.add(cleaned)
        items.append(cleaned)
    return tuple(items)


class PlanOutput(BaseModel):
    """Immutable structured plan produced by a Planner agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1)
    assumptions: tuple[str, ...]
    affected_components: tuple[str, ...]
    steps: tuple[str, ...] = Field(min_length=1)
    required_checks: tuple[str, ...] = Field(min_length=1)
    risks: tuple[str, ...] = Field(min_length=1)
    security_considerations: tuple[str, ...]
    dependency_changes: tuple[str, ...]

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_non_blank_text(value, "summary", max_length=_MAX_SUMMARY_LENGTH)

    @field_validator("assumptions")
    @classmethod
    def validate_assumptions(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(
            value,
            "assumptions",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )

    @field_validator("affected_components")
    @classmethod
    def validate_affected_components(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(
            value,
            "affected_components",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: Sequence[str]) -> tuple[str, ...]:
        result = _validate_string_tuple(
            value,
            "steps",
            reject_duplicates=False,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )
        if not result:
            raise ValueError("steps must not be empty")
        return result

    @field_validator("required_checks")
    @classmethod
    def validate_required_checks(cls, value: Sequence[str]) -> tuple[str, ...]:
        result = _validate_string_tuple(
            value,
            "required_checks",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )
        if not result:
            raise ValueError("required_checks must not be empty")
        return result

    @field_validator("risks")
    @classmethod
    def validate_risks(cls, value: Sequence[str]) -> tuple[str, ...]:
        result = _validate_string_tuple(
            value,
            "risks",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )
        if not result:
            raise ValueError("risks must not be empty")
        return result

    @field_validator("security_considerations")
    @classmethod
    def validate_security_considerations(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(
            value,
            "security_considerations",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )

    @field_validator("dependency_changes")
    @classmethod
    def validate_dependency_changes(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(
            value,
            "dependency_changes",
            reject_duplicates=True,
            max_length=_MAX_ITEM_LENGTH,
            max_items=_MAX_COLLECTION_SIZE,
        )


class ScopedPlanOutput(PlanOutput):
    """Subscription proposal whose writable paths are part of human-approved evidence."""

    owned_paths: tuple[str, ...] = Field(
        max_length=64,
        description="Canonical repository-relative writable files or directories proposed for human approval. Empty means no writable paths.",
    )

    @field_validator("owned_paths")
    @classmethod
    def validate_owned_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_policy_paths(value)

    @field_validator("required_checks")
    @classmethod
    def validate_subscription_checks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # Approved names must also be representable in LogicalTaskContract.
        if any(re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None for name in value):
            raise ValueError("scoped plan checks must be subscription command identifiers")
        return value


def decode_plan_output(value: object) -> PlanOutput:
    """Read retained legacy or scoped plans without changing legacy serialization."""
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    schema = (
        ScopedPlanOutput if isinstance(value, Mapping) and "owned_paths" in value else PlanOutput
    )
    return schema.model_validate(value)


__all__ = ["PlanOutput", "ScopedPlanOutput", "decode_plan_output"]
