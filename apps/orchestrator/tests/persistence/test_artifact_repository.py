"""Integration tests for immutable artifact content and normalized lineage."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.persistence.repositories.artifacts import (
    ArtifactMetadataConflict,
    ArtifactNotFound,
    ArtifactRepository,
)


@pytest.mark.integration
async def test_same_digest_replay_and_cross_run_lineage_are_additive(
    tmp_path, session_factory, persisted_run
) -> None:
    other_run = type(persisted_run)(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other_run)
        await work.commit()

    descriptor = await FilesystemArtifactStore(tmp_path).put_bytes(
        b"content", media_type="text/plain"
    )
    repository = ArtifactRepository(session_factory)
    first = await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type="test",
        producer_id=uuid4(),
    )
    replay = await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type=first.producer_type,
        producer_id=first.producer_id,
        parent_digests=(),
    )
    second = await repository.record(
        descriptor,
        run_id=other_run.id,
        producer_type="test",
        producer_id=uuid4(),
    )

    assert replay.digest == first.digest == second.digest
    assert len(await repository.lineages(first.digest)) == 2


@pytest.mark.integration
async def test_concurrent_same_digest_recording_converges(
    tmp_path, session_factory, persisted_run
) -> None:
    descriptor = await FilesystemArtifactStore(tmp_path).put_bytes(
        b"concurrent", media_type="text/plain"
    )
    repository = ArtifactRepository(session_factory)
    producer_id = uuid4()

    results = await asyncio.gather(
        repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
            producer_id=producer_id,
        ),
        repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
            producer_id=producer_id,
        ),
    )

    assert [result.digest for result in results] == [descriptor.digest, descriptor.digest]
    assert len(await repository.lineages(descriptor.digest)) == 1


@pytest.mark.integration
async def test_immutable_metadata_and_lineage_parent_rules_fail_closed(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repository = ArtifactRepository(session_factory)
    descriptor = await store.put_bytes(b"content", media_type="text/plain")
    producer_id = uuid4()
    await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type="test",
        producer_id=producer_id,
    )

    with pytest.raises(ArtifactMetadataConflict):
        await repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="different",
            producer_id=producer_id,
        )
    with pytest.raises(ArtifactMetadataConflict):
        await repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
            producer_id=producer_id,
            parent_digests=(descriptor.digest,),
        )
    with pytest.raises(ArtifactMetadataConflict):
        await repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
            producer_id=producer_id,
            parent_digests=("0" * 64,),
        )


@pytest.mark.integration
async def test_repository_redacts_metadata_and_requires_run_for_lookup(
    tmp_path, session_factory, persisted_run
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repository = ArtifactRepository(session_factory)
    metadata = {"note": "safe", "authorization": "Bearer abcdefghijkl"}
    original = deepcopy(metadata)
    descriptor = await store.put_bytes(b"content", media_type="text/plain")

    recorded = await repository.record(
        descriptor,
        run_id=persisted_run.id,
        producer_type="test",
        producer_id=uuid4(),
        metadata=metadata,
    )

    assert metadata == original
    assert recorded.metadata["authorization"] == "[REDACTED]"
    with pytest.raises(TypeError):
        await repository.get_by_digest(descriptor.digest)
    with pytest.raises(ArtifactNotFound):
        await repository.get_by_digest(descriptor.digest, run_id=uuid4())
