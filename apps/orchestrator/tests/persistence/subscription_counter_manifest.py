"""Retain the offline counter scenario's exact evidence before its DB is removed."""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

from forge.domain.operation import canonical_digest
from forge.domain.policy import RunnerMode
from forge.domain.subscription import encode_subscription_record
from forge.evaluations.credentials import assert_credential_free
from forge.persistence.models import Approval
from forge.persistence.models.subscription import SubscriptionClientLaunch
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from pydantic import BaseModel
from sqlalchemy import select


def _json(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {item.name: _json(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    raise TypeError("unsupported acceptance evidence")


async def retain_counter_manifest(
    factory,
    store,
    fixture,
    script,
    *,
    run_id,
    tmp_path,
    grade,
    scenario="A1",
    worker_check_repair_sequences=1,
    quota_status=None,
    operator_view=None,
    restarted_before_handoff=True,
):
    attempts = [request.attempt.attempt_id for request in script.requests]
    async with factory() as work:
        run = await work.runs.get(run_id)
        policy = await work.projects.get_policy(run.project_id, run.policy_version)
        calls = await work.tool_calls.list_for_run(run_id)
        events = await work.events.list_after(run_id, 0)
        usage = await work.subscription_budget.usage(run_id)
        results = (
            await work.session.scalars(
                select(SubscriptionAttemptResult).where(
                    SubscriptionAttemptResult.attempt_id.in_(attempts),
                )
            )
        ).all()
        launches = (
            await work.session.scalars(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id.in_(attempts),
                )
            )
        ).all()
        approvals = (
            await work.session.scalars(select(Approval).where(Approval.run_id == run_id))
        ).all()
        pending = {digest for call in calls for digest in call.artifact_digests}
        pending.update(approval.evidence_digest for approval in approvals)
        if run.pending_evidence_digest is not None:
            pending.add(run.pending_evidence_digest)
        result_values = [
            {
                name: getattr(row, name)
                for name in (
                    "attempt_id",
                    "result_digest",
                    "result_payload",
                    "disposition",
                    "accepted",
                    "application_digest",
                    "application_payload",
                )
            }
            for row in results
        ]
        launch_values = [
            {
                name: getattr(row, name)
                for name in (
                    "attempt_id",
                    "launch_id",
                    "worker_identity",
                    "state",
                    "terminal_payload",
                )
            }
            for row in launches
        ]
        approval_values = [
            {
                name: getattr(row, name)
                for name in (
                    "id",
                    "gate",
                    "evidence_digest",
                    "run_version",
                    "policy_version",
                )
            }
            for row in approvals
        ]
        descriptors = {}
        while pending:
            assert len(descriptors) < 256
            digest = pending.pop()
            if digest in descriptors:
                continue
            descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
            descriptors[digest] = descriptor
            pending.update(set(descriptor.parent_digests) - descriptors.keys())
        await work.rollback()
    value = _json(
        {
            "scenario": scenario,
            "proof_class": "deterministic",
            "fixture": fixture.manifest,
            "limitations": [
                "scripted provider and launch proof",
                *(
                    ["trusted-host runner"]
                    if policy.document["runner_mode"] == RunnerMode.TRUSTED_HOST.value
                    else []
                ),
                "no live model capability or allowance enforcement proof",
            ],
            "run": run,
            "policy_digest": policy.policy_digest,
            "runner": {
                "mode": policy.document["runner_mode"],
                "trusted_project": policy.document["trusted_project"],
            },
            "commands": policy.document["commands"],
            "tasks": [encode_subscription_record(request.task) for request in script.requests],
            "envelope": encode_subscription_record(script.requests[0].envelope),
            "attempts": [request.attempt for request in script.requests],
            "results": result_values,
            "launches": launch_values,
            "approvals": approval_values,
            "events": events,
            "tools": calls,
            "receipts": script.receipts,
            "artifacts": descriptors,
            "usage": usage,
            "grade": grade,
            "worker_check_repair_sequences": worker_check_repair_sequences,
            "scheduled_repair_debits": usage.consumed.repairs,
            "restarted_before_handoff_application": restarted_before_handoff,
            "quota_status": quota_status,
            "operator_view": operator_view,
        }
    )
    assert_credential_free(json.dumps(value, ensure_ascii=False))
    configured = os.environ.get("FORGE_ACCEPTANCE_OUTPUT_ROOT")
    output = Path(configured).resolve() if configured else tmp_path / "acceptance"
    if configured:
        scratch = Path(__file__).resolve().parents[4] / ".llm-output"
        assert output.is_relative_to(scratch.resolve()) and output != scratch.resolve()
    output = output / str(run_id)
    output.mkdir(parents=True, exist_ok=False)
    for digest, descriptor in descriptors.items():
        blob = await store.open_bytes(digest, max_bytes=8 * 1024 * 1024)
        assert len(blob) == descriptor.byte_count and hashlib.sha256(blob).hexdigest() == digest
        with (output / digest).open("xb") as stream:
            stream.write(blob)
    manifest = {"manifest_digest": canonical_digest(value), "evidence": value}
    with (output / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return output / "manifest.json"
