from dataclasses import replace

import pytest
from forge.release.base_update import BaseUpdateOperation
from forge.release.controller import ReleaseReconciliationRequired
from forge.release.fake_github_write import FakeGitHubWriteCrash

from apps.orchestrator.tests.release.test_merge_controller import ready
from apps.orchestrator.tests.release.test_publication_operations import intent


def setup_update():
    read, write, _, record = ready()
    repository = record.pull_request.base_repository
    read.bases[repository.casefold(), "main"] = "c" * 40
    write.pull_requests[repository, 42] = replace(record.pull_request, base_sha="c" * 40)
    operation = BaseUpdateOperation(record, read, write, "c" * 40, 1, "d" * 64, 1)
    return read, write, record, operation


async def test_update_crash_observes_new_head_without_second_request():
    _, write, record, operation = setup_update()
    calls = []

    async def update(repository, number, expected_head):
        calls.append(expected_head)
        write.pull_requests[repository, number] = replace(
            write.pull_requests[repository, number], head_sha="e" * 40
        )
        write.branch_shas[repository, record.pull_request.head_ref] = "e" * 40
        raise FakeGitHubWriteCrash()

    write.update_branch = update
    with pytest.raises(FakeGitHubWriteCrash):
        await operation.invoke(intent(operation.request))
    result = await operation.reconcile(intent(operation.request))
    assert result.payload["head_sha"] == "e" * 40
    assert result.payload["base_sha"] == "c" * 40
    assert calls == [record.pull_request.head_sha]


@pytest.mark.parametrize("drift", ["head", "base", "identity", "protection"])
async def test_update_rechecks_authority_before_any_write(drift):
    read, write, record, operation = setup_update()
    repository = record.pull_request.base_repository
    if drift == "base":
        read.bases[repository.casefold(), "main"] = "f" * 40
    elif drift == "protection":
        protection = read.merge_protections[repository.casefold(), "main"]
        read.merge_protections[repository.casefold(), "main"] = replace(
            protection, actor_can_bypass=True
        )
    else:
        changes = {"head_sha": "f" * 40} if drift == "head" else {"node_id": "other"}
        write.pull_requests[repository, 42] = replace(
            write.pull_requests[repository, 42], **changes
        )

    async def forbidden(*args):
        raise AssertionError("must not update")

    write.update_branch = forbidden
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.invoke(intent(operation.request))


async def test_acknowledged_but_unchanged_head_remains_unresolved():
    _, write, _, operation = setup_update()

    async def pending(repository, number, expected_head):
        return write.pull_requests[repository, number]

    write.update_branch = pending
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.invoke(intent(operation.request))
    with pytest.raises(ReleaseReconciliationRequired):
        await operation.reconcile(intent(operation.request))
