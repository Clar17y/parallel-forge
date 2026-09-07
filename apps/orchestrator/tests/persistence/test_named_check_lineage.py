"""PostgreSQL lineage contracts used by named-check receipt recovery."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.persistence.repositories.artifacts import ArtifactRepository


@pytest.mark.integration
async def test_named_check_output_lineage_reuses_same_run_content_and_isolates_runs(
    tmp_path, session_factory, persisted_run
) -> None:
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    other = type(persisted_run)(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other)
        await work.commit()

    store = FilesystemArtifactStore(tmp_path)
    descriptor = await store.put_bytes(b'{"stream":"stdout"}', media_type="application/json")
    repository = ArtifactRepository(session_factory)
    first = await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type="command_output",
        producer_id=persisted_run.id,
    )
    replay = await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type="command_output",
        producer_id=persisted_run.id,
    )
    second = await repository.record(
        descriptor, run_id=other.id, producer_type="command_output", producer_id=other.id
    )

    assert first.digest == replay.digest == second.digest
    assert len(await repository.lineages(descriptor.digest)) == 2
    assert await repository.get_by_producer(
        run_id=persisted_run.id,
        producer_type="command_output",
        producer_id=persisted_run.id,
    ) == (first,)


@pytest.mark.integration
async def test_exact_producer_lookup_returns_all_matches_for_recovery_to_reject_as_ambiguous(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repository = ArtifactRepository(session_factory)
    producer_id = uuid4()
    first = await store.put_bytes(b"one", media_type="application/json")
    second = await store.put_bytes(b"two", media_type="application/json")
    await repository.record(
        first, run_id=persisted_run.id, producer_type="named_check", producer_id=producer_id
    )
    await repository.record(
        second, run_id=persisted_run.id, producer_type="named_check", producer_id=producer_id
    )

    matches = await repository.get_by_producer(
        run_id=persisted_run.id, producer_type="named_check", producer_id=producer_id
    )

    assert {item.digest for item in matches} == {first.digest, second.digest}
