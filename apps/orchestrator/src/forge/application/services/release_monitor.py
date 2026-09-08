"""Durable, bounded PR polling and exact-evidence merge readiness."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.monitor_replay import verify_monitor_replay
from forge.application.services.monitor_resume import monitor_inputs, monitor_origin
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.application.services.validation import _fence_command
from forge.domain.approval import ApprovalGate, MergeApprovalEvidence, canonical_digest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.github import CheckSnapshot, ReviewSnapshot
from forge.domain.operation import canonical_digest as payload_digest
from forge.domain.run import RunState
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.monitor import CheckAssessment, assess_checks, required_check_results


class ReleaseMonitor:
    def __init__(
        self,
        store: ArtifactStore,
        evidence: PrEvidenceValidator,
        reads: GitHubPort,
        writes: GitHubWritePort,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._store, self._evidence = store, evidence
        self._reads, self._writes = reads, writes
        self._clock = clock or SystemClock()

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if command.status is not CommandStatus.LEASED:
            raise CommandRecoveryRequired("PR monitoring command is not leased")
        await _fence_command(command, work)
        origin = await monitor_origin(work, command)
        record_id, poll = monitor_inputs(command)
        run = await work.runs.get_for_update(command.run_id)
        events = await work.events.list_after(run.id, 0)
        settled = [
            e
            for e in events
            if e.event_type == "run.pr_observed"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if settled:
            await self._replay(command, work, settled, record_id, poll)
            await work.commit()
            return
        if (
            run.state is not RunState.MONITORING_PR
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("PR monitoring awaits control reconciliation")
        record = await work.releases.get_for_run(run.id)
        parents = [
            e
            for e in events
            if e.payload.get("monitor_command_id") == str(origin.id)
            and e.event_type
            in {
                "run.pr_published",
                "run.pr_observed",
                "run.pr_updated",
                "run.merge_approval_consumed",
                "run.merge_rejected",
                "run.review_decided",
            }
        ]
        if (
            record is None
            or record.id != record_id
            or len(parents) != 1
            or (
                parents[0].event_type != "run.review_decided"
                and parents[0].actor_id != command.actor_id
            )
            or parents[0].payload.get("pull_request_id") != str(record_id)
            or (poll == 1 and parents[0].event_type != "run.pr_published")
            or (poll > 1 and parents[0].payload.get("poll") != poll - 1)
        ):
            raise CommandRecoveryRequired("PR monitoring has no causal delivery authority")
        if parents[0].event_type == "run.review_decided":
            parent = parents[0]
            try:
                source_id = UUID(str(parent.payload.get("source_command_id")))
            except ValueError:
                raise CommandRecoveryRequired("base review monitor source differs") from None
            source = await work.commands.get(source_id)
            if (
                source.run_id != run.id
                or source.command_type != "review"
                or source.status is not CommandStatus.COMPLETED
                or source.actor_id != command.actor_id
                or source.expected_run_version + 1 != origin.expected_run_version
                or parent.run_version != origin.expected_run_version
                or parent.actor_class != "worker"
                or parent.actor_id is not None
                or parent.payload.get("target") != RunState.MONITORING_PR.value
                or parent.payload.get("queued_payload") != origin.payload
                or parent.payload.get("queued_key") != origin.idempotency_key
                or parent.payload.get("base_update_intent_id") != str(record.base_update_intent_id)
                or parent.payload.get("base_adoption_intent_id")
                != str(record.base_adoption_intent_id)
            ):
                raise CommandRecoveryRequired("base review monitor source differs")
        publication = await work.operations.get(record.publication_intent_id)
        try:
            approval_id = UUID(str(publication.request_payload["approval_id"]))
        except KeyError, ValueError, TypeError:
            raise CommandRecoveryRequired("PR publication authority is invalid") from None
        now = self._clock.now()
        deadline = await work.runs.duration_deadline(run.id)
        observation: dict[str, object] = {"pull_request_id": str(record.id)}
        assessment = CheckAssessment("intervene", "run_duration_exhausted")
        verified = None
        checks: tuple[CheckSnapshot, ...] = ()
        reviews: tuple[ReviewSnapshot, ...] = ()
        protection = None
        target_base: str | None = None
        if now < deadline:
            try:
                pull = await self._writes.get_pull_request(
                    record.pull_request.base_repository, record.pull_request.number
                )
                observation["pull_request"] = asdict(pull)
                remote_base = await self._reads.get_base(
                    record.pull_request.base_repository, record.pull_request.base_ref
                )
                if remote_base != record.pull_request.base_sha:
                    target_base = remote_base
                if pull not in (
                    record.pull_request,
                    replace(record.pull_request, base_sha=remote_base),
                ):
                    assessment = CheckAssessment("intervene", "remote_identity_drift")
                else:
                    try:
                        if target_base is None:
                            verified = await self._evidence.validate_published(
                                work, run.id, approval_id
                            )
                        else:
                            verified = await self._evidence.validate_for_base_update(
                                work, run.id, approval_id, target_base
                            )
                    except PrEvidenceValidationError as exc:
                        if isinstance(exc.__cause__, GitHubClientError):
                            raise exc.__cause__
                        assessment = CheckAssessment("intervene", exc.category)
                    if verified is not None:
                        repository = pull.base_repository
                        checks = await self._reads.get_checks(repository, pull.head_sha)
                        reviews = await self._reads.get_reviews(repository, pull.number)
                        protection = await self._reads.get_merge_protection(
                            repository, pull.base_ref
                        )
                        observation.update(
                            {
                                "checks": [asdict(item) for item in checks],
                                "reviews": [asdict(item) for item in reviews],
                                "protection": asdict(protection),
                            }
                        )
                        assessment = assess_checks(pull.head_sha, checks, reviews, protection)
                        if target_base is not None:
                            observation["target_base_sha"] = target_base
                            assessment = (
                                CheckAssessment("remediate", "base_advanced")
                                if protection.safe_for_managed_merge
                                and protection.required_check_names
                                else CheckAssessment("intervene", "unsafe_merge_protection")
                            )
            except (GitHubClientError, GitHubWriteError) as error:
                category = (
                    error.category
                    if error.category
                    in {
                        "unavailable",
                        "rate_limited",
                        "permission",
                        "forbidden",
                        "credential",
                        "not_found",
                        "invalid_response",
                        "malformed_response",
                        "untrusted_redirect",
                        "pagination_limit",
                        "response_too_large",
                    }
                    else "unverified"
                )
                observation = {"pull_request_id": str(record.id), "read_error": category}
                verified, protection, target_base = None, None, None
                checks, reviews = (), ()
                assessment = CheckAssessment(
                    "pending" if category in {"unavailable", "rate_limited"} else "intervene",
                    f"remote_read_{category}",
                )
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if current != run or await pending_current_control_stop(work, current):
            raise CommandRecoveryRequired("PR observation settlement awaits control reconciliation")
        now = self._clock.now()
        if now >= deadline:
            assessment = CheckAssessment("intervene", "run_duration_exhausted")
        digest = await self._artifact(
            work, command, record.id, "remote_pr_observation", observation
        )
        payload: dict[str, object] = {
            "source_command_id": str(command.id),
            "pull_request_id": str(record.id),
            "poll": poll,
            "observation_digest": digest,
            "reason": assessment.reason,
            "disposition": assessment.disposition,
        }
        if assessment.disposition == "pending":
            previous = parents[0].payload
            unchanged = previous.get("observation_digest") == digest
            previous_delay = previous.get("delay_seconds", 15)
            if type(previous_delay) is not int or previous_delay not in {15, 30, 60, 120}:
                raise CommandRecoveryRequired("PR polling backoff is invalid")
            delay = min(120, previous_delay * 2) if unchanged else 15
            queued = await work.commands.enqueue(
                run_id=run.id,
                command_type="monitor_pr",
                idempotency_key=f"{run.id}:monitor-pr:{poll + 1}",
                payload={"pull_request_id": str(record.id), "poll": poll + 1},
                expected_run_version=run.version,
                actor_id=command.actor_id,
                available_at=min(now + timedelta(seconds=delay), deadline),
            )
            payload.update({"monitor_command_id": str(queued.id), "delay_seconds": delay})
        elif assessment.disposition == "ready":
            assert verified is not None and protection is not None
            local = verified.evidence
            merge = MergeApprovalEvidence(
                repository=local.repository,
                pull_request_number=record.pull_request.number,
                head_sha=local.candidate_commit,
                base_ref=local.base_ref,
                base_sha=verified.remote_base_sha,
                required_checks=required_check_results(checks, protection),
                unresolved_blocking_findings=0,
                validation_digest=local.validation_digest,
                review_digest=local.review_digest,
                runner_mode=local.runner_mode,
                runner_evidence_digest=local.runner_evidence_digest,
                protection_digest=payload_digest(asdict(protection)),
                merge_method=verified.approved.policy.allowed_merge_methods[0],
                policy_version=verified.approved.policy.version,
            )
            merge_digest = await self._artifact(
                work,
                command,
                record.id,
                "merge_approval_evidence",
                merge.model_dump(mode="json"),
                parents=(
                    local.validation_digest,
                    local.review_digest,
                    local.runner_evidence_digest,
                ),
            )
            if merge_digest != canonical_digest(merge):
                raise CommandRecoveryRequired("merge evidence encoding differs")
            payload["merge_evidence_digest"] = merge_digest
            current = await work.runs.await_approval(
                run.id,
                run.version,
                ApprovalGate.MERGE,
                merge_digest,
                "run.merge_ready",
                payload,
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=now,
            )
        elif assessment.disposition == "remediate":
            assert verified is not None
            current = await work.runs.begin_remote_remediation(
                run.id,
                run.version,
                limit=verified.evidence.remote_remediation_limit,
                event_type="run.remote_remediation_requested",
                event_payload=payload,
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=now,
            )
            if current.state is RunState.REMEDIATING:
                command_payload: dict[str, object] = {
                    "observation_digest": digest,
                    "pull_request_id": str(record.id),
                    "remote_attempt": current.remote_remediation_count,
                }
                if target_base is not None:
                    command_payload["target_base_sha"] = target_base
                else:
                    command_payload["semantic_attempt"] = await work.executions.next_attempt(
                        run.id, "implement"
                    )
                queued = await work.commands.enqueue(
                    run_id=run.id,
                    command_type="update_base" if target_base is not None else "remediate_remote",
                    idempotency_key=f"{run.id}:remote-remediation:{current.remote_remediation_count}",
                    payload=command_payload,
                    expected_run_version=current.version,
                    actor_id=verified.approved.approval_actor_id,
                )
                payload["remediation_command_id"] = str(queued.id)
                payload["remediation_payload"] = dict(queued.payload)
                payload["remediation_key"] = queued.idempotency_key
                payload["remediation_actor_id"] = str(queued.actor_id)
        else:
            current = await work.runs.intervene(
                run.id,
                run.version,
                "run.release_intervention",
                payload,
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=now,
            )
        payload["target"] = current.state.value
        await work.events.append(
            RunEvent(
                run_id=run.id,
                run_version=current.version,
                event_type="run.pr_observed",
                payload=payload,
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=now,
            )
        )
        await work.releases.record_observation(
            run.id, command.id, digest, await self._store.open_bytes(digest, max_bytes=1048576)
        )
        await work.commit()

    async def _artifact(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        producer_id: UUID,
        kind: str,
        value: object,
        *,
        parents: Sequence[str] = (),
    ) -> str:
        wire = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_json_default
        ).encode("utf-8")
        descriptor = await self._store.put_bytes(
            wire, media_type="application/json", max_bytes=1048576, bounding_policy="head_tail"
        )
        if descriptor.truncated or descriptor.digest != hashlib.sha256(wire).hexdigest():
            raise CommandRecoveryRequired("remote observation exceeds evidence bounds")
        await work.artifacts.record(
            descriptor,
            run_id=command.run_id,
            producer_type=kind,
            producer_id=producer_id,
            parent_digests=tuple(sorted(set(parents))),
        )
        return descriptor.digest

    async def _replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        settled: Sequence[RunEvent],
        record_id: UUID,
        poll: int,
    ) -> None:
        await verify_monitor_replay(self._store, command, work, settled, record_id, poll)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("unsupported remote observation value")
