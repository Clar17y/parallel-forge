"""Strict credential and secret scanner for evaluation fixtures."""

from __future__ import annotations

import re

from forge.evaluations.errors import CredentialDetectedError

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub personal access token", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[a-zA-Z0-9_]{50,}")),
    ("GitHub OAuth/server token", re.compile(r"gh[os]_[a-zA-Z0-9]{36}")),
    ("API secret key", re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private encryption key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"\bBearer\s+[a-zA-Z0-9_\-\.]{20,}\b", re.IGNORECASE)),
    (
        "Hardcoded secret assignment",
        re.compile(
            r"""\b(?:password|passwd|secret|api_key|auth_token)\s*[:=]\s*["'][a-zA-Z0-9_\-\.]{8,}["']""",
            re.IGNORECASE,
        ),
    ),
)


def assert_credential_free(text: str, source_identifier: str = "content") -> None:
    """Scan text and raise CredentialDetectedError if any credential pattern is matched."""
    for description, pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            raise CredentialDetectedError(
                f"Credential detected in {source_identifier}: {description}"
            )


__all__ = ["assert_credential_free"]
