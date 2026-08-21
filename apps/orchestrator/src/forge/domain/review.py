"""Immutable review findings used by publication and merge gates."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FindingSeverity(StrEnum):
    """The severity assigned to one stable review finding."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class ReviewFinding(BaseModel):
    """A review finding that remains stable across remediation attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: FindingSeverity
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    summary: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    proposed_resolution: str | None = None
    resolved_at: datetime | None = None

    @field_validator("finding_id", "path", "summary", "evidence")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @property
    def is_resolved(self) -> bool:
        """Whether this finding has an explicit resolution timestamp."""

        return self.resolved_at is not None


__all__ = ["FindingSeverity", "ReviewFinding"]
