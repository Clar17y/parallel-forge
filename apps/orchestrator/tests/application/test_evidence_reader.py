from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID

import pytest
from forge.application.ports.evidence import (
    EvidenceCorruptLineage,
    EvidenceInputPurpose,
    EvidenceKind,
    EvidenceReadScope,
    EvidenceSetDescriptor,
)
from forge.application.services.evidence_reader import EvidenceReader
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from forge.domain.evidence import (
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    encode_evidence_manifest,
)

_RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_STEP_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_CONSUMER_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_SET_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_PRIOR_SET_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
_ARTIFACT_ID = UUID("11111111-1111-4111-8111-111111111111")
_HEAD = "f" * 40
_MEDIA_TYPE = "application/vnd.forge.evidence-manifest+json"


class _Artifacts:
    def __init__(self, descriptor: ArtifactDescriptor) -> None:
        self.descriptor = descriptor

    async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor:
        assert digest == self.descriptor.digest
        assert run_id == _RUN_ID
        return self.descriptor


class _Evidence:
    def __init__(
        self,
        inputs: dict[EvidenceInputPurpose, EvidenceSetDescriptor],
        sets: dict[UUID, EvidenceSetDescriptor] | None = None,
    ) -> None:
        self.inputs = inputs
        self.sets = sets or {value.evidence_set_id: value for value in inputs.values()}

    async def input_for_execution(
        self, purpose: EvidenceInputPurpose, scope: EvidenceReadScope
    ) -> EvidenceSetDescriptor | None:
        assert scope == _scope()
        return self.inputs.get(purpose)

    async def get_by_id(self, evidence_set_id: UUID, *, run_id: UUID) -> EvidenceSetDescriptor:
        assert run_id == _RUN_ID
        return self.sets[evidence_set_id]


class _Work(AbstractAsyncContextManager["_Work"]):
    def __init__(self, evidence: _Evidence, artifacts: _Artifacts) -> None:
        self.evidence = evidence
        self.artifacts = artifacts

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Store:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.verify_calls = 0
        self.open_calls = 0

    async def verify(self, digest: str) -> bool:
        self.verify_calls += 1
        return hashlib.sha256(self.data).hexdigest() == digest

    async def open_bytes(self, digest: str) -> bytes:
        self.open_calls += 1
        assert digest == hashlib.sha256(self.data).hexdigest()
        return self.data


def _scope() -> EvidenceReadScope:
    return EvidenceReadScope(_RUN_ID, 1, _CONSUMER_ID, _STEP_ID, _HEAD)


