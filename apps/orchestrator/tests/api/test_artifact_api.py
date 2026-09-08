from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from forge.api.dependencies import require_operator
from forge.api.routes.artifacts import router_for
from forge.application.services.artifact_reads import ArtifactReadError, ArtifactReadService
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from httpx import ASGITransport, AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _descriptor(data: bytes, media_type: str = "text/plain") -> ArtifactDescriptor:
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    return ArtifactDescriptor(
        digest=digest,
        media_type=media_type,
        byte_count=len(data),
        storage_path=Path(canonical_storage_pointer(digest)),
        run_id=uuid4(),
        created_at=datetime.now(UTC),
        metadata={"safe": "value", "storage_path": "hidden", "hidden_reasoning": "private"},
    )


class _Query:
    def __init__(self, records):
        self.records = tuple(records)

    async def get_by_digest(self, digest: str):
        return self.records


class _Store:
    def __init__(self, data: bytes):
        self.data = data

    async def open_bytes(self, digest: str, *, max_bytes=None) -> bytes:
        return self.data


def _app(service: ArtifactReadService) -> FastAPI:
    app = FastAPI()
    app.state.artifact_read_service = service
    app.include_router(router_for(), prefix="/api")
    app.dependency_overrides[require_operator] = lambda: object()
    return app


async def _get(app: FastAPI, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_review_representation_retains_explicit_decision_with_no_findings() -> None:
    from forge.domain.agent import ReviewDecision, ReviewOutput
    from forge.domain.evidence import ReviewEvidenceManifest, encode_evidence_manifest

    manifest = ReviewEvidenceManifest(
        evidence_set_id=uuid4(),
        run_id=uuid4(),
        step_id=uuid4(),
        policy_version=1,
        head_sha="a" * 40,
        producer_execution_id=uuid4(),
        validation_evidence_set_id=uuid4(),
        review=ReviewOutput(
            decision=ReviewDecision.REQUEST_CHANGES,
            tested_claims=(),
            missing_evidence=("No test evidence",),
            summary="Evidence missing",
        ),
    )
    data = encode_evidence_manifest(manifest)
    descriptor = _descriptor(data, "application/vnd.forge.evidence-manifest+json")
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    response = await _get(app, f"/api/artifacts/{descriptor.digest}/review")
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "request_changes"
    assert response.json()["head_sha"] == manifest.head_sha
    assert response.json()["run_id"] == str(manifest.run_id)
    assert response.json()["missing_evidence"] == ["No test evidence"]
    app.dependency_overrides[require_operator] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="authentication required")
    )
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}/review")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,media_type",
    [
        (b"not json", "application/vnd.forge.evidence-manifest+json"),
        (b'{"schema_version":99}', "application/vnd.forge.evidence-manifest+json"),
        (b"plain text", "text/plain"),
    ],
)
async def test_review_representation_rejects_wrong_or_unknown_content(data, media_type):
    descriptor = _descriptor(data, media_type)
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}/review")).status_code == 422
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}/reviewer-diff")).status_code == 422


@pytest.mark.asyncio
async def test_artifact_routes_auth_metadata_text_and_download() -> None:
    data = b'{"text":"safe"}'
    descriptor = _descriptor(data, "application/json")
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    metadata = await _get(app, f"/api/artifacts/{descriptor.digest}")
    assert metadata.status_code == 200
    assert metadata.json()["lineage"][0]["run_id"] == str(descriptor.run_id)
    assert "storage_path" not in metadata.json()["metadata"]
    assert "hidden_reasoning" not in metadata.json()["metadata"]
    text = await _get(app, f"/api/artifacts/{descriptor.digest}/text")
    assert text.json() == {"digest": descriptor.digest, "text": data.decode()}
    download = await _get(app, f"/api/artifacts/{descriptor.digest}/download")
    assert download.headers["content-type"] == "application/octet-stream"
    assert (
        download.headers["content-disposition"]
        == f'attachment; filename="{descriptor.digest}.blob"'
    )
    assert download.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_artifact_routes_require_auth_and_canonical_digest() -> None:
    data = b"safe"
    descriptor = _descriptor(data)
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    app.dependency_overrides[require_operator] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="authentication required")
    )
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}")).status_code == 401
    app.dependency_overrides[require_operator] = lambda: object()
    assert (await _get(app, f"/api/artifacts/{descriptor.digest.upper()}")).status_code == 422
    assert (await _get(app, "/api/artifacts/../etc/passwd")).status_code in {404, 422}


@pytest.mark.asyncio
async def test_active_media_is_never_rendered_and_json_scalars_are_allowed() -> None:
    html = b"<svg>unsafe</svg>"
    descriptor = _descriptor(html, "text/html")
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(html)))
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}/text")).status_code == 422
    assert (await _get(app, f"/api/artifacts/{descriptor.digest}/download")).headers[
        "content-type"
    ] == "application/octet-stream"
    scalar = b"[1, 2, 3]"
    scalar_descriptor = _descriptor(scalar, "application/json")
    scalar_app = _app(ArtifactReadService(_Query([scalar_descriptor]), _Store(scalar)))
    assert (
        await _get(scalar_app, f"/api/artifacts/{scalar_descriptor.digest}/text")
    ).status_code == 200


@pytest.mark.asyncio
async def test_markdown_pr_body_is_returned_as_literal_text() -> None:
    data = b"# Exact PR body\n<script>untrusted</script>"
    descriptor = _descriptor(data, "text/markdown")
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    response = await _get(app, f"/api/artifacts/{descriptor.digest}/text")
    assert response.status_code == 200
    assert response.json()["text"] == data.decode()
    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{}, {"verified": "true"}, {"actor_can_bypass": 0}, {"extra": 1}])
