"""Canonical evidence manifest contracts and codec."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from forge.domain.agent import ReviewOutput
from forge.domain.review import ReviewFinding

_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_SHA1_HEX = re.compile(r"\A[0-9a-f]{40}\Z", re.ASCII)
_CANONICAL_UUID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)
_MAX_MANIFEST_BYTES = 256 * 1024  # 262_144 bytes
_MAX_VALIDATION_MEMBERS = 64
_MAX_POLICY_VERSION = 2_147_483_647
_MAX_STRING_LENGTH = 255


class EvidenceManifestError(ValueError):
    """Raised when an evidence manifest fails validation or codec constraints."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"EvidenceManifestError({self.message!r})"


class EvidenceStatus(StrEnum):
    """Terminal operational status values for validation evidence members."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


ValidationStatus = EvidenceStatus


def _validate_non_nil_uuid(value: Any, field_name: str) -> UUID:
    if isinstance(value, str):
        if _CANONICAL_UUID.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be a canonical lowercase UUID string")
        try:
            value = UUID(value)
        except ValueError, TypeError:
            raise ValueError(f"{field_name} must be a valid UUID") from None
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be a nil UUID")
    return value


def _validate_bounded_non_blank_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > _MAX_STRING_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {_MAX_STRING_LENGTH}")
    return value


def _validate_strict_int(
    value: Any, field_name: str, *, min_value: int = 1, max_value: int | None = None
) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a strict integer")
    if value < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{field_name} must be <= {max_value}")
    return value


def _validate_sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a string")
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase 64-character hex SHA-256 digest")
    return value


def _validate_sha1(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a string")
    if _SHA1_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase 40-character hex commit SHA")
    return value


def _validate_timezone_aware(value: Any, field_name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError, TypeError:
            raise ValueError(f"{field_name} must be a valid ISO format datetime") from None
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    try:
        value.astimezone(UTC)
    except OverflowError, ValueError:
        raise ValueError(f"{field_name} out of range for UTC conversion") from None
    return value


class ValidationEvidenceMember(BaseModel):
    """One immutable command execution outcome within a validation evidence set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: UUID
    check_name: str
    command_name: str
    command_version: int
    command_digest: str
    command_result_digest: str
    stdout_digest: str
    stderr_digest: str
    status: EvidenceStatus
    exit_code: int | None = None
    started_at: datetime
    completed_at: datetime

    @field_validator("result_id", mode="before")
    @classmethod
    def _validate_result_id(cls, value: Any) -> UUID:
        return _validate_non_nil_uuid(value, "result_id")

    @field_validator("check_name", mode="before")
    @classmethod
    def _validate_check_name(cls, value: Any) -> str:
        return _validate_bounded_non_blank_str(value, "check_name")

    @field_validator("command_name", mode="before")
    @classmethod
    def _validate_command_name(cls, value: Any) -> str:
        return _validate_bounded_non_blank_str(value, "command_name")

    @field_validator("command_version", mode="before")
    @classmethod
    def _validate_command_version(cls, value: Any) -> int:
        return _validate_strict_int(value, "command_version", min_value=1)

    @field_validator(
        "command_digest",
        "command_result_digest",
        "stdout_digest",
        "stderr_digest",
        mode="before",
    )
    @classmethod
    def _validate_digests(cls, value: Any, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("exit_code", mode="before")
    @classmethod
    def _validate_exit_code(cls, value: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or isinstance(value, bool):
            raise TypeError("exit_code must be a strict integer or None")
        return value

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def _validate_timestamps(cls, value: Any, info: Any) -> datetime:
        return _validate_timezone_aware(value, info.field_name)

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        if self.completed_at.astimezone(UTC) < self.started_at.astimezone(UTC):
            raise ValueError("completed_at must be greater than or equal to started_at")

        if self.status == EvidenceStatus.PASSED:
            if self.exit_code != 0:
                raise ValueError("PASSED member requires exit_code == 0")
        elif self.status == EvidenceStatus.FAILED:
            if self.exit_code == 0:
                raise ValueError("FAILED member must not claim exit_code == 0")
        elif self.status in (EvidenceStatus.SKIPPED, EvidenceStatus.CANCELLED):
            pass

        return self


class ValidationEvidenceManifest(BaseModel):
    """Immutable manifest recording command check outcomes for a validation step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["validation"] = "validation"
    evidence_set_id: UUID
    run_id: UUID
    step_id: UUID
    policy_version: int
    head_sha: str
    prior_review_evidence_set_id: UUID | None = None
    members: tuple[ValidationEvidenceMember, ...] = Field(
        default=(), max_length=_MAX_VALIDATION_MEMBERS
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: Any) -> int:
        if type(value) is not int or isinstance(value, bool):
            raise TypeError("schema_version must be a strict integer")
        if value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("evidence_set_id", "run_id", "step_id", mode="before")
    @classmethod
    def _validate_uuids(cls, value: Any, info: Any) -> UUID:
        return _validate_non_nil_uuid(value, info.field_name)

    @field_validator("policy_version", mode="before")
    @classmethod
    def _validate_policy_version(cls, value: Any) -> int:
        return _validate_strict_int(
            value, "policy_version", min_value=1, max_value=_MAX_POLICY_VERSION
        )

    @field_validator("head_sha", mode="before")
    @classmethod
    def _validate_head_sha(cls, value: Any) -> str:
        return _validate_sha1(value, "head_sha")

    @field_validator("prior_review_evidence_set_id", mode="before")
    @classmethod
    def _validate_prior_review_set_id(cls, value: Any) -> UUID | None:
        if value is not None:
            return _validate_non_nil_uuid(value, "prior_review_evidence_set_id")
        return None

    @field_validator("members", mode="before")
    @classmethod
    def _validate_members_sequence(
        cls, value: Sequence[Any]
    ) -> tuple[ValidationEvidenceMember, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise TypeError("members must be a sequence")
        if len(value) > _MAX_VALIDATION_MEMBERS:
            raise ValueError(f"members exceeds maximum count of {_MAX_VALIDATION_MEMBERS}")

        parsed_members: list[ValidationEvidenceMember] = []
        for item in value:
            if isinstance(item, ValidationEvidenceMember):
                parsed_members.append(ValidationEvidenceMember.model_validate(_plain_state(item)))
            elif isinstance(item, dict):
                parsed_members.append(ValidationEvidenceMember.model_validate(item))
            else:
                raise TypeError("members must be ValidationEvidenceMember instances")

        seen_result_ids: set[UUID] = set()
        seen_semantic_ids: set[tuple[str, str, int]] = set()

        for member in parsed_members:
            if member.result_id in seen_result_ids:
                raise ValueError("duplicate result_id in validation members")
            seen_result_ids.add(member.result_id)

            semantic_id = (member.check_name, member.command_name, member.command_version)
            if semantic_id in seen_semantic_ids:
                raise ValueError("duplicate semantic check identity in validation members")
            seen_semantic_ids.add(semantic_id)

        sorted_members = sorted(
            parsed_members,
            key=lambda m: (m.check_name, m.command_name, m.command_version, str(m.result_id)),
        )
        return tuple(sorted_members)

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        if (
            self.prior_review_evidence_set_id is not None
            and self.prior_review_evidence_set_id == self.evidence_set_id
        ):
            raise ValueError("prior_review_evidence_set_id cannot be identical to evidence_set_id")
        return self


class ReviewEvidenceManifest(BaseModel):
    """Immutable manifest recording reviewer decisions and findings for a review step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["review"] = "review"
    evidence_set_id: UUID
    run_id: UUID
    step_id: UUID
    policy_version: int
    head_sha: str
    producer_execution_id: UUID
    validation_evidence_set_id: UUID
    review: ReviewOutput

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: Any) -> int:
        if type(value) is not int or isinstance(value, bool):
            raise TypeError("schema_version must be a strict integer")
        if value != 1:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator(
        "evidence_set_id",
        "run_id",
        "step_id",
        "producer_execution_id",
        "validation_evidence_set_id",
        mode="before",
    )
    @classmethod
    def _validate_uuids(cls, value: Any, info: Any) -> UUID:
        return _validate_non_nil_uuid(value, info.field_name)

    @field_validator("policy_version", mode="before")
    @classmethod
    def _validate_policy_version(cls, value: Any) -> int:
        return _validate_strict_int(
            value, "policy_version", min_value=1, max_value=_MAX_POLICY_VERSION
        )

    @field_validator("head_sha", mode="before")
    @classmethod
    def _validate_head_sha(cls, value: Any) -> str:
        return _validate_sha1(value, "head_sha")

    @field_validator("review", mode="before")
    @classmethod
    def _validate_review(cls, value: Any) -> ReviewOutput:
        if not isinstance(value, (Mapping, ReviewOutput)):
            raise TypeError("review must be a ReviewOutput instance or dict")

        review_state = _plain_state(value)
        if not isinstance(review_state, dict):
            raise TypeError("review must be an object")
        findings_data = review_state.get("findings", ())

        if not isinstance(findings_data, Sequence) or isinstance(findings_data, (str, bytes)):
            raise TypeError("review findings must be a sequence")

        parsed_findings: list[ReviewFinding] = []
        for f in findings_data:
            f_data = _plain_state(f)
            if not isinstance(f_data, dict):
                raise TypeError("review findings entries must be ReviewFinding instances or dicts")

            start_line = f_data.get("start_line")
            if type(start_line) is not int or isinstance(start_line, bool):
                raise TypeError("finding start_line must be a strict integer")

            resolved_at = f_data.get("resolved_at")
            if resolved_at is not None:
                if isinstance(resolved_at, str):
                    try:
                        resolved_at = datetime.fromisoformat(resolved_at)
                    except ValueError, TypeError:
                        raise ValueError(
                            "finding resolved_at must be a valid ISO format datetime"
                        ) from None
                if not isinstance(resolved_at, datetime):
                    raise TypeError("finding resolved_at must be a datetime")
                if resolved_at.tzinfo is None or resolved_at.utcoffset() is None:
                    raise ValueError("finding resolved_at must be timezone-aware")
                try:
                    resolved_at.astimezone(UTC)
                except OverflowError, ValueError:
                    raise ValueError(
                        "finding resolved_at out of range for UTC conversion"
                    ) from None
                f_data["resolved_at"] = resolved_at

            parsed_findings.append(ReviewFinding.model_validate(f_data))

        review_state["findings"] = tuple(sorted(parsed_findings, key=lambda item: item.finding_id))
        return ReviewOutput.model_validate(review_state)

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        if self.validation_evidence_set_id == self.evidence_set_id:
            raise ValueError("validation_evidence_set_id cannot be identical to evidence_set_id")
        return self


type EvidenceManifest = Annotated[
    ValidationEvidenceManifest | ReviewEvidenceManifest,
    Field(discriminator="kind"),
]


def _format_datetime(dt: datetime) -> str:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise EvidenceManifestError("invalid datetime for canonical serialization")
    try:
        return dt.astimezone(UTC).isoformat()
    except OverflowError, ValueError:
        raise EvidenceManifestError("datetime out of range for UTC conversion") from None


def _plain_state(value: Any) -> Any:
    """Copy complete Pydantic state without silently discarding bypassed fields."""
    if isinstance(value, BaseModel):
        state = dict(value.__dict__)
        extras = value.__pydantic_extra__
        if extras is not None:
            state.update(extras)
        return {key: _plain_state(item) for key, item in state.items()}
    if isinstance(value, Mapping):
        return {key: _plain_state(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain_state(item) for item in value)
    if isinstance(value, list):
        return [_plain_state(item) for item in value]
    return value


def _to_canonical_dict(
    manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
) -> dict[str, Any]:
    if isinstance(manifest, ValidationEvidenceManifest):
        return {
            "evidence_set_id": str(manifest.evidence_set_id),
            "head_sha": manifest.head_sha,
            "kind": manifest.kind,
            "members": [
                {
                    "check_name": m.check_name,
                    "command_digest": m.command_digest,
                    "command_name": m.command_name,
                    "command_result_digest": m.command_result_digest,
                    "command_version": m.command_version,
                    "completed_at": _format_datetime(m.completed_at),
                    "exit_code": m.exit_code,
                    "result_id": str(m.result_id),
                    "started_at": _format_datetime(m.started_at),
                    "status": m.status.value,
                    "stderr_digest": m.stderr_digest,
                    "stdout_digest": m.stdout_digest,
                }
                for m in manifest.members
            ],
            "policy_version": manifest.policy_version,
            "prior_review_evidence_set_id": (
                str(manifest.prior_review_evidence_set_id)
                if manifest.prior_review_evidence_set_id is not None
                else None
            ),
            "run_id": str(manifest.run_id),
            "schema_version": manifest.schema_version,
            "step_id": str(manifest.step_id),
        }
    elif isinstance(manifest, ReviewEvidenceManifest):
        return {
            "evidence_set_id": str(manifest.evidence_set_id),
            "head_sha": manifest.head_sha,
            "kind": manifest.kind,
            "policy_version": manifest.policy_version,
            "producer_execution_id": str(manifest.producer_execution_id),
            "review": {
                "decision": manifest.review.decision.value,
                "findings": [
                    {
                        "evidence": f.evidence,
                        "finding_id": f.finding_id,
                        "path": f.path,
                        "proposed_resolution": f.proposed_resolution,
                        "resolved_at": (
                            _format_datetime(f.resolved_at) if f.resolved_at is not None else None
                        ),
                        "severity": f.severity.value,
                        "start_line": f.start_line,
                        "summary": f.summary,
                    }
                    for f in manifest.review.findings
                ],
                "missing_evidence": list(manifest.review.missing_evidence),
                "summary": manifest.review.summary,
                "tested_claims": list(manifest.review.tested_claims),
            },
            "run_id": str(manifest.run_id),
            "schema_version": manifest.schema_version,
            "step_id": str(manifest.step_id),
            "validation_evidence_set_id": str(manifest.validation_evidence_set_id),
        }
    else:
        raise EvidenceManifestError("unknown manifest type")


def _deep_validate_manifest(
    manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
) -> ValidationEvidenceManifest | ReviewEvidenceManifest:
    """Deeply revalidate a manifest to catch any bypass via model_copy or model_construct."""
    try:
        if not isinstance(manifest, (ValidationEvidenceManifest, ReviewEvidenceManifest)):
            raise EvidenceManifestError(
                "manifest must be ValidationEvidenceManifest or ReviewEvidenceManifest"
            )
        state = _plain_state(manifest)
        if isinstance(manifest, ValidationEvidenceManifest):
            return ValidationEvidenceManifest.model_validate(state)
        return ReviewEvidenceManifest.model_validate(state)
    except EvidenceManifestError:
        raise
    except (
        AttributeError,
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
    ):
        raise EvidenceManifestError("evidence manifest validation failed") from None


def encode_evidence_manifest(
    manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
) -> bytes:
    """Encode an evidence manifest into canonical UTF-8 JSON bytes."""
    validated = _deep_validate_manifest(manifest)
    try:
        canonical_dict = _to_canonical_dict(validated)
        json_str = json.dumps(
            canonical_dict,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = json_str.encode("utf-8")
    except EvidenceManifestError:
        raise
    except UnicodeEncodeError, OverflowError, ValueError, TypeError, RecursionError:
        raise EvidenceManifestError("evidence manifest encoding failed") from None

    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise EvidenceManifestError(
            f"encoded evidence manifest exceeds maximum size of 256 KiB ({_MAX_MANIFEST_BYTES} bytes)"
        )
    return encoded


def evidence_manifest_digest(
    manifest: ValidationEvidenceManifest | ReviewEvidenceManifest,
) -> str:
    """Compute the canonical lowercase SHA-256 digest of an evidence manifest."""
    return hashlib.sha256(encode_evidence_manifest(manifest)).hexdigest()


def _reject_constant(val: str) -> None:
    raise EvidenceManifestError(f"illegal JSON constant: {val}")


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise EvidenceManifestError("duplicate JSON key detected")
        obj[key] = value
    return obj


def decode_evidence_manifest(
    data: bytes,
) -> ValidationEvidenceManifest | ReviewEvidenceManifest:
    """Decode canonical bytes into a typed ValidationEvidenceManifest or ReviewEvidenceManifest."""
    if not isinstance(data, bytes):
        raise EvidenceManifestError("manifest data must be bytes")

    if len(data) > _MAX_MANIFEST_BYTES:
        raise EvidenceManifestError(
            f"evidence manifest exceeds maximum size of 256 KiB ({_MAX_MANIFEST_BYTES} bytes)"
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceManifestError("manifest contains invalid UTF-8") from None

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError, ValueError, RecursionError:
        raise EvidenceManifestError("manifest is not valid JSON") from None

    if not isinstance(raw, dict):
        raise EvidenceManifestError("evidence manifest root must be a JSON object")

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or isinstance(schema_version, bool) or schema_version != 1:
        raise EvidenceManifestError("unsupported or invalid schema_version")

    kind = raw.get("kind")
    if kind not in ("validation", "review"):
        raise EvidenceManifestError("unsupported or invalid manifest kind")

    policy_version = raw.get("policy_version")
    if (
        type(policy_version) is not int
        or isinstance(policy_version, bool)
        or policy_version < 1
        or policy_version > _MAX_POLICY_VERSION
    ):
        raise EvidenceManifestError("invalid policy_version")

    for uuid_key in ("evidence_set_id", "run_id", "step_id"):
        val = raw.get(uuid_key)
        if not isinstance(val, str) or _CANONICAL_UUID.fullmatch(val) is None:
            raise EvidenceManifestError(f"invalid canonical UUID string for {uuid_key}")

    head_sha = raw.get("head_sha")
    if not isinstance(head_sha, str) or _SHA1_HEX.fullmatch(head_sha) is None:
        raise EvidenceManifestError("invalid head_sha commit SHA")

    manifest: ValidationEvidenceManifest | ReviewEvidenceManifest
    try:
        if kind == "validation":
            prior_id = raw.get("prior_review_evidence_set_id")
            if prior_id is not None and (
                not isinstance(prior_id, str) or _CANONICAL_UUID.fullmatch(prior_id) is None
            ):
                raise EvidenceManifestError(
                    "invalid canonical UUID string for prior_review_evidence_set_id"
                )

            raw_members = raw.get("members", ())
            if not isinstance(raw_members, Sequence) or isinstance(raw_members, (str, bytes)):
                raise EvidenceManifestError("members must be a sequence")
            for rm in raw_members:
                if not isinstance(rm, dict):
                    raise EvidenceManifestError("member must be an object")
                cmd_ver = rm.get("command_version")
                if type(cmd_ver) is not int or isinstance(cmd_ver, bool):
                    raise EvidenceManifestError("command_version must be a strict integer")
                exit_c = rm.get("exit_code")
                if exit_c is not None and (type(exit_c) is not int or isinstance(exit_c, bool)):
                    raise EvidenceManifestError("exit_code must be a strict integer or None")

            manifest = ValidationEvidenceManifest.model_validate(raw)
        else:
            for review_uuid_key in ("producer_execution_id", "validation_evidence_set_id"):
                val = raw.get(review_uuid_key)
                if not isinstance(val, str) or _CANONICAL_UUID.fullmatch(val) is None:
                    raise EvidenceManifestError(
                        f"invalid canonical UUID string for {review_uuid_key}"
                    )

            manifest = ReviewEvidenceManifest.model_validate(raw)
    except EvidenceManifestError:
        raise
    except ValidationError, ValueError, TypeError, KeyError, OverflowError, RecursionError:
        raise EvidenceManifestError("evidence manifest validation failed") from None

    canonical_bytes = encode_evidence_manifest(manifest)
    if canonical_bytes != data:
        raise EvidenceManifestError("non-canonical evidence manifest wire representation")

    return manifest


__all__ = [
    "EvidenceManifest",
    "EvidenceManifestError",
    "EvidenceStatus",
    "ReviewEvidenceManifest",
    "ValidationEvidenceManifest",
    "ValidationEvidenceMember",
    "ValidationStatus",
    "decode_evidence_manifest",
    "encode_evidence_manifest",
    "evidence_manifest_digest",
]
