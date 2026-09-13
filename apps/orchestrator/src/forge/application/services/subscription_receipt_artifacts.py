"""Bounded receipt artifact closure and byte verification outside database locks."""

import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.artifact import ArtifactDescriptor


async def read_receipt_artifacts(
    work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
    store: ArtifactStore,
    run_id: UUID,
    roots: tuple[str, ...],
) -> tuple[dict[str, ArtifactDescriptor], dict[str, bytes]] | None:
    descriptors: dict[str, ArtifactDescriptor] = {}
    async with work_factory() as work:
        pending = list(roots)
        while pending:
            digest = pending.pop()
            if digest in descriptors:
                continue
            descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
            if (
                descriptor.digest != digest
                or descriptor.run_id != run_id
                or descriptor.truncated
                or descriptor.byte_count > 8 * 1024 * 1024
            ):
                return None
            descriptors[digest] = descriptor
            if (
                len(descriptors) > 256
                or sum(d.byte_count for d in descriptors.values()) > 64 * 1024 * 1024
            ):
                return None
            pending.extend(descriptor.parent_digests)
        await work.rollback()
    blobs = {}
    for digest, descriptor in descriptors.items():
        if await store.verify(digest) is not True:
            return None
        data = await store.open_bytes(digest, max_bytes=8 * 1024 * 1024)
        if (
            type(data) is not bytes
            or len(data) != descriptor.byte_count
            or hashlib.sha256(data).hexdigest() != digest
        ):
            return None
        blobs[digest] = data
    return descriptors, blobs