async def test_merge_protection_representation_hashes_the_exact_recorded_source(override) -> None:
    import hashlib
    import json

    protection = {
        "strict_required_checks": True,
        "merge_queue_enabled": False,
        "actor_can_bypass": False,
        "evidence_source": "branch-protection+rulesets",
        "verified": True,
        "required_check_names": ["ci"],
    }
    protection.update(override)
    wire = json.dumps(
        protection, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    data = json.dumps(
        {
            "protection": protection,
            "pull_request": {
                "base_repository": "owner/repo",
                "number": 12,
                "head_sha": "a" * 40,
                "base_ref": "main",
                "base_sha": "b" * 40,
            },
        }
    ).encode()
    descriptor = _descriptor(data, "application/json")
    app = _app(ArtifactReadService(_Query([descriptor]), _Store(data)))
    response = await _get(app, f"/api/artifacts/{descriptor.digest}/merge-protection")
    if override:
        assert response.status_code == 422
        return
    assert response.status_code == 200, response.text
    assert response.json()["protection_digest"] == hashlib.sha256(wire).hexdigest()
    assert response.json()["protection"] == protection


@pytest.mark.asyncio
async def test_artifact_corruption_utf8_oversize_and_empty_records_fail_closed() -> None:
    data = b"trusted"
    descriptor = _descriptor(data)
    corrupted = _app(ArtifactReadService(_Query([descriptor]), _Store(b"tampered")))
    assert (
        await _get(corrupted, f"/api/artifacts/{descriptor.digest}/download")
    ).status_code == 503
    empty = _app(ArtifactReadService(_Query([]), _Store(data)))
    assert (await _get(empty, f"/api/artifacts/{descriptor.digest}")).status_code == 404
    invalid = b"\xff"
    invalid_descriptor = _descriptor(invalid)
    invalid_app = _app(ArtifactReadService(_Query([invalid_descriptor]), _Store(invalid)))
    assert (
        await _get(invalid_app, f"/api/artifacts/{invalid_descriptor.digest}/text")
    ).status_code == 422
    oversized = b"x" * 5
    oversized_descriptor = _descriptor(oversized)
    oversized_app = _app(
        ArtifactReadService(_Query([oversized_descriptor]), _Store(oversized), max_text_bytes=4)
    )
    assert (
        await _get(oversized_app, f"/api/artifacts/{oversized_descriptor.digest}/text")
    ).status_code == 422


@pytest.mark.asyncio
async def test_artifact_corruption_is_detected_by_service() -> None:
    data = b"trusted"
    descriptor = _descriptor(data)
    service = ArtifactReadService(_Query([descriptor]), _Store(b"tampered"))
    with pytest.raises(ArtifactReadError):
        await service.content(descriptor.digest)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_persisted_artifact_routes_enforce_real_session(
    session_factory, persisted_run, tmp_path
) -> None:
    from forge.api.app import create_app
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.repositories.artifacts import ArtifactRepository
    from forge.settings import Settings

    settings = Settings(data_root=tmp_path / "data")
    descriptor = await FilesystemArtifactStore(settings.artifact_root).put_bytes(
        b"actual persisted text", media_type="text/plain"
    )
    await ArtifactRepository(session_factory).record(
        descriptor, run_id=persisted_run.id, producer_type="test"
    )
    app = create_app(settings, session_factory=session_factory)
    paths = [f"/api/artifacts/{descriptor.digest}{suffix}" for suffix in ("", "/text", "/download")]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        for path in paths:
            assert (await client.get(path)).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        response = await client.post(
            "/api/auth/bootstrap",
            json={"token": token},
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        assert response.status_code == 200
        metadata, text, download = [await client.get(path) for path in paths]
        assert metadata.status_code == text.status_code == download.status_code == 200
        assert metadata.json()["lineage"][0]["run_id"] == str(persisted_run.id)
        assert text.json()["text"] == "actual persisted text"
        assert download.content == b"actual persisted text"
        assert download.headers["content-type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_text_bound_rejects_before_opening_blob() -> None:
    class UnreadableStore:
        async def open_bytes(self, digest, *, max_bytes=None):
            pytest.fail("oversize content must not be opened")

    descriptor = _descriptor(b"oversized")
    service = ArtifactReadService(_Query([descriptor]), UnreadableStore(), max_text_bytes=4)
    with pytest.raises(ArtifactReadError):
        await service.text(descriptor.digest)


@pytest.mark.asyncio
async def test_malformed_json_is_not_retryable() -> None:
    descriptor = _descriptor(b"{", "application/json")
    response = await _get(
        _app(ArtifactReadService(_Query([descriptor]), _Store(b"{"))),
        f"/api/artifacts/{descriptor.digest}/text",
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_download_bound_rejects_before_opening_blob() -> None:
    from dataclasses import replace

    from forge.application.services.artifact_reads import MAX_DOWNLOAD_BYTES

    class UnreadableStore:
        async def open_bytes(self, digest, *, max_bytes=None):
            pytest.fail("oversize download must not be opened")

    descriptor = replace(
        _descriptor(b"x"),
        byte_count=MAX_DOWNLOAD_BYTES + 1,
        original_byte_count=MAX_DOWNLOAD_BYTES + 1,
    )
    service = ArtifactReadService(_Query([descriptor]), UnreadableStore())
    with pytest.raises(ArtifactReadError):
        await service.content(descriptor.digest)
