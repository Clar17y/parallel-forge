import sys
from datetime import UTC, datetime, timedelta

import pytest
from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor
from forge.domain.epic_brainstorm import BrainstormConflict
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.worker.epic_brainstorm import DurableBrainstormProcessLifecycle

from apps.orchestrator.tests.epic_brainstorm.test_brainstorm_worker import prepared


@pytest.mark.asyncio
async def test_missing_terminal_proof_cannot_settle(brainstorm_session_factory):
    _, _, _, _, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("worker-a")
        assert claimed is not None
        _, attempt = claimed
        attempt_id, fence = attempt.id, attempt.fence
    lifecycle = DurableBrainstormProcessLifecycle(
        brainstorm_session_factory, attempt_id, fence, "worker-a"
    )
    await lifecycle.launch_intent("launch-a")
    await lifecycle.finished(None, None)
    async with brainstorm_session_factory() as session:
        attempt = await session.get(BrainstormAttemptRow, attempt_id)
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert attempt is not None and not attempt.process_settled
        assert job is not None and job.state == "running"


@pytest.mark.asyncio
async def test_physical_child_stopped_after_started_receipt_loses_authority(
    brainstorm_session_factory,
):
    _, _, _, _, _ = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("worker-a")
        assert claimed is not None
        _, attempt = claimed
        attempt_id, fence = attempt.id, attempt.fence
    lifecycle = DurableBrainstormProcessLifecycle(
        brainstorm_session_factory, attempt_id, fence, "worker-a"
    )

    class ExpireAtStarted:
        async def launch_intent(self, launch_id):
            await lifecycle.launch_intent(launch_id)

        async def started(self, process_receipt):
            async with brainstorm_session_factory() as session, session.begin():
                row = await session.get(BrainstormAttemptRow, attempt_id, with_for_update=True)
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await lifecycle.started(process_receipt)

        async def finished(self, process_receipt, result):
            await lifecycle.finished(process_receipt, result)

    spec = ClientLaunchSpec(
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=".",
        environment={},
        duration_seconds=5,
    )
    with pytest.raises(BrainstormConflict, match="authority expired"):
        await ClientProcessSupervisor().start(spec, lifecycle=ExpireAtStarted())
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormAttemptRow, attempt_id)
        assert row is not None and row.launch_intent and row.process_started
        assert row.process_pid is not None
        assert row.process_settled and row.terminal_proof is not None
