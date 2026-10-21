"""Validated per-run workflow preferences shared by API, services, and storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal

DelegationPreference = Literal["auto", "disabled", "requested"]
OptionalReviewPreference = Literal["auto", "skip", "required"]

_DELEGATION = frozenset({"auto", "disabled", "requested"})
_OPTIONAL_REVIEW = frozenset({"auto", "skip", "required"})
_FIELDS = frozenset({"review_plan_first", "delegation", "optional_review"})


@dataclass(frozen=True, slots=True)
class WorkflowPreferences:
    """Frozen ordinary-development preferences for one subscription run.

    A run without preferences (``None``) retains legacy frozen semantics and
    is never silently migrated to ordinary mode.
    """

    review_plan_first: bool = False
    delegation: DelegationPreference = "auto"
    optional_review: OptionalReviewPreference = "auto"

    DEFAULT: ClassVar[WorkflowPreferences]

    def __post_init__(self) -> None:
        if type(self.review_plan_first) is not bool:
            raise TypeError("review_plan_first must be a bool")
        if self.delegation not in _DELEGATION:
            raise ValueError("delegation preference is invalid")
        if self.optional_review not in _OPTIONAL_REVIEW:
            raise ValueError("optional_review preference is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "review_plan_first": self.review_plan_first,
            "delegation": self.delegation,
            "optional_review": self.optional_review,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> WorkflowPreferences | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("workflow preferences must be a mapping")
        if not set(value) <= _FIELDS:
            raise ValueError("workflow preferences contain unknown fields")
        return cls(
            review_plan_first=value.get("review_plan_first", False),  # type: ignore[arg-type]
            delegation=value.get("delegation", "auto"),  # type: ignore[arg-type]
            optional_review=value.get("optional_review", "auto"),  # type: ignore[arg-type]
        )


WorkflowPreferences.DEFAULT = WorkflowPreferences()

__all__ = ["DelegationPreference", "OptionalReviewPreference", "WorkflowPreferences"]
