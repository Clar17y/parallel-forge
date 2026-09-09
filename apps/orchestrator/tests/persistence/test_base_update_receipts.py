from dataclasses import asdict, replace

import pytest
from forge.domain.operation import OperationOutcome, canonical_digest
from forge.persistence.models import PullRequest, Run
from forge.persistence.repositories.release import ReleaseRecordConflict
from sqlalchemy.exc import IntegrityError

from apps.orchestrator.tests.persistence.test_release import prepared


@pytest.mark.integration
@pytest.mark.parametrize(
    "defect", [None, "pending_remote", "pending_local", "wrong_parent", "head", "policy"]
)
async def test_base_update_settles_only_matching_remote_and_local_receipts(
    uow, persisted_run, defect
):
    async with uow as work:
        pull, ids = await prepared(work, persisted_run.id)
        original = await work.releases.record_publication(persisted_run.id, pull, *ids)
        run_row = await work.session.get(Run, persisted_run.id)
        run_row.base_sha, run_row.base_ref = pull.base_sha, "refs/heads/main"
        await work.session.flush()
        base_before = (await work.runs.get(persisted_run.id)).base_sha
        updated = replace(pull, head_sha="e" * 40, base_sha="c" * 40)
        remote = {
            "pull_request_id": str(original.id),
            "node_id": pull.node_id,
            "repository": pull.base_repository,
            "pull_request_number": pull.number,
            "head_sha": pull.head_sha,
            "previous_base_sha": pull.base_sha,
            "base_sha": updated.base_sha,
            "base_ref": pull.base_ref,
            "policy_version": persisted_run.policy_version,
            "observation_digest": "d" * 64,
            "remote_attempt": 1,
        }
        if defect == "head":
            remote["head_sha"] = "f" * 40
        if defect == "policy":
            remote["policy_version"] = persisted_run.policy_version + 1
        receipts = []
        for kind, prefix in (("update_branch", "update-branch"), ("adopt_base", "adopt-base")):
            request = (
                remote
                if kind == "update_branch"
                else remote
                | {
                    "update_intent_id": str(receipts[0]),
                    "head_sha": updated.head_sha,
                    "previous_head_sha": pull.head_sha,
                }
            )
            if kind == "adopt_base" and defect == "wrong_parent":
                request["update_intent_id"] = str(ids[0])
            digest = canonical_digest(request)
            intent = await work.operations.begin(
                run_id=persisted_run.id,
                operation_type=kind,
                idempotency_key=f"{persisted_run.id}:{prefix}:{digest}",
                request_digest=digest,
                request_payload=request,
                execution_owner="updater",
                execution_lease_seconds=30,
            )
            receipts.append(intent.id)
            if not (
                (kind == "update_branch" and defect == "pending_remote")
                or (kind == "adopt_base" and defect == "pending_local")
            ):
                await work.operations.complete(
                    intent.id,
                    OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(updated)),
                    owner_id="updater",
                )
        if defect:
            with pytest.raises(ReleaseRecordConflict):
                await work.releases.record_base_update(persisted_run.id, updated, *receipts)
            assert await work.releases.get_for_run(persisted_run.id) == original
        else:
            recorded = await work.releases.record_base_update(persisted_run.id, updated, *receipts)
            assert recorded.pull_request == updated
            assert recorded.base_update_intent_id == receipts[0]
            assert recorded.base_adoption_intent_id == receipts[1]
            assert recorded.publication_intent_id == original.publication_intent_id
            with pytest.raises(IntegrityError):
                async with work.session.begin_nested():
                    row = await work.session.get(PullRequest, recorded.id)
                    row.base_adoption_intent_id = None
                    await work.session.flush()
            assert (
                await work.releases.record_base_update(persisted_run.id, updated, *receipts)
                == recorded
            )
            assert (await work.runs.get(persisted_run.id)).base_sha == base_before
