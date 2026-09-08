"""Authenticated immutable artifact metadata, text, and download routes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from forge.api.dependencies import require_operator
from forge.api.schemas.artifacts import (
    ArtifactLineageResponse,
    ArtifactResponse,
    ArtifactTextResponse,
    ReviewArtifactResponse,
    ReviewerDiffResponse,
)
from forge.application.services.artifact_reads import (
    ArtifactNotRepresentable,
    ArtifactReadError,
    ArtifactReadNotFound,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.public_data import public_payload
from forge.domain.agent import ReviewerInput, UntrustedSourceKind
from forge.domain.evidence import (
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)
from forge.persistence.repositories.artifacts import ArtifactNotFound


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/artifacts/{digest}/reviewer-diff", response_model=ReviewerDiffResponse)
    async def reviewer_diff(
        digest: str,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ReviewerDiffResponse:
        try:
            service = _service(request)
            descriptor, data = await service.content(digest)
            if descriptor.media_type != "application/json" or descriptor.truncated:
                raise ArtifactNotRepresentable("reviewer context is not complete JSON")
            raw = json.loads(data)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema_version", "execution_id", "context"}
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
                or json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                != data
            ):
                raise ArtifactNotRepresentable("reviewer context schema differs")
            execution_id = UUID(raw["execution_id"])
            context = ReviewerInput.model_validate(raw["context"])
            if (
                len(context.check_evidence) != 1
                or context.current_diff.source_kind is not UntrustedSourceKind.DIFF
            ):
                raise ArtifactNotRepresentable("reviewer diff binding missing")
            check = context.check_evidence[0]
            validation = decode_evidence_manifest(check.content.encode("utf-8"))
            if (
                not isinstance(validation, ValidationEvidenceManifest)
                or check.truncated
                or check.source_kind is not UntrustedSourceKind.CHECK
                or check.source_reference != str(validation.evidence_set_id)
            ):
                raise ArtifactNotRepresentable("reviewer validation binding differs")
            records = await service.metadata(digest)
            if not any(
                record.run_id == validation.run_id
                and record.producer_id == execution_id
                and record.producer_type == "reviewer_context"
                for record in records
            ):
                raise ArtifactNotRepresentable("reviewer context lineage differs")
            return ReviewerDiffResponse(
                digest=digest,
                run_id=validation.run_id,
                producer_execution_id=execution_id,
                validation_evidence_set_id=validation.evidence_set_id,
                head_sha=validation.head_sha,
                policy_version=validation.policy_version,
                diff_digest=context.current_diff.content_digest,
                text=context.current_diff.content,
                original_byte_count=context.current_diff.original_byte_count,
                truncated=context.current_diff.truncated,
            )
        except Exception as error:  # noqa: BLE001 - bounded immutable artifact failures
            raise _read_error(error) from None

    @router.get("/artifacts/{digest}/review", response_model=ReviewArtifactResponse)
    async def review_artifact(
        digest: str,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ReviewArtifactResponse:
        try:
            descriptor, data = await _service(request).content(digest, max_bytes=262_144)
            if descriptor.media_type != "application/vnd.forge.evidence-manifest+json":
                raise ArtifactNotRepresentable("artifact is not an evidence manifest")
            manifest = decode_evidence_manifest(data)
            if not isinstance(manifest, ReviewEvidenceManifest):
                raise ArtifactNotRepresentable("artifact is not a review manifest")
            return ReviewArtifactResponse.model_validate(
                {
                    "digest": digest,
                    "run_id": manifest.run_id,
                    "producer_execution_id": manifest.producer_execution_id,
                    "head_sha": manifest.head_sha,
                    "policy_version": manifest.policy_version,
                    **public_payload(
                        {
                            "decision": manifest.review.decision.value,
                            "summary": manifest.review.summary,
                            "tested_claims": list(manifest.review.tested_claims),
                            "missing_evidence": list(manifest.review.missing_evidence),
                        }
                    ),
                }
            )
        except Exception as error:  # noqa: BLE001 - bounded artifact/manifest failures
            raise _read_error(error) from None

    @router.get("/artifacts/{digest}", response_model=ArtifactResponse)
    async def get_artifact(
        digest: str,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ArtifactResponse:
        records = await _records(request, digest)
        first = records[0]
        return ArtifactResponse(
            digest=first.digest,
            media_type=first.media_type,
            byte_count=first.byte_count,
            schema_version=first.schema_version,
            created_at=first.created_at,
            metadata=_safe_metadata(first.metadata),
            truncated=first.truncated,
            original_byte_count=first.original_byte_count or first.byte_count,
            truncation_policy=first.truncation_policy,
            lineage=[
                ArtifactLineageResponse(
                    run_id=item.run_id,
                    producer_type=item.producer_type,
                    producer_id=item.producer_id,
                    created_at=item.created_at,
                    parent_digests=list(item.parent_digests),
                )
                for item in records
            ],
        )

    @router.get("/artifacts/{digest}/text", response_model=ArtifactTextResponse)
    async def get_text(
        digest: str,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ArtifactTextResponse:
        service = _service(request)
        try:
            _, text = await service.text(digest)
        except Exception as error:  # noqa: BLE001 - keep storage and parse failures bounded
            raise _read_error(error) from None
        return ArtifactTextResponse(digest=digest, text=text)

    @router.get(
        "/artifacts/{digest}/download",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
    )
    async def download(
        digest: str,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> Response:
        service = _service(request)
        try:
            _, data = await service.content(digest)
        except Exception as error:  # noqa: BLE001 - keep storage failures bounded
            raise _read_error(error) from None
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{digest}.blob"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


async def _records(request: Request, digest: str) -> Any:
    try:
        return await _service(request).metadata(digest)
    except Exception as error:  # noqa: BLE001 - keep query failures bounded
        raise _read_error(error) from None


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "artifact_read_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="artifact unavailable")
    return service


def _read_error(error: BaseException) -> HTTPException:
    if isinstance(error, (ArtifactNotFound, ArtifactReadNotFound)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    if isinstance(error, ValueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid artifact digest"
        )
    if isinstance(error, ArtifactNotRepresentable):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="artifact cannot be returned",
        )
    if isinstance(error, ArtifactReadError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="artifact unavailable"
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="artifact unavailable"
    )


_SENSITIVE_METADATA_WORDS = (
    "secret",
    "token",
    "password",
    "credential",
    "storage",
    "path",
    "hidden_reasoning",
    "chain_of_thought",
    "private_reasoning",
    "reasoning",
)


def _safe_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Keep metadata JSON typed and prevent pointers or secret references escaping."""

    def clean(item: object, depth: int = 0) -> object:
        if depth > 3:
            return "[bounded]"
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, child in list(item.items())[:64]:
                if not isinstance(key, str) or any(
                    word in key.casefold() for word in _SENSITIVE_METADATA_WORDS
                ):
                    continue
                result[key[:96]] = clean(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(child, depth + 1) for child in item[:32]]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return item[:1024]
        return "[bounded]"

    result = clean(value)
    return result if isinstance(result, dict) else {}


__all__ = ["router_for"]
