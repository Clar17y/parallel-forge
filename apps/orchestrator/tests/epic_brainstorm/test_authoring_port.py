from uuid import uuid4

import pytest
from forge.application.services.epic_brainstorm import BoundEpicAuthoringAdapter
from forge.domain.epic_brainstorm import BrainstormConflict
from forge.domain.subscription import RouteBinding, RouteSpec
from forge.persistence.models.epic_brainstorm import BrainstormJobRow
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

from apps.orchestrator.tests.epic_brainstorm.test_worker import prepared


@pytest.mark.asyncio
async def test_bound_authoring_port_rejects_subject_and_route_rebinding(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, first.job_id)
        snapshot = PostgresBrainstormRepository.decode_snapshot(row)
    bound = BoundEpicAuthoringAdapter(
        service, epic_id=epic_id, project_id=project_id, actor=actor, idempotency_key="bound-submit"
    )
    new_job = snapshot.model_copy(update={"job_id": uuid4(), "reservation_id": uuid4()})
    with pytest.raises(BrainstormConflict, match="subject"):
        await bound.submit(new_job.model_copy(update={"project_id": uuid4()}))
    other = RouteSpec(provider="fake", client="fake", model="other")
    with pytest.raises(BrainstormConflict, match="snapshot"):
        await bound.submit(
            new_job.model_copy(update={"route": RouteBinding(requested=other, effective=other)})
        )
    receipt = await bound.submit(new_job)
    assert receipt.job_id == new_job.job_id
    assert (await bound.observe(receipt.job_id)).state == "queued"
    threads = await service.threads(epic_id=epic_id, project_id=project_id)
    assert len(threads) == 1
    assert threads[0].conversation_version == snapshot.conversation_version
    assert threads[0].job_ids == (first.job_id, receipt.job_id)
    assert await service.threads(epic_id=epic_id, project_id=uuid4()) == ()
    assert await bound.submit(new_job) == receipt
    distinct = new_job.model_copy(update={"job_id": uuid4(), "reservation_id": uuid4()})
    assert (await bound.submit(distinct)).job_id == distinct.job_id
    with pytest.raises(BrainstormConflict, match="payload"):
        await bound.submit(new_job.model_copy(update={"reservation_id": uuid4()}))