def _descriptor(
    digest: str, byte_count: int, *, evidence_set_id: UUID = _SET_ID, parents: tuple[str, ...] = ()
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        digest=digest,
        media_type=_MEDIA_TYPE,
        byte_count=byte_count,
        storage_path=Path(canonical_storage_pointer(digest)),
        producer_type="evidence_set",
        producer_id=evidence_set_id,
        run_id=_RUN_ID,
        parent_digests=parents,
        artifact_id=_ARTIFACT_ID,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _bound(descriptor: ArtifactDescriptor, *, prior: UUID | None = None) -> EvidenceSetDescriptor:
    return EvidenceSetDescriptor(
        _SET_ID,
        _RUN_ID,
        _STEP_ID,
        EvidenceKind.VALIDATION,
        1,
        _HEAD,
        None,
        _ARTIFACT_ID,
        descriptor.digest,
        _MEDIA_TYPE,
        descriptor.byte_count,
        1,
        None,
        prior,
        None,
    )


async def test_reader_returns_canonical_validation_manifest_and_checks_prior_parent() -> None:
    prior_digest = "1" * 64
    manifest = ValidationEvidenceManifest(
        evidence_set_id=_SET_ID,
        run_id=_RUN_ID,
        step_id=_STEP_ID,
        policy_version=1,
        head_sha=_HEAD,
        prior_review_evidence_set_id=_PRIOR_SET_ID,
    )
    data = encode_evidence_manifest(manifest)
    descriptor = _descriptor(hashlib.sha256(data).hexdigest(), len(data), parents=(prior_digest,))
    prior = EvidenceSetDescriptor(
        _PRIOR_SET_ID,
        _RUN_ID,
        _STEP_ID,
        EvidenceKind.REVIEW,
        1,
        "0" * 40,
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
        prior_digest,
        _MEDIA_TYPE,
        10,
        1,
        _SET_ID,
        None,
        (),
    )
    work = _Work(
        _Evidence(
            {
                EvidenceInputPurpose.VALIDATION_RESULTS: _bound(descriptor, prior=_PRIOR_SET_ID),
                EvidenceInputPurpose.PRIOR_REVIEW: prior,
            }
        ),
        _Artifacts(descriptor),
    )
    reader = EvidenceReader(lambda: work, _Store(data))

    result = await reader.read(EvidenceInputPurpose.VALIDATION_RESULTS, _scope())

    assert result == manifest


async def test_reader_returns_none_for_legitimately_absent_prior_review() -> None:
    work = _Work(_Evidence({}), _Artifacts(_descriptor("1" * 64, 0)))

    assert (
        await EvidenceReader(lambda: work, _Store(b"")).read(
            EvidenceInputPurpose.PRIOR_REVIEW, _scope()
        )
        is None
    )


@pytest.mark.parametrize("substituted_parent", (False, True))
async def test_reader_follows_exact_historical_validation_parent_for_review(
    substituted_parent: bool,
) -> None:
    validation_digest = "4" * 64
    validation_set_id = UUID("44444444-4444-4444-8444-444444444444")
    producer_id = UUID("55555555-5555-4555-8555-555555555555")
    manifest = ReviewEvidenceManifest(
        evidence_set_id=_SET_ID,
        run_id=_RUN_ID,
        step_id=_STEP_ID,
        policy_version=1,
        head_sha="0" * 40,
        producer_execution_id=producer_id,
        validation_evidence_set_id=validation_set_id,
        review=ReviewOutput(
            decision=ReviewDecision.APPROVE,
            tested_claims=(),
            missing_evidence=(),
            summary="approved",
        ),
    )
    data = encode_evidence_manifest(manifest)
    descriptor = _descriptor(
        hashlib.sha256(data).hexdigest(), len(data), parents=(validation_digest,)
    )
    bound = EvidenceSetDescriptor(
        _SET_ID,
        _RUN_ID,
        _STEP_ID,
        EvidenceKind.REVIEW,
        1,
        "0" * 40,
        producer_id,
        _ARTIFACT_ID,
        descriptor.digest,
        _MEDIA_TYPE,
        descriptor.byte_count,
        1,
        validation_set_id,
        None,
        (),
    )
    historical_validation = EvidenceSetDescriptor(
        validation_set_id,
        _RUN_ID,
        _STEP_ID,
        EvidenceKind.VALIDATION,
        1,
        "0" * 40,
        None,
        UUID("66666666-6666-4666-8666-666666666666"),
        validation_digest,
        _MEDIA_TYPE,
        10,
        1,
        None,
        None,
        None,
    )
    if substituted_parent:
        historical_validation = replace(historical_validation, manifest_digest="5" * 64)
    work = _Work(
        _Evidence(
            {EvidenceInputPurpose.PRIOR_REVIEW: bound}, {validation_set_id: historical_validation}
        ),
        _Artifacts(descriptor),
    )

    reader = EvidenceReader(lambda: work, _Store(data))
    if substituted_parent:
        with pytest.raises(EvidenceCorruptLineage):
            await reader.read(EvidenceInputPurpose.PRIOR_REVIEW, _scope())
    else:
        assert await reader.read(EvidenceInputPurpose.PRIOR_REVIEW, _scope()) == manifest


@pytest.mark.parametrize(
    "variant",
    (
        "missing",
        "oversized",
        "noncanonical",
        "wrong_parent",
        "wrong_head",
        "wrong_run",
        "wrong_producer",
    ),
)
async def test_reader_fails_closed_for_corrupt_bound_evidence(variant: str) -> None:
    manifest = ValidationEvidenceManifest(
        evidence_set_id=_SET_ID, run_id=_RUN_ID, step_id=_STEP_ID, policy_version=1, head_sha=_HEAD
    )
    data = encode_evidence_manifest(manifest)
    descriptor = _descriptor(hashlib.sha256(data).hexdigest(), len(data))
    store_data = data
    bound = _bound(descriptor)
    if variant == "missing":
        store_data = b"missing"
    elif variant == "oversized":
        descriptor = replace(descriptor, byte_count=262_145, original_byte_count=262_145)
        bound = _bound(descriptor)
    elif variant == "noncanonical":
        store_data = data[:-1] + b" }"
        descriptor = _descriptor(hashlib.sha256(store_data).hexdigest(), len(store_data))
        bound = _bound(descriptor)
    elif variant == "wrong_parent":
        descriptor = replace(descriptor, parent_digests=("2" * 64,))
        bound = _bound(descriptor)
    elif variant == "wrong_head":
        bound = replace(bound, head_sha="0" * 40)
    elif variant == "wrong_run":
        descriptor = replace(descriptor, run_id=UUID("77777777-7777-4777-8777-777777777777"))
    else:
        descriptor = replace(descriptor, producer_id=UUID("88888888-8888-4888-8888-888888888888"))
    work = _Work(
        _Evidence({EvidenceInputPurpose.VALIDATION_RESULTS: bound}), _Artifacts(descriptor)
    )

    with pytest.raises(EvidenceCorruptLineage):
        await EvidenceReader(lambda: work, _Store(store_data)).read(
            EvidenceInputPurpose.VALIDATION_RESULTS, _scope()
        )
