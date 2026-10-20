"""Bounded validation-attempt history with exact step/evidence associations."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.policy import ProjectPolicy
from forge.persistence.models import ProjectPolicyVersion, Run
from forge.persistence.queries.local_checks import local_checks
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
        return await local_checks(session, run, policy, offset, limit)
