"""Transaction-bound remote identity persistence, checked against settled intents."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.release import ReleaseRecord
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.release import GitHubPullRequest
from forge.persistence.models import Approval, PullRequest, Run
from forge.persistence.models import RunEvent as RunEventRow
from forge.persistence.repositories.artifacts import ArtifactRepository
from forge.persistence.repositories.operations import PostgresOperationRepository


class ReleaseRecordConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("release identity does not match durable operation evidence")


class PostgresReleaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_observation(
        self, run_id: UUID, source_command_id: UUID, digest: str, wire: bytes
    ) -> None:
        if len(wire) > 1048576 or hashlib.sha256(wire).hexdigest() != digest:
            raise ReleaseRecordConflict()
        try:
            observation = json.loads(wire)
        except ValueError:
            raise ReleaseRecordConflict() from None
        if not isinstance(observation, dict):
            raise ReleaseRecordConflict()
        record = await self.get_for_run(run_id)
        latest = await self._session.scalar(
            select(RunEventRow)
            .where(RunEventRow.run_id == run_id, RunEventRow.event_type == "run.pr_observed")
            .order_by(RunEventRow.sequence.desc())
            .limit(1)
        )
        descriptor = await ArtifactRepository(session=self._session).get_by_digest(
            digest, run_id=run_id
        )
        if (
            record is None
            or latest is None
            or latest.payload.get("source_command_id") != str(source_command_id)
            or latest.payload.get("observation_digest") != digest
            or latest.payload.get("pull_request_id") != str(record.id)
            or observation.get("pull_request_id") != str(record.id)
            or descriptor.producer_type != "remote_pr_observation"
            or descriptor.producer_id != record.id
            or descriptor.truncated
            or descriptor.media_type != "application/json"
            or descriptor.byte_count != len(wire)
            or latest.payload.get("disposition")
            not in {"pending", "ready", "remediate", "intervene"}
        ):
            raise ReleaseRecordConflict()
        pull = observation.get("pull_request")
        head = pull.get("head_sha") if isinstance(pull, Mapping) else None
        row = await self._session.get(PullRequest, record.id)
        assert row is not None
        binding = {"observation_digest": digest, "head_sha": head}
        row.checks = binding | {"items": observation.get("checks", [])}
        row.review_state = binding | {"items": observation.get("reviews", [])}
        row.merge_state = str(latest.payload["disposition"])
        await self._session.flush()

    async def record_base_update(
        self,
        run_id: UUID,
        pull_request: GitHubPullRequest,
        update_intent_id: UUID,
        adoption_intent_id: UUID,
    ) -> ReleaseRecord:
        run = await self._session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        current = await self.get_for_run(run_id)
        if run is None or current is None:
            raise ReleaseRecordConflict()
        operations = PostgresOperationRepository(session=self._session)
        update = await operations.get(update_intent_id)
        adoption = await operations.get(adoption_intent_id)
        pull, request = pull_request, update.request_payload
        for operation, kind, prefix in (
            (update, "update_branch", "update-branch"),
            (adoption, "adopt_base", "adopt-base"),
        ):
            if (
                operation.run_id != run_id
                or operation.kind != kind
                or operation.status is not OperationStatus.SUCCEEDED
                or operation.request_schema_version != 1
                or operation.request_digest != canonical_digest(operation.request_payload)
                or operation.idempotency_key != f"{run_id}:{prefix}:{operation.request_digest}"
                or operation.outcome != asdict(pull)
                or operation.remote_resource_id != pull.node_id
            ):
                raise ReleaseRecordConflict()
        expected = {
            "pull_request_id": str(current.id),
            "node_id": pull.node_id,
            "repository": pull.base_repository,
            "pull_request_number": pull.number,
            "head_sha": request.get("head_sha"),
            "previous_base_sha": request.get("previous_base_sha"),
            "base_sha": pull.base_sha,
            "base_ref": pull.base_ref,
            "policy_version": run.policy_version,
            "observation_digest": request.get("observation_digest"),
            "remote_attempt": request.get("remote_attempt"),
        }
        if (
            request != expected
            or adoption.request_payload
            != dict(request)
            | {
                "update_intent_id": str(update.id),
                "head_sha": pull.head_sha,
                "previous_head_sha": request.get("head_sha"),
            }
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
            or replace(current.pull_request, head_sha=pull.head_sha, base_sha=pull.base_sha) != pull
            or any(
                not isinstance(value, str)
                or len(value) != length
                or any(c not in "0123456789abcdef" for c in value)
                for value, length in (
                    (pull.head_sha, 40),
                    (pull.base_sha, 40),
                    (request.get("head_sha"), 40),
                    (request.get("previous_base_sha"), 40),
                    (request.get("observation_digest"), 64),
                )
            )
            or type(request.get("remote_attempt")) is not int
            or int(str(request.get("remote_attempt"))) < 1
            or request.get("head_sha") == pull.head_sha
            or request.get("previous_base_sha") == pull.base_sha
        ):
            raise ReleaseRecordConflict()
        if current.base_update_intent_id == update_intent_id:
            if (
                current.base_adoption_intent_id != adoption_intent_id
                or current.pull_request != pull
            ):
                raise ReleaseRecordConflict()
            return current
        if current.pull_request.head_sha != request.get(
            "head_sha"
        ) or current.pull_request.base_sha != request.get("previous_base_sha"):
            raise ReleaseRecordConflict()
        row = await self._session.get(PullRequest, current.id)
        assert row is not None
        row.head_sha, row.base_sha = pull.head_sha, pull.base_sha
        row.checks, row.review_state, row.merge_state = {}, {}, None
        row.base_update_intent_id, row.base_adoption_intent_id = (
            update_intent_id,
            adoption_intent_id,
        )
        row.reviewed_push_intent_id = row.candidate_evidence_digest = None
        await self._session.flush()
        return _record(row)

    async def record_merge(
        self, run_id: UUID, pull_request: GitHubPullRequest, merge_intent_id: UUID
    ) -> ReleaseRecord:
        current = await self.get_for_run(run_id)
        if current is None:
            raise ReleaseRecordConflict()
        intent = await PostgresOperationRepository(session=self._session).get(merge_intent_id)
        request = intent.request_payload
        try:
            approval = await self._session.get(Approval, UUID(str(request["approval_id"])))
        except KeyError, ValueError:
            raise ReleaseRecordConflict() from None
        pull = pull_request
        expected = {
            "repository": pull.base_repository,
            "pull_request_number": pull.number,
            "node_id": pull.node_id,
            "head_sha": pull.head_sha,
            "base_ref": f"refs/heads/{pull.base_ref}",
        }
        if (
            approval is None
            or approval.run_id != run_id
            or approval.gate != "merge"
            or approval.evidence_digest != request.get("approval_digest")
            or approval.policy_version != request.get("policy_version")
            or intent.run_id != run_id
            or intent.kind != "merge_pr"
            or intent.status is not OperationStatus.SUCCEEDED
            or set(request)
            != set(expected)
            | {"approval_id", "approval_digest", "policy_version", "base_sha", "merge_method"}
            or intent.request_digest != canonical_digest(request)
            or intent.idempotency_key != f"{run_id}:merge_pr:{intent.request_digest}"
            or any(request.get(key) != value for key, value in expected.items())
            or intent.outcome != asdict(pull)
            or intent.remote_resource_id != pull.node_id
            or not pull.merged
            or pull.state != "closed"
            or pull.merge_sha is None
            or len(pull.merge_sha) != 40
            or any(c not in "0123456789abcdef" for c in pull.merge_sha)
            or request.get("merge_method") not in {"squash", "merge", "rebase"}
            or replace(
                current.pull_request,
                state="closed",
                merged=True,
                merge_sha=pull.merge_sha,
                base_sha=pull.base_sha,
            )
            != pull
        ):
            raise ReleaseRecordConflict()
        if current.merge_intent_id is not None:
            if current.merge_intent_id != merge_intent_id or current.pull_request != pull:
                raise ReleaseRecordConflict()
            return current
        if current.pull_request.base_sha != request.get("base_sha"):
            raise ReleaseRecordConflict()
        row = await self._session.get(PullRequest, current.id)
        assert row is not None
        row.state, row.merge_sha, row.base_sha = "MERGED", pull.merge_sha, pull.base_sha
        row.merge_method = str(request["merge_method"])
        row.merge_intent_id = merge_intent_id
        row.merged_at = intent.completed_at
        await self._session.flush()
        return _record(row)

    async def record_reviewed_push(
        self, run_id: UUID, pull_request: GitHubPullRequest, push_intent_id: UUID
    ) -> ReleaseRecord:
        current = await self.get_for_run(run_id)
        if current is None:
            raise ReleaseRecordConflict()
        operations = PostgresOperationRepository(session=self._session)
        operation = await operations.get(push_intent_id)
        publication = await operations.get(current.publication_intent_id)
        request = operation.request_payload
        digest = request.get("candidate_evidence_digest")
        pull = pull_request
        expected = {
            "approval_id": publication.request_payload.get("approval_id"),
            "approval_digest": publication.request_payload.get("approval_digest"),
            "policy_version": publication.request_payload.get("policy_version"),
            "repository": pull.base_repository,
            "branch": pull.head_ref,
            "head_sha": pull.head_sha,
            "base_ref": f"refs/heads/{pull.base_ref}",
            "base_sha": pull.base_sha,
            "pull_request_id": str(current.id),
            "pull_request_number": pull.number,
            "node_id": pull.node_id,
        }
        if (
            publication.run_id != run_id
            or publication.kind != "create_pr"
            or publication.status is not OperationStatus.SUCCEEDED
            or operation.run_id != run_id
            or operation.kind != "push_branch"
            or operation.status is not OperationStatus.SUCCEEDED
            or operation.request_digest != canonical_digest(request)
            or operation.idempotency_key != f"{run_id}:push-reviewed:{operation.request_digest}"
            or any(request.get(key) != value for key, value in expected.items())
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or operation.outcome != asdict(pull)
            or operation.remote_resource_id != pull.node_id
            or replace(current.pull_request, head_sha=pull.head_sha) != pull
            or request.get("base_update_intent_id")
            != (str(current.base_update_intent_id) if current.base_update_intent_id else None)
            or request.get("base_adoption_intent_id")
            != (str(current.base_adoption_intent_id) if current.base_adoption_intent_id else None)
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
        ):
            raise ReleaseRecordConflict()
        if current.reviewed_push_intent_id == push_intent_id:
            if current.pull_request != pull or current.candidate_evidence_digest != digest:
                raise ReleaseRecordConflict()
            return current
        if request.get("previous_head_sha") != current.pull_request.head_sha:
            raise ReleaseRecordConflict()
        row = await self._session.get(PullRequest, current.id)
        assert row is not None
        row.head_sha = pull.head_sha
        row.checks, row.review_state, row.merge_state = {}, {}, None
        row.reviewed_push_intent_id = push_intent_id
        row.candidate_evidence_digest = digest
        await self._session.flush()
        return _record(row)

    async def get_for_run(self, run_id: UUID) -> ReleaseRecord | None:
        rows = list(
            (
                await self._session.scalars(
                    select(PullRequest).where(PullRequest.run_id == run_id).with_for_update()
                )
            ).all()
        )
        if len(rows) > 1:
            raise ReleaseRecordConflict()
        return _record(rows[0]) if rows else None

    async def record_publication(
        self,
        run_id: UUID,
        pull_request: GitHubPullRequest,
        push_intent_id: UUID,
        publication_intent_id: UUID,
    ) -> ReleaseRecord:
        # Serialize first insertion too; locking a nonexistent PR cannot do so.
        run = await self._session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None:
            raise ReleaseRecordConflict()
        operations = PostgresOperationRepository(session=self._session)
        push = await operations.get(push_intent_id)
        publication = await operations.get(publication_intent_id)
        pull = pull_request
        expected = {
            "repository": pull.base_repository,
            "branch": pull.head_ref,
            "head_sha": pull.head_sha,
            "base_ref": f"refs/heads/{pull.base_ref}",
            "base_sha": pull.base_sha,
        }
        for operation, kind in ((push, "push_branch"), (publication, "create_pr")):
            if (
                operation.run_id != run_id
                or operation.kind != kind
                or operation.status is not OperationStatus.SUCCEEDED
                or operation.request_digest != canonical_digest(operation.request_payload)
                or any(
                    operation.request_payload.get(key) != value for key, value in expected.items()
                )
                or not operation.request_payload.get("approval_id")
                or not operation.request_payload.get("approval_digest")
                or operation.request_payload.get("policy_version") != run.policy_version
            ):
                raise ReleaseRecordConflict()
        if (
            push.request_payload != publication.request_payload
            or publication.outcome != asdict(pull)
            or publication.remote_resource_id != pull.node_id
            or push.outcome
            != {
                "repository": pull.base_repository,
                "branch": pull.head_ref,
                "head_sha": pull.head_sha,
            }
            or pull.head_repository != pull.base_repository
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
        ):
            raise ReleaseRecordConflict()
        existing = await self.get_for_run(run_id)
        if existing is not None:
            if (
                existing.pull_request != pull
                or existing.push_intent_id != push_intent_id
                or existing.publication_intent_id != publication_intent_id
            ):
                raise ReleaseRecordConflict()
            return existing
        row = PullRequest(
            run_id=run_id,
            repository=pull.base_repository,
            branch=pull.head_ref,
            base_ref=pull.base_ref,
            pull_request_number=pull.number,
            head_sha=pull.head_sha,
            base_sha=pull.base_sha,
            checks={},
            review_state={},
            state="OPEN",
            node_id=pull.node_id,
            url=pull.url,
            head_repository=pull.head_repository,
            push_intent_id=push_intent_id,
            publication_intent_id=publication_intent_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _record(row)


def _record(row: PullRequest) -> ReleaseRecord:
    if (
        row.node_id is None
        or row.url is None
        or row.head_repository is None
        or row.push_intent_id is None
        or row.publication_intent_id is None
    ):
        # Legacy projections carry no managed-release authority.
        raise ReleaseRecordConflict()
    return ReleaseRecord(
        row.id,
        row.run_id,
        GitHubPullRequest(
            row.pull_request_number,
            row.node_id,
            row.url,
            row.head_repository,
            row.branch,
            row.head_sha,
            row.repository,
            row.base_ref,
            row.base_sha,
            "closed" if row.state == "MERGED" else row.state.lower(),
            row.state == "MERGED",
            row.merge_sha,
        ),
        row.push_intent_id,
        row.publication_intent_id,
        row.merge_intent_id,
        row.reviewed_push_intent_id,
        row.candidate_evidence_digest,
        row.base_update_intent_id,
        row.base_adoption_intent_id,
    )
