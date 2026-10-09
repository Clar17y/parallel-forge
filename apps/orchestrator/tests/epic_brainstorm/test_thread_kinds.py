from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.persistence.models.epic_brainstorm import BrainstormJobRow
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository

from apps.orchestrator.tests.epic_brainstorm.test_brainstorm_worker import prepared


@pytest.mark.asyncio
async def test_thread_projections_filter_job_kinds_and_keep_empty_and_legacy_conversations(
    brainstorm_session_factory,
):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    empty_id, empty_version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="empty", text="No job yet"
    )
    brainstorm_only_id, brainstorm_only_version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="brainstorm-only", text="Brainstorm"
    )
    brainstorm_turn = (await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=brainstorm_only_id
    ))[0]
    brainstorm_only_receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=brainstorm_only_id,
        prompt_turn_id=brainstorm_turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=brainstorm_only_version,
        actor=actor,
        key="brainstorm-only-submit",
    )
    decomp_only_id, decomp_only_version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="decomp", text="Decomposition"
    )
    async with brainstorm_session_factory() as session, session.begin():
        original = await session.get(BrainstormJobRow, receipt.job_id)
        snapshot = PostgresBrainstormRepository.decode_snapshot(original)
        mixed = snapshot.model_copy(
            update={"job_id": uuid4(), "reservation_id": uuid4(), "kind": "decomposition"}
        )
        session.add(BrainstormJobRow(
            id=mixed.job_id, epic_id=epic_id, project_id=project_id,
            conversation_id=mixed.conversation_id, version=1, state="queued",
            snapshot=PostgresBrainstormRepository.snapshot_payload(mixed),
        ))
        decomp_turn = (await service.turns(
            epic_id=epic_id, project_id=project_id, conversation_id=decomp_only_id
        ))[0]
        decomp_only = mixed.model_copy(update={
            "job_id": uuid4(), "reservation_id": uuid4(), "conversation_id": decomp_only_id,
            "prompt_turn_id": decomp_turn.turn_id, "conversation_version": decomp_only_version,
        })
        session.add(BrainstormJobRow(
            id=decomp_only.job_id, epic_id=epic_id, project_id=project_id,
            conversation_id=decomp_only_id, version=1, state="queued",
            snapshot=PostgresBrainstormRepository.snapshot_payload(decomp_only),
        ))
        legacy_job_id = uuid4()
        legacy_snapshot = snapshot.model_copy(update={"job_id": legacy_job_id, "reservation_id": uuid4()})
        legacy = PostgresBrainstormRepository.snapshot_payload(legacy_snapshot)
        legacy.pop("kind")
        base_time = datetime.now(UTC)
        original.created_at = base_time
        legacy_created_at = base_time + timedelta(seconds=1)
        session.add(BrainstormJobRow(
            id=legacy_job_id, epic_id=epic_id, project_id=project_id,
            conversation_id=snapshot.conversation_id, version=1, state="queued", snapshot=legacy,
            created_at=legacy_created_at, updated_at=legacy_created_at,
        ))

    brainstorm_threads = await service.threads(epic_id=epic_id, project_id=project_id, kind="brainstorm")
    decomposition = EpicDecompositionService(None, authoring_service=service)
    decomposition_threads = await decomposition.threads(epic_id=epic_id, project_id=project_id)
    bs_by_id = {thread.conversation_id: thread for thread in brainstorm_threads}
    dec_by_id = {thread.conversation_id: thread for thread in decomposition_threads}
    assert set(bs_by_id) == {snapshot.conversation_id, empty_id, brainstorm_only_id}
    assert set(dec_by_id) == {snapshot.conversation_id, empty_id, decomp_only_id}
    assert bs_by_id[snapshot.conversation_id].job_ids == (receipt.job_id, UUID(legacy["job_id"]))
    assert dec_by_id[snapshot.conversation_id].job_ids == (mixed.job_id,)
    assert bs_by_id[empty_id].job_ids == dec_by_id[empty_id].job_ids == ()
    assert bs_by_id[brainstorm_only_id].job_ids == (brainstorm_only_receipt.job_id,)
    assert brainstorm_only_id not in dec_by_id
    assert dec_by_id[decomp_only_id].job_ids == (decomp_only.job_id,)
    assert bs_by_id[snapshot.conversation_id].conversation_version == snapshot.conversation_version
    assert bs_by_id[empty_id].conversation_version == empty_version
    assert bs_by_id[brainstorm_only_id].conversation_version == brainstorm_only_version
    assert dec_by_id[decomp_only_id].conversation_version == decomp_only_version
    assert await service.threads(epic_id=epic_id, project_id=project_id, kind="brainstorm")
    assert await decomposition.threads(epic_id=epic_id, project_id=uuid4()) == ()
    wrong_epic = uuid4()
    assert await service.threads(epic_id=wrong_epic, project_id=project_id, kind="brainstorm") == ()
    assert await decomposition.threads(epic_id=wrong_epic, project_id=project_id) == ()
