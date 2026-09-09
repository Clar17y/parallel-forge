"""Bounded validation-attempt history with exact step/evidence associations."""

from uuid import UUID

from sqlalchemy import and_, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.policy import ProjectPolicy
from forge.persistence.models import (
    Artifact,
    EvidenceSet,
    ProjectPolicyVersion,
    Run,
    Step,
    ValidationResult,
)
from forge.persistence.repositories.runs import PersistenceDataError


async def check_history(
    factory: async_sessionmaker[AsyncSession], run_id: UUID, offset: int, limit: int
) -> tuple[list[dict[str, object]], bool] | None:
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        run = await session.get(Run, run_id)
        if run is None:
            return None
        policy_row = await session.get(ProjectPolicyVersion, (run.project_id, run.policy_version))
        if policy_row is None:
            raise PersistenceDataError("validation history policy missing")
        policy = ProjectPolicy.model_validate(policy_row.document)
        if policy.id != run.project_id or policy.version != run.policy_version:
            raise PersistenceDataError("validation history policy differs")
        evidence = (
            select(EvidenceSet.head_sha, EvidenceSet.manifest_artifact_id)
            .where(
                EvidenceSet.run_id == run_id,
                EvidenceSet.kind == "validation",
                EvidenceSet.step_id == ValidationResult.step_id,
            )
            .order_by(EvidenceSet.created_at.desc(), EvidenceSet.id.desc())
            .limit(1)
            .correlate(ValidationResult)
            .lateral()
        )
        rows = (
            await session.execute(
                select(
                    ValidationResult,
                    Step.attempt,
                    evidence.c.head_sha,
                    Artifact.digest.label("evidence_digest"),
                )
                .outerjoin(Step, and_(Step.id == ValidationResult.step_id, Step.run_id == run_id))
                .outerjoin(evidence, true())
                .outerjoin(Artifact, Artifact.id == evidence.c.manifest_artifact_id)
                .where(ValidationResult.run_id == run_id)
                .order_by(ValidationResult.started_at, ValidationResult.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        artifact_ids = [
            row[0].output_artifact_id for row in rows[:limit] if row[0].output_artifact_id
        ]
        output_digests: dict[UUID, str] = {}
        if artifact_ids:
            outputs = await session.execute(
                select(Artifact.id, Artifact.digest).where(Artifact.id.in_(artifact_ids))
            )
            output_digests = {artifact_id: digest for artifact_id, digest in outputs.all()}
        items = []
        for check, attempt, head_sha, evidence_digest in rows[:limit]:
            items.append(
                {
                    "id": check.id,
                    "name": check.check_name,
                    "command_name": check.command_name,
                    "command_version": check.command_version,
                    "status": check.status,
                    "exit_code": check.exit_code,
                    "output_artifact_digest": output_digests.get(check.output_artifact_id),
                    "completed_at": check.completed_at,
                    "head_sha": head_sha,
                    "step_id": check.step_id,
                    "attempt": attempt,
                    "started_at": check.started_at,
                    "duration_ms": int(
                        (check.completed_at - check.started_at).total_seconds() * 1000
                    )
                    if check.completed_at
                    else None,
                    "evidence_digest": evidence_digest,
                    "configured_runner_mode": policy.runner_mode.value,
                }
            )
        return items, len(rows) > limit
