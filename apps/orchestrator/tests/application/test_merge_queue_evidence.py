import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.domain.approval import canonical_digest as evidence_digest
from forge.domain.operation import canonical_digest
from forge.persistence.models import Approval
from forge.release.merge import StaleMergeEvidence

from apps.orchestrator.tests.domain.test_approval import merge_evidence


@pytest.mark.parametrize("queue", [True, False])
@pytest.mark.parametrize("fault", [None, "digest", "producer", "missing", "method", "boolean"])
async def test_queue_mode_requires_original_approved_observation(queue, fault):
    run_id, approval_id, record_id = uuid4(), uuid4(), uuid4()
    protection = {
        "strict_required_checks": True, "merge_queue_enabled": queue,
        "actor_can_bypass": False, "verified": True,
        "merge_queue_method": "squash" if queue else None,
    }
    approved = merge_evidence(protection_digest=canonical_digest(protection))
    if fault == "method":
        protection["merge_queue_method"] = "rebase"
    elif fault == "boolean":
        protection["merge_queue_enabled"] = int(queue)
    wire = json.dumps({"protection": protection}).encode()
    digest = hashlib.sha256(wire).hexdigest()
    event = SimpleNamespace(
        event_type="run.merge_ready", actor_class="worker",
        payload={"merge_evidence_digest": evidence_digest(approved), "observation_digest": digest,
                 "pull_request_id": str(record_id)},
    )
    descriptor = SimpleNamespace(
        digest=digest, producer_type="remote_pr_observation", producer_id=record_id,
        truncated=False, media_type="application/json", byte_count=len(wire),
    )
    if fault == "digest":
        wire += b" "
    elif fault == "producer":
        descriptor.producer_id = uuid4()
    work = SimpleNamespace(
        auth=SimpleNamespace(get_approval=AsyncMock(return_value=Approval(
            run_id=run_id, gate="merge", evidence_digest=evidence_digest(approved), run_version=3,
        ))),
        releases=SimpleNamespace(get_for_run=AsyncMock(return_value=SimpleNamespace(id=record_id))),
        events=SimpleNamespace(list_for_version=AsyncMock(return_value=[] if fault == "missing" else [event])),
        artifacts=SimpleNamespace(get_by_digest=AsyncMock(return_value=descriptor)),
    )
    store = SimpleNamespace(open_bytes=AsyncMock(return_value=wire))
    validator = MergeEvidenceValidator(store, None, None)
    with patch("forge.application.services.merge_evidence.approval_gate_origin", AsyncMock(return_value=3)):
        if fault:
            with pytest.raises(StaleMergeEvidence):
                await validator.queue_required(work, run_id, approval_id, approved)
        else:
            assert await validator.queue_required(work, run_id, approval_id, approved) is queue
