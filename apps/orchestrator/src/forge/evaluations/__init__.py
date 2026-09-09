"""Evaluation fixture loading and deterministic materialization."""

from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.errors import (
    CredentialDetectedError,
    EvaluationFixtureError,
    FixtureNotFoundError,
    InvalidCaseContractError,
    MaterializationError,
    UnsafeFixturePathError,
)
from forge.evaluations.loader import (
    load_evaluation_case,
    load_evaluation_cases,
    load_expected_output,
)
from forge.evaluations.materializer import (
    MaterializedFixture,
    TemplateSnapshotFile,
    calculate_fixture_identity,
    materialize_fixture,
    validate_repository_template,
)

__all__ = [
    "CredentialDetectedError",
    "EvaluationCaseContract",
    "EvaluationFixtureError",
    "FixtureNotFoundError",
    "InvalidCaseContractError",
    "MaterializationError",
    "MaterializedFixture",
    "TemplateSnapshotFile",
    "UnsafeFixturePathError",
    "calculate_fixture_identity",
    "load_evaluation_case",
    "load_evaluation_cases",
    "load_expected_output",
    "materialize_fixture",
    "validate_repository_template",
]
