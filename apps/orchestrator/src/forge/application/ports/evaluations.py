"""Evaluation application ports and result contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from forge.application.ports.agents import AgentGateway
from forge.domain.actor import AgentRole
from forge.evaluations.contracts import EvaluationCaseContract


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    """Outcome of evaluating one individual fixture case."""

    case_key: str
    role: AgentRole
    passed: bool
    status: str
    metrics: Mapping[str, int | float | bool | None]
    run_id: UUID
    execution_id: UUID
    usage_id: UUID | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    regression_failures: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationSuiteResult:
    """Outcome of evaluating a complete suite of cases."""

    suite_id: UUID
    name: str
    fixture_version: str
    metric_version: str
    status: str
    cases: tuple[EvaluationCaseResult, ...] = ()
    regressions: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "passed" and not self.regressions


@runtime_checkable
class EvaluationServicePort(Protocol):
    """Execution boundary running agent evaluation suites."""

    async def run_suite(
        self,
        suite_name: str = "deterministic",
        *,
        idempotency_key: str | None = None,
        fixture_version: str | None = None,
        metric_version: str | None = None,
        fixtures_dir: Path | None = None,
        expected_dir: Path | None = None,
        cases: Mapping[str, EvaluationCaseContract] | Sequence[EvaluationCaseContract] | None = None,
        gateway: AgentGateway | None = None,
        provider_reference: str | None = None,
        live_model: str | None = None,
        baseline_fixture_version: str | None = None,
        baseline_metric_version: str | None = None,
        floors: Mapping[str, float] | None = None,
        ceilings: Mapping[str, float] | None = None,
        is_live: bool = False,
        promoted_baseline: bool = False,
        baseline_name: str | None = None,
        baseline_id: UUID | None = None,
    ) -> EvaluationSuiteResult: ...

    async def promote_baseline(
        self,
        suite_id: UUID,
        name: str = "live",
        *,
        floors: Mapping[str, float] | None = None,
        ceilings: Mapping[str, float] | None = None,
        promoted_by: str = "operator",
    ) -> Any: ...


__all__ = [
    "EvaluationCaseResult",
    "EvaluationServicePort",
    "EvaluationSuiteResult",
]
