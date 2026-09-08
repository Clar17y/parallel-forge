"""Caller-owned PostgreSQL transactions for versioned evaluation results."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.artifact import validate_artifact_digest
from forge.persistence.models import AgentExecution, ModelUsage
from forge.persistence.models.evaluation import EvaluationCase, EvaluationSuite
from forge.persistence.repositories.artifacts import ArtifactNotFound, ArtifactRepository


class EvaluationConflict(RuntimeError):
    """An evaluation identity or settlement conflicts with durable evidence."""


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    id: UUID
    name: str
    fixture_version: str
    metric_version: str
    status: str


def _text(value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError("invalid evaluation identity")


def _summary(row: EvaluationSuite) -> EvaluationSummary:
    return EvaluationSummary(row.id, row.name, row.fixture_version, row.metric_version, row.status)


class EvaluationRepository:
    """Serialize suite changes on the suite row; callers commit or roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_suite(
        self, *, name: str, fixture_version: str, metric_version: str,
        idempotency_key: str, cases: Mapping[str, str],
    ) -> EvaluationSummary:
        for value, maximum in ((name, 128), (fixture_version, 64), (metric_version, 64), (idempotency_key, 255)):
            _text(value, maximum)
        if not cases or len(cases) > 1000:
            raise ValueError("evaluation suite requires a bounded nonempty manifest")
        manifest = dict(cases)
        for key, role in manifest.items():
            _text(key, 255)
            if role not in {"planner", "developer", "reviewer"}:
                raise ValueError("invalid evaluation role")
        created = await self._session.scalar(
            insert(EvaluationSuite).values(
                id=uuid4(), name=name, fixture_version=fixture_version, metric_version=metric_version,
                idempotency_key=idempotency_key, status="running",
            ).on_conflict_do_nothing(index_elements=["idempotency_key"]).returning(EvaluationSuite.id)
        )
        suite = await self._session.scalar(
            select(EvaluationSuite).where(EvaluationSuite.idempotency_key == idempotency_key)
            .with_for_update().execution_options(populate_existing=True)
        )
        if suite is None or (suite.name, suite.fixture_version, suite.metric_version) != (
            name, fixture_version, metric_version,
        ):
            raise EvaluationConflict("evaluation suite identity differs")
        if created is not None:
            self._session.add_all([
                EvaluationCase(id=uuid4(), suite_id=suite.id, case_key=key, role=role,
                               fixture_version=fixture_version, metric_version=metric_version,
                               status="pending", metrics={})
                for key, role in sorted(manifest.items())
            ])
            await self._session.flush()
        else:
            existing = {key: role for key, role in (await self._session.execute(
                select(EvaluationCase.case_key, EvaluationCase.role).where(EvaluationCase.suite_id == suite.id)
            )).all()}
            if existing != manifest:
                raise EvaluationConflict("evaluation case manifest differs")
        return _summary(suite)

    async def record_case(
        self, *, suite_id: UUID, case_key: str, run_id: UUID, usage_id: UUID,
        input_digest: str, output_digest: str, passed: bool,
        metrics: Mapping[str, int | float | bool | None],
    ) -> None:
        """Settle once using persisted, same-execution usage and output lineage."""
        if type(passed) is not bool:
            raise ValueError("evaluation result must be explicit")
        validate_artifact_digest(input_digest)
        validate_artifact_digest(output_digest)
        scores = dict(metrics)
        if not scores or len(scores) > 100:
            raise ValueError("evaluation metrics must be bounded and nonempty")
        for key, value in scores.items():
            if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", key) is None:
                raise ValueError("invalid evaluation metric name")
            if value is not None:
                try:
                    valid = type(value) in {int, float, bool} and math.isfinite(value)
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError("evaluation metrics must be finite numeric values")
        suite = await self._locked_suite(suite_id)
        case = await self._session.scalar(select(EvaluationCase).where(
            EvaluationCase.suite_id == suite_id, EvaluationCase.case_key == case_key,
        ).execution_options(populate_existing=True))
        if case is None:
            raise EvaluationConflict("evaluation case is absent")
        bound = (await self._session.execute(
            select(ModelUsage, AgentExecution).join(
                AgentExecution, AgentExecution.id == ModelUsage.agent_execution_id,
            ).where(ModelUsage.id == usage_id, ModelUsage.run_id == run_id, AgentExecution.run_id == run_id)
        )).one_or_none()
        if bound is None:
            raise EvaluationConflict("evaluation usage is not bound to the run")
        usage, execution = bound
        if (execution.role != case.role or execution.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
                or usage.provider != execution.provider or usage.model != execution.model
                or (passed and execution.status != "SUCCEEDED")):
            raise EvaluationConflict("evaluation execution evidence differs")
        artifacts = ArtifactRepository(session=self._session)
        try:
            source = await artifacts.get_by_digest(input_digest, run_id=run_id)
            output = await artifacts.get_by_digest(output_digest, run_id=run_id)
        except ArtifactNotFound:
            raise EvaluationConflict("evaluation artifact lineage is absent") from None
        if (output.producer_id != execution.id or input_digest not in output.parent_digests
                or execution.input_artifact_id != source.artifact_id
                or execution.output_artifact_id != output.artifact_id):
            raise EvaluationConflict("evaluation output is not bound to its input and execution")
        status = "passed" if passed else "failed"
        result = (status, scores, usage_id, input_digest, output_digest)
        if case.status in {"passed", "failed", "skipped"}:
            if (case.status, case.metrics, case.model_usage_id,
                    case.input_artifact_digest, case.output_artifact_digest) != result:
                raise EvaluationConflict("evaluation result differs on replay")
            return
        if suite.status != "running" or case.status not in {"pending", "running"}:
            raise EvaluationConflict("evaluation case cannot settle")
        case.status, case.metrics, case.model_usage_id = status, scores, usage_id
        case.input_artifact_digest, case.output_artifact_digest = input_digest, output_digest
        case.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def finish_suite(self, suite_id: UUID) -> EvaluationSummary:
        suite = await self._locked_suite(suite_id)
        states = tuple((await self._session.scalars(
            select(EvaluationCase.status).where(EvaluationCase.suite_id == suite_id)
        )).all())
        if not states or any(state not in {"passed", "failed", "skipped"} for state in states):
            raise EvaluationConflict("evaluation suite has unsettled cases")
        status = "passed" if all(state == "passed" for state in states) else "failed"
        if suite.status not in {"running", status}:
            raise EvaluationConflict("evaluation suite settlement differs")
        suite.status = status
        await self._session.flush()
        return _summary(suite)

    async def _locked_suite(self, suite_id: UUID) -> EvaluationSuite:
        suite = await self._session.scalar(
            select(EvaluationSuite).where(EvaluationSuite.id == suite_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if suite is None:
            raise EvaluationConflict("evaluation suite is absent")
        return suite
