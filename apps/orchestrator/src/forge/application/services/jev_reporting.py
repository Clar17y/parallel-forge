"""Read-only Jev run usage projection for operator surfaces."""

from collections.abc import Callable
from typing import Any, Protocol, Self
from uuid import UUID

from forge.domain.policy import JevPolicy
from forge.persistence.repositories.runs import RunNotFound


class JevReportingUnitOfWork(Protocol):
    runs: Any
    projects: Any
    jev: Any

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class JevReportingService:
    def __init__(self, unit_of_work_factory: Callable[[], JevReportingUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def report(self, run_id: UUID) -> dict[str, object] | None:
        async with self._unit_of_work_factory() as work:
            try:
                run = await work.runs.get(run_id)
            except RunNotFound:
                return None
            policy = None
            if run.policy_version is not None:
                record = await work.projects.get_policy(run.project_id, run.policy_version)
                raw = record.document.get("jev")
                if raw is not None:
                    policy = JevPolicy.model_validate(raw)
            summary = await work.jev.summary(run_id, policy=policy)
        return {"schema_version": 1, "run_id": str(run_id), **summary,
                "actual_model": summary.get("actual_model"),
                "review_focus_available": bool(summary.get("review_focus_available", False))}
