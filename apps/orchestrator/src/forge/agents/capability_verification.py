"""Shared invocation binding for asynchronous capability evidence verifiers."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.capability_evidence import (
    CapabilityEvidenceScope,
    validate_capability_installation_identity,
)


def capability_scope(request: SubscriptionInvocationRequest) -> CapabilityEvidenceScope:
    if type(request) is not SubscriptionInvocationRequest:
        raise TypeError("subscription request is required")
    return CapabilityEvidenceScope(
        route=request.task.route.effective,
        role=request.task.purpose,
        tool_surface=tuple(request.authorization.permitted_tools),
    )


async def capability_report[Report](
    candidate: Report | Awaitable[Report], expected_type: type[Report]
) -> Report:
    """Await a database-backed verifier while retaining simple deterministic fakes."""

    if isinstance(candidate, Awaitable):
        candidate = await candidate
    if type(candidate) is not expected_type:
        raise TypeError("capability verifier returned an invalid report")
    return candidate


_CLIENT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z", re.ASCII)


def validate_client_version(value: Any) -> None:
    """Require an exact released build, never a range, channel or alias.

    A version an operator can widen would let one conformance proof cover
    builds it never observed, so only an exact release identifies one.
    """

    if type(value) is not str or _CLIENT_VERSION.fullmatch(value) is None:
        raise ValueError("client version must be an exact released build")


def validate_installation_identity(account: Any, executable_digest: Any) -> None:
    """Reuse the closed evidence identity validators without accepting raw account data."""

    validate_capability_installation_identity(account, executable_digest)


def stable_executable_digest(filename: str) -> str | None:
    """Hash one stable regular file without exposing filesystem diagnostics."""

    try:
        path = Path(filename).resolve(strict=True)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
    except OSError:
        return None

    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    return digest.hexdigest() if identity(before) == identity(after) else None


__all__ = [
    "capability_report",
    "capability_scope",
    "stable_executable_digest",
    "validate_client_version",
    "validate_installation_identity",
]
