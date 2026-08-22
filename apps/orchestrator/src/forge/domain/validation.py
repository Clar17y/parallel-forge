"""Validation evidence and fail-closed runner boundary errors."""

from __future__ import annotations

import hashlib
import json
import re

from forge.domain.policy import CommandSpec

_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_IMAGE_REPOSITORY = re.compile(r"\A[a-z0-9][a-z0-9._:/-]*\Z", re.ASCII)


class UnknownNamedCommand(RuntimeError):
    """A requested name is not an exact command in the active policy."""

    def __init__(self) -> None:
        super().__init__("unknown named command")


def validate_runner_image_reference(value: str) -> str:
    """Accept only an unset value or an immutable SHA-256 image reference."""

    if not isinstance(value, str):
        raise TypeError("runner image reference must be a string")
    if value == "":
        return value
    if value != value.strip() or "\x00" in value or value.count("@") > 1:
        raise ValueError("runner image must use an immutable digest reference")

    repository: str | None = None
    digest = value
    if "@" in value:
        repository, digest = value.rsplit("@", maxsplit=1)
        if not repository or _IMAGE_REPOSITORY.fullmatch(repository) is None:
            raise ValueError("runner image must use an immutable digest reference")
    if (
        not digest.startswith("sha256:")
        or _DIGEST.fullmatch(digest.removeprefix("sha256:")) is None
    ):
        raise ValueError("runner image must use an immutable digest reference")
    return value


def runner_image_digest(reference: str) -> str:
    """Return the immutable digest component of a configured image reference."""

    validated = validate_runner_image_reference(reference)
    if not validated:
        raise ValueError("runner image digest is not configured")
    return validated.rsplit("@", maxsplit=1)[-1]


def command_spec_digest(spec: CommandSpec) -> str:
    """Hash the exact policy command vector and its execution constraints."""

    if not isinstance(spec, CommandSpec):
        raise TypeError("command digest requires a command specification")
    payload = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_evidence_digest(value: str, field_name: str) -> str:
    """Validate one canonical lower-case SHA-256 evidence digest."""

    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "UnknownNamedCommand",
    "command_spec_digest",
    "require_evidence_digest",
    "runner_image_digest",
    "validate_runner_image_reference",
]
