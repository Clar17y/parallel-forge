"""Exceptions for evaluation fixture loading, validation, and materialization."""

from __future__ import annotations


class EvaluationFixtureError(Exception):
    """Base exception for all evaluation fixture operations."""


class InvalidCaseContractError(EvaluationFixtureError, ValueError):
    """Raised when an evaluation case JSON contract violates schema, role, or shape rules."""


class UnsafeFixturePathError(EvaluationFixtureError, ValueError):
    """Raised when a repository template path violates safety rules (e.g. symlink, traversal)."""


class CredentialDetectedError(EvaluationFixtureError, ValueError):
    """Raised when potential credentials or secrets are detected in fixtures."""


class MaterializationError(EvaluationFixtureError, RuntimeError):
    """Raised when Git repository materialization fails."""


class FixtureNotFoundError(EvaluationFixtureError, FileNotFoundError):
    """Raised when a fixture file or directory cannot be found."""


__all__ = [
    "CredentialDetectedError",
    "EvaluationFixtureError",
    "FixtureNotFoundError",
    "InvalidCaseContractError",
    "MaterializationError",
    "UnsafeFixturePathError",
]
