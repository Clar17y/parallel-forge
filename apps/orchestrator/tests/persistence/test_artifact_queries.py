import forge.persistence.queries.artifacts as artifact_queries
import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.persistence.queries.artifacts import PostgresArtifactReadQuery
from forge.persistence.repositories.artifacts import ArtifactRepository


@pytest.mark.asyncio
async def test_artifact_query_rejects_noncanonical_digest(session_factory) -> None:
    query = PostgresArtifactReadQuery(session_factory)
    with pytest.raises(ValueError):
        await query.get_by_digest("A" * 64)


@pytest.mark.asyncio
async def test_artifact_query_returns_persisted_lineage(
    session_factory, persisted_run, tmp_path
) -> None:
    data = b"persisted artifact"
    descriptor = await FilesystemArtifactStore(tmp_path).put_bytes(data, media_type="text/plain")
    await ArtifactRepository(session_factory).record(
        descriptor, run_id=persisted_run.id, producer_type="test"
    )
    records = await PostgresArtifactReadQuery(session_factory).get_by_digest(descriptor.digest)
    assert len(records) == 1
    assert records[0].digest == descriptor.digest
    assert records[0].run_id == persisted_run.id
    assert records[0].parent_digests == ()


@pytest.mark.asyncio
async def test_artifact_query_bounds_parent_edges(
    session_factory, persisted_run, tmp_path, monkeypatch
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    repository = ArtifactRepository(session_factory)
    parents = []
    for value in (b"parent-a", b"parent-b", b"parent-c"):
        descriptor = await store.put_bytes(value, media_type="text/plain")
        await repository.record(descriptor, run_id=persisted_run.id, producer_type="test")
        parents.append(descriptor)
    child_two = await store.put_bytes(b"child-two", media_type="text/plain")
    await repository.record(
        child_two,
        run_id=persisted_run.id,
        producer_type="test",
        parent_digests=sorted(parent.digest for parent in parents[:2]),
    )
    monkeypatch.setattr(artifact_queries, "MAX_PARENTS", 2)
    query = PostgresArtifactReadQuery(session_factory)
    records = await query.get_by_digest(child_two.digest)
    assert len(records[0].parent_digests) == 2
    child_three = await store.put_bytes(b"child-three", media_type="text/plain")
    await repository.record(
        child_three,
        run_id=persisted_run.id,
        producer_type="test",
        parent_digests=sorted(parent.digest for parent in parents),
    )
    with pytest.raises(RuntimeError, match="too many parents"):
        await query.get_by_digest(child_three.digest)
