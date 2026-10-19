"""Integration tests for immutable artifact content and normalized lineage."""

from __future__ import annotations

import asyncio
import traceback
from copy import deepcopy
from uuid import uuid4

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.persistence.repositories.artifacts import (
    ArtifactMetadataConflict,
    ArtifactNotFound,
    ArtifactRepository,
    ArtifactRepositoryError,
)
from sqlalchemy.exc import IntegrityError


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


@pytest.mark.integration
@pytest.mark.parametrize("digest", ("０" * 64, "١" * 64))
async def test_repository_rejects_non_ascii_digest_at_every_boundary(
    digest, tmp_path, session_factory, persisted_run
) -> None:
    repository = ArtifactRepository(session_factory)
    descriptor = await FilesystemArtifactStore(tmp_path).put_bytes(
        b"content", media_type="text/plain"
    )

    with pytest.raises(ValueError):
        await repository.get_by_digest(digest, run_id=persisted_run.id)
    with pytest.raises(ValueError):
        await repository.lineages(digest)
    with pytest.raises(ValueError):
        await repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
            parent_digests=(digest,),
        )


@pytest.mark.integration
async def test_database_integrity_error_is_translated_without_raw_context(
    tmp_path, session_factory, persisted_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "Bearer database-secret"
    descriptor = await FilesystemArtifactStore(tmp_path).put_bytes(
        b"content", media_type="text/plain"
    )
    repository = ArtifactRepository(session_factory)

    async def fail_content_row(*_args: object) -> None:
        raise IntegrityError(
            "INSERT INTO artifacts (artifact_metadata) VALUES (:payload)",
            {"payload": secret},
            RuntimeError(secret),
        )

    monkeypatch.setattr(repository, "_content_row", fail_content_row)
    with pytest.raises(ArtifactRepositoryError) as raised:
        await repository.record(
            descriptor,
            run_id=persisted_run.id,
            producer_type="test",
        )

    error = raised.value
    rendered = "".join(traceback.TracebackException.from_exception(error).format(chain=True))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in rendered
    assert "INSERT INTO artifacts" not in rendered

@pytest.mark.integration
async def test_raw_candidate_diff_reference_preserves_original_lineage_across_attempts_and_runs(
    tmp_path, session_factory, persisted_run
):
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    other = type(persisted_run)(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other)
        await work.commit()
    store = FilesystemArtifactStore(tmp_path)
    descriptor = await store.put_bytes(b'diff --git a/a b/a\n-old\n+new\n', media_type='text/plain')
    repo = ArtifactRepository(session_factory)
    original = await repo.record(
        descriptor, run_id=persisted_run.id, producer_type='subscription_candidate_diff',
        producer_id=uuid4(), metadata={'head_sha': 'a' * 40, 'base_sha': 'b' * 40,
                                      'candidate_tree_digest': 'c' * 64},
    )
    later_attempt = uuid4()
    replay = await repo.record_content_reference(
        descriptor, run_id=persisted_run.id, producer_type='subscription_candidate_diff',
        producer_id=later_attempt,
    )
    cross_run = await repo.record_content_reference(
        descriptor, run_id=other.id, producer_type='subscription_candidate_diff',
        producer_id=later_attempt,
    )
    assert replay == original
    assert cross_run.producer_id == later_attempt and cross_run.run_id == other.id
    assert cross_run.metadata == original.metadata
    assert await repo.get_by_digest(descriptor.digest, run_id=persisted_run.id) == original
    assert await store.open_bytes(descriptor.digest) == b'diff --git a/a b/a\n-old\n+new\n'


@pytest.mark.integration
@pytest.mark.parametrize('mismatch', ['kind', 'ordinary', 'parents', 'media', 'size', 'schema'])
async def test_raw_content_reference_rejects_other_authority_or_descriptor(
    tmp_path, session_factory, persisted_run, mismatch
):
    from dataclasses import replace

    store = FilesystemArtifactStore(tmp_path)
    descriptor = await store.put_bytes(b'diff body', media_type='text/plain')
    repo = ArtifactRepository(session_factory)
    parents = ()
    if mismatch == 'parents':
        parent = await store.put_bytes(b'parent', media_type='text/plain')
        await repo.record(parent, run_id=persisted_run.id, producer_type='test')
        parents = (parent.digest,)
    original = await repo.record(
        descriptor, run_id=persisted_run.id,
        producer_type='ordinary_receipt' if mismatch == 'ordinary' else (
            'other_authority' if mismatch == 'kind' else 'subscription_candidate_diff'
        ),
        producer_id=uuid4(), parent_digests=parents,
    )
    candidate = {
        'media': replace(descriptor, media_type='application/json'),
        'size': replace(descriptor, byte_count=10, original_byte_count=10),
        'schema': replace(descriptor, schema_version=2),
    }.get(mismatch, descriptor)
    with pytest.raises(ArtifactMetadataConflict):
        await repo.record_content_reference(
            candidate, run_id=persisted_run.id,
            producer_type='ordinary_receipt' if mismatch == 'ordinary' else 'subscription_candidate_diff',
            producer_id=uuid4(),
        )
    assert await repo.get_by_digest(descriptor.digest, run_id=persisted_run.id) == original
