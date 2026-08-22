"""Immutable, content-bound approval evidence and approval records."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from forge.domain.policy import RunnerMode

_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST64 = re.compile(r"^[0-9a-fA-F]{64}$")


class ApprovalGate(StrEnum):
    """The human gate an approval record authorizes."""

    PLAN = "plan"
    PR = "pr"
    MERGE = "merge"


class EvidenceModel(BaseModel):
    """Base contract for immutable evidence snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanApprovalEvidence(EvidenceModel):
    task_version: int = Field(ge=1)
    plan_digest: str
    repository: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    base_sha: str
    policy_version: int = Field(ge=1)
    dependency_changes: tuple[str, ...] = ()
    required_checks: Mapping[str, str] = Field(default_factory=dict)
    runner_mode: RunnerMode
    local_remediation_limit: int = Field(ge=0)
    token_budget: int = Field(ge=0)
    cost_budget_minor: int = Field(ge=0)
    duration_budget_seconds: int = Field(ge=1)

    _validate_digests = field_validator("plan_digest")(
        lambda value: _require_digest(value, "plan_digest")
    )
    _validate_base_sha = field_validator("base_sha")(lambda value: _require_sha(value, "base_sha"))
    _normalize_dependencies = field_validator("dependency_changes", mode="before")(
        lambda value: _normalize_dependency_changes(value)
    )
    _validate_checks = field_validator("required_checks")(lambda value: _freeze_checks(value))

    @field_serializer("required_checks")
    def serialize_required_checks(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class PrApprovalEvidence(EvidenceModel):
    candidate_commit: str
    diff_digest: str
    validation_digest: str
    review_digest: str
    repository: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    base_sha: str
    title: str = Field(min_length=1)
    body_digest: str
    runner_mode: RunnerMode
    runner_evidence_digest: str
    remote_remediation_limit: int = Field(ge=0)

    _validate_commit = field_validator("candidate_commit")(
        lambda value: _require_sha(value, "candidate_commit")
    )
    _validate_base_sha = field_validator("base_sha")(lambda value: _require_sha(value, "base_sha"))
    _validate_digests = field_validator(
        "diff_digest", "validation_digest", "review_digest", "body_digest", "runner_evidence_digest"
    )(lambda value: _require_digest(value, "evidence digest"))


class MergeApprovalEvidence(EvidenceModel):
    repository: str = Field(min_length=1)
    pull_request_number: int = Field(ge=1)
    head_sha: str
    base_ref: str = Field(min_length=1)
    base_sha: str
    required_checks: Mapping[str, str]
    unresolved_blocking_findings: int = Field(ge=0)
    validation_digest: str
    review_digest: str
    runner_mode: RunnerMode
    runner_evidence_digest: str
    merge_method: str = Field(min_length=1)
    policy_version: int = Field(ge=1)

    _validate_head_sha = field_validator("head_sha")(lambda value: _require_sha(value, "head_sha"))
    _validate_base_sha = field_validator("base_sha")(lambda value: _require_sha(value, "base_sha"))
    _validate_digests = field_validator(
        "validation_digest", "review_digest", "runner_evidence_digest"
    )(lambda value: _require_digest(value, "evidence digest"))
    _validate_checks = field_validator("required_checks")(lambda value: _freeze_checks(value))

    @field_serializer("required_checks")
    def serialize_required_checks(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class ApprovalRecord(BaseModel):
    """A human approval bound to one exact run, policy, actor, and digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID | None = None
    gate: ApprovalGate
    evidence_digest: str
    run_id: UUID
    run_version: int = Field(ge=0)
    policy_version: int = Field(ge=1)
    authenticated_actor_id: str = Field(min_length=1)
    created_at: datetime
    invalidated_at: datetime | None = None

    _validate_digest = field_validator("evidence_digest")(
        lambda value: _require_digest(value, "evidence_digest")
    )

    @field_validator("authenticated_actor_id")
    @classmethod
    def actor_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("authenticated_actor_id must not be blank")
        return value

    @field_validator("created_at", "invalidated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return value

    def is_valid_for(
        self,
        gate: ApprovalGate,
        evidence_digest: str,
        run_id: UUID,
        run_version: int,
        policy_version: int,
        authenticated_actor_id: str,
    ) -> bool:
        """Return true only when all bound values still match exactly."""

        if self.invalidated_at is not None:
            return False
        return (
            self.gate is gate
            and hmac.compare_digest(self.evidence_digest, evidence_digest)
            and self.run_id == run_id
            and self.run_version == run_version
            and self.policy_version == policy_version
            and self.authenticated_actor_id == authenticated_actor_id
        )

    def invalidate(self, at: datetime) -> ApprovalRecord:
        """Return an immutable invalidated copy of this approval."""

        if at.utcoffset() is None:
            raise ValueError("approval invalidation timestamp must be timezone-aware")
        return self.model_copy(update={"invalidated_at": at})


def canonical_digest(evidence: EvidenceModel) -> str:
    """Hash a stable, compact JSON representation of exact evidence values."""

    payload = json.dumps(
        _canonicalize(evidence.model_dump(mode="python", warnings=False)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_canonicalize(item) for item in value]
        return sorted(
            normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
        )
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    return value


def _normalize_dependency_changes(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value))
    return value


def _freeze_checks(value: Mapping[str, str]) -> Mapping[str, str]:
    if any(not key.strip() or not status.strip() for key, status in value.items()):
        raise ValueError("required check names and statuses must not be blank")
    return MappingProxyType(dict(value))


def _require_sha(value: str, field_name: str) -> str:
    if not _SHA40.fullmatch(value):
        raise ValueError(f"{field_name} must be a 40-character hexadecimal SHA")
    return value


def _require_digest(value: str, field_name: str) -> str:
    if not _DIGEST64.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-character hexadecimal digest")
    return value


__all__ = [
    "ApprovalGate",
    "ApprovalRecord",
    "EvidenceModel",
    "MergeApprovalEvidence",
    "PlanApprovalEvidence",
    "PrApprovalEvidence",
    "canonical_digest",
]
