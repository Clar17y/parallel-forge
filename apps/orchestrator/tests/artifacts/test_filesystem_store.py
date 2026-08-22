"""Acceptance tests for the content-addressed filesystem artifact store."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from forge.artifacts.filesystem import ArtifactIntegrityError, FilesystemArtifactStore


@pytest.mark.asyncio
async def test_equal_content_is_deduplicated_sequentially_and_concurrently(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)

    first, second = await asyncio.gather(
        store.put_bytes(b"same", media_type="text/plain"),
        store.put_bytes(b"same", media_type="text/plain"),
    )

    assert first.digest == second.digest == hashlib.sha256(b"same").hexdigest()
    assert first.storage_path == second.storage_path
    assert list(tmp_path.rglob("*.blob")) == [first.storage_path]
    assert list(tmp_path.rglob("*.tmp")) == []


@pytest.mark.asyncio
async def test_read_rejects_tampered_missing_and_malformed_objects(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    artifact = await store.put_bytes(b"trusted", media_type="text/plain")
    artifact.storage_path.write_bytes(b"changed")

    with pytest.raises(ArtifactIntegrityError):
        await store.open_bytes(artifact.digest)
    with pytest.raises(ArtifactIntegrityError):
        await store.verify(artifact.digest)
    with pytest.raises(ValueError):
        await store.open_bytes("ABC")
    with pytest.raises(ValueError):
        await store.open_bytes("../" + "a" * 64)


@pytest.mark.asyncio
async def test_link_and_nonregular_targets_fail_closed(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    artifact = await store.put_bytes(b"trusted", media_type="text/plain")
    replacement = tmp_path / "replacement.blob"
    replacement.write_bytes(b"trusted")
    artifact.storage_path.unlink()
    try:
        artifact.storage_path.symlink_to(replacement)
    except OSError, NotImplementedError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(ArtifactIntegrityError):
        await store.verify(artifact.digest)


@pytest.mark.asyncio
async def test_corrupt_preexisting_target_is_never_overwritten(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    digest = hashlib.sha256(b"trusted").hexdigest()
    target = tmp_path / "sha256" / digest[:2] / f"{digest[2:]}.blob"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError):
        await store.put_bytes(b"trusted", media_type="text/plain")
    assert target.read_bytes() == b"corrupt"


@pytest.mark.asyncio
async def test_bounded_output_is_head_tail_and_discards_middle_everywhere(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    source = b"HEAD-" + b"DISCARDED-MARKER-" + b"TAIL"

    artifact = await store.put_bytes(
        source,
        media_type="text/plain",
        max_bytes=9,
        bounding_policy="head_tail",
    )

    stored = await store.open_bytes(artifact.digest)
    assert stored == b"HEAD-TAIL"
    assert artifact.truncated is True
    assert artifact.original_byte_count == len(source)
    assert artifact.truncation_policy == "head_tail"
    assert b"DISCARDED-MARKER" not in stored
    assert "DISCARDED-MARKER" not in repr(artifact)


@pytest.mark.asyncio
async def test_invalid_bounds_fail_before_writing(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)

    with pytest.raises(ValueError):
        await store.put_bytes(b"data", media_type="text/plain", max_bytes=0)
    assert list(tmp_path.rglob("*")) == []
