"""Resolve advisory defaults without changing the approved project policy."""

from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.policy import JevPolicy
from forge.domain.run import RunSnapshot


async def run_jev_policy(
    work: UnitOfWork, run: RunSnapshot, project_jev: JevPolicy | None,
) -> JevPolicy | None:
    if project_jev is not None:
        return project_jev
    return await work.jev.policy_for_run(run.id, policy_version=run.policy_version)
