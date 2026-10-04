import asyncio
import gc
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor, _process_token
from forge.application.ports.epic_brainstorm import BrainstormGatewayResult, BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import BrainstormConflict, BrainstormProposal
from forge.domain.subscription import (
    AttemptTelemetry,
    RouteBinding,
    RouteSpec,
    TaskBudget,
    UnknownTelemetryPolicy,
)
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormBudgetLedger,
    BrainstormJobRow,
    BrainstormQuotaAdmission,
)
from forge.persistence.models.project import Project
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaObservation,
    SubscriptionQuotaPool,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.worker.epic_brainstorm import (
    DurableBrainstormProcessLifecycle,
    EpicBrainstormWorker,
    worker_host_scope,
)
from sqlalchemy import delete


class BriefFixture:
    def __init__(self, epic_id, project_id):
        self.current = BriefInput(
            epic_id=epic_id,
            project_id=project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=None,
            accepted_digest=None,
        )
        self.saved = []

    async def input(self, epic_id, *, for_update=False):
        assert epic_id == self.current.epic_id
        return self.current

    async def save_proposal_revision(self, epic_id, *, expected_version, source_job_id, proposal):
        assert self.current.epic_version == expected_version
        revision_id = uuid4()
        self.saved.append((source_job_id, revision_id, proposal.digest))
        self.current = replace(self.current, epic_version=expected_version + 1)
        return revision_id


class FakeGateway:
    def __init__(self, *, finish=True, blocked=None):
        self.finish, self.blocked = finish, blocked
        self.result = None

    async def execute(self, job, turns, reader, *, cancelled, lifecycle):
        if self.blocked:
            self.blocked.set()
        owner = self

        class RecordingLifecycle:
            async def launch_intent(self, launch_id):
                await lifecycle.launch_intent(launch_id)

            async def started(self, receipt):
                await lifecycle.started(receipt)

            async def finished(self, receipt, result):
                owner.result = result
                if owner.finish:
                    await lifecycle.finished(receipt, result)

        spec = ClientLaunchSpec(
            argv=(
                sys.executable,
                "-c",
                "import sys,json; sys.stdin.readline(); print(json.dumps({'ok':True}))",
            ),
            cwd=".",
            environment={},
            duration_seconds=5,
        )
        self.result = await ClientProcessSupervisor().run(
            spec, {"prompt": turns[-1].text}, lifecycle=RecordingLifecycle()
        )
        proposal = BrainstormProposal(
            turn_id=uuid4(), problem="Proposed problem", requirements=("One requirement",)
        )
        self.proposal = proposal
        return BrainstormGatewayResult(
            proposal=proposal,
            telemetry=AttemptTelemetry(input_tokens=4, output_tokens=5, duration_ms=10),
        )


async def prepared(factory, *, attempts=2, budget=None, repository="example/repo", provider="fake"):
    project_id, epic_id = uuid4(), uuid4()
    async with factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository=repository,
                default_branch="main",
            )
        )
    brief = BriefFixture(epic_id, project_id)
    route = RouteSpec(provider=provider, client="fake", model="fixture")
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = EpicBrainstormService(
        factory,
        lambda _: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=budget or TaskBudget(max_provider_attempts=attempts),
    )
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Investigate"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="submit",
    )
    return service, epic_id, project_id, actor, receipt


@pytest.mark.asyncio
async def test_restart_adoption_and_stale_draft(brainstorm_session_factory):
    project_id, epic_id = uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
    brief = BriefFixture(epic_id, project_id)
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())

    def service():
        return EpicBrainstormService(
            brainstorm_session_factory,
            lambda _session: brief,
            route=RouteBinding(requested=route, effective=route),
            budget=TaskBudget(max_provider_attempts=2),
        )

    conversation_id, version = await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Please brainstorm"
    )
    turns = await service().turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
    )
    receipt = await service().submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="submit",
    )
    worker = lambda gateway: EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-A",
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: object(),
    )
    assert await worker(FakeGateway()).run_once() == receipt.job_id
    outcome = await service().observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "proposed" and outcome.process_settled and outcome.usage_known
    assert outcome.proposal is not None and outcome.proposal_digest == outcome.proposal.digest
    adopted = await service().adopt(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        proposal_digest=outcome.proposal_digest,
        expected_job_version=outcome.job_version,
        expected_epic_version=1,
        actor=actor,
        key="adopt",
    )
    assert brief.saved == [(receipt.job_id, adopted, outcome.proposal_digest)]
    assert (
        await service().adopt(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest,
            expected_job_version=outcome.job_version,
            expected_epic_version=1,
            actor=actor,
            key="adopt",
        )
        == adopted
    )
    second_id, second_version = await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create-second", text="Follow up"
    )
    second_turn = (
        await service().turns(epic_id=epic_id, project_id=project_id, conversation_id=second_id)
    )[0]
    second = await service().submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=second_id,
        prompt_turn_id=second_turn.turn_id,
        expected_epic_version=2,
        expected_conversation_version=second_version,
        actor=actor,
        key="submit-second",
    )
    assert await worker(FakeGateway()).run_once() == second.job_id
    second_outcome = await service().observe(
        epic_id=epic_id, project_id=project_id, job_id=second.job_id
    )
    # Unaccepted human draft edits change epic version, even with unchanged accepted pointer.
    brief.current = replace(brief.current, epic_version=3, draft_digest="b" * 64)
    with pytest.raises(BrainstormConflict, match="brief changed"):
        await service().adopt(
            epic_id=epic_id,
            project_id=project_id,
            job_id=second.job_id,
            proposal_digest=second_outcome.proposal_digest,
            expected_job_version=second_outcome.job_version,
            expected_epic_version=2,
            actor=actor,
            key="adopt-stale",
        )
    assert len(brief.saved) == 1
    assert (
        await service().observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    ).state == "proposed"


@pytest.mark.asyncio
async def test_sibling_proposals_remain_selectable_until_human_or_brief_changes(
    brainstorm_session_factory,
):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, attempts=2
    )
    async with brainstorm_session_factory() as session:
        first_row = await session.get(BrainstormJobRow, first.job_id)
        assert first_row is not None
        conversation_id = first_row.conversation_id
    prompt = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=prompt.turn_id,
        expected_epic_version=1,
        expected_conversation_version=2,
        actor=actor,
        key="sibling-submit",
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="sibling-worker",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert {await worker.run_once(), await worker.run_once()} == {first.job_id, second.job_id}
    first_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=first.job_id
    )
    second_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=second.job_id
    )
    assert first_outcome.state == second_outcome.state == "proposed"
    assert (
        len(
            await service.turns(
                epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
            )
        )
        == 3
    )
    assert await service.adopt(
        epic_id=epic_id,
        project_id=project_id,
        job_id=second.job_id,
        proposal_digest=second_outcome.proposal_digest,
        expected_job_version=second_outcome.job_version,
        expected_epic_version=1,
        actor=actor,
        key="sibling-adopt",
    )
    with pytest.raises(BrainstormConflict, match="brief changed"):
        await service.adopt(
            epic_id=epic_id,
            project_id=project_id,
            job_id=first.job_id,
            proposal_digest=first_outcome.proposal_digest,
            expected_job_version=first_outcome.job_version,
            expected_epic_version=1,
            actor=actor,
            key="sibling-stale",
        )


@pytest.mark.asyncio
async def test_same_key_concurrent_adoption_replays_after_revision_write(
    brainstorm_session_factory,
):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="adopt-worker",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    brief = service.briefs(None)
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = brief.save_proposal_revision

    async def delayed_save(epic_id, *, expected_version, source_job_id, proposal):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        return await original_save(
            epic_id,
            expected_version=expected_version,
            source_job_id=source_job_id,
            proposal=proposal,
        )

    brief.save_proposal_revision = delayed_save
    request = {
        "epic_id": epic_id,
        "project_id": project_id,
        "job_id": receipt.job_id,
        "proposal_digest": outcome.proposal_digest,
        "expected_job_version": outcome.job_version,
        "expected_epic_version": 1,
        "actor": actor,
        "key": "concurrent-adopt",
    }
    first = asyncio.create_task(service.adopt(**request))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(service.adopt(**request))
    await asyncio.sleep(0.1)
    release.set()
    first_revision, second_revision = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=5
    )
    assert first_revision == second_revision
    assert len(brief.saved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_operator_followup_makes_proposal_stale(brainstorm_session_factory, pending):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="followup-worker",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None
        conversation_id = row.conversation_id
    await service.append(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        expected_version=3,
        actor=actor,
        key=f"followup-{pending}",
        text="Human changed direction",
        pending=pending,
    )
    with pytest.raises(BrainstormConflict, match="conversation changed"):
        await service.adopt(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            proposal_digest=outcome.proposal_digest,
            expected_job_version=outcome.job_version,
            expected_epic_version=1,
            actor=actor,
            key=f"adopt-followup-{pending}",
        )


@pytest.mark.asyncio
async def test_unsettled_launch_cannot_be_retried(brainstorm_session_factory):
    project_id, epic_id = uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
    brief = BriefFixture(epic_id, project_id)
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Investigate"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="submit",
    )
    gateway = FakeGateway(finish=False)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-A",
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    assert (
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    ).state == "reconciling"
    assert await worker.run_once() == receipt.job_id
    async with brainstorm_session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        attempt = await session.get(BrainstormAttemptRow, job.current_attempt_id)
        assert attempt is not None and attempt.launch_intent and attempt.process_settled
        attempt_id, fence = attempt.id, attempt.fence
    await DurableBrainstormProcessLifecycle(
        brainstorm_session_factory, attempt_id, fence, "worker-A"
    ).finished(gateway.result.receipt, gateway.result)
    assert await worker.run_once() is None
    recovered = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert (
        recovered.state == "failed"
        and recovered.failure == "lost_result"
        and recovered.usage_known is False
    )


@pytest.mark.asyncio
async def test_cancel_before_and_during_invocation_discards_late_proposal(
    brainstorm_session_factory,
):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    stopped = await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=receipt.job_version,
        actor=actor,
        key="cancel-before",
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() is None
    assert stopped.state == "cancelled"
    assert (
        await service.cancel(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            expected_job_version=receipt.job_version,
            actor=actor,
            key="cancel-before",
        )
        == stopped
    )

    # A separate job is already running when cancellation commits.
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create-2", text="Second idea"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="submit-2",
    )
    started, released = asyncio.Event(), asyncio.Event()

    class SlowGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await FakeGateway().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            started.set()
            await released.wait()
            assert await cancelled()
            return result

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-b",
        gateway_factory=lambda _: SlowGateway(),
        reader_factory=lambda _: object(),
    )
    operation = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)
    current = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=second.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="cancel-during",
    )
    released.set()
    assert await operation == second.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert outcome.state == "cancelled" and outcome.proposal is None and outcome.process_settled


@pytest.mark.asyncio
async def test_concurrent_admission_and_shared_quota_wait(brainstorm_session_factory):
    service, epic_id, project_id, _actor, receipt = await prepared(brainstorm_session_factory)
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    key = QuotaPolicy().key_for(route)
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            SubscriptionQuotaPool(
                provider=key.provider,
                account=key.account,
                pool=key.pool,
                revision=1,
                blocked=True,
                observed_at=datetime.now(UTC),
                reason="usage_exhausted",
                reset_at=datetime.now(UTC) + timedelta(hours=1),
                next_eligible_at=datetime.now(UTC) + timedelta(hours=1),
                retry_basis="known_reset",
            )
        )
    worker = lambda owner: EpicBrainstormWorker(
        brainstorm_session_factory,
        owner=owner,
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker("one").run_once() is None
    waiting = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert waiting.state == "quota_wait" and waiting.job_version > receipt.job_version
    assert await worker("same").run_once() is None
    assert (
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    ).job_version == waiting.job_version
    async with brainstorm_session_factory() as session, session.begin():
        pool = await session.get(
            SubscriptionQuotaPool, (key.provider, key.account, key.pool), with_for_update=True
        )
        assert pool is not None
        pool.blocked, pool.next_eligible_at, pool.reset_at = False, None, None
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        job.next_eligible_at = None
    outcomes = await asyncio.gather(worker("one").run_once(), worker("two").run_once())
    assert outcomes.count(receipt.job_id) == 1 and outcomes.count(None) == 1


@pytest.mark.asyncio
async def test_second_job_cannot_reset_epic_attempt_ceiling(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, attempts=1
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    conversation_id, version = await service.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key="second-conversation",
        text="Another idea",
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="second-job",
    )
    assert await worker.run_once() is None
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert outcome.state == "failed" and outcome.failure == "budget_exhausted"
    assert outcome.cumulative_usage.input_tokens == 4
    assert outcome.cumulative_usage.output_tokens == 5


@pytest.mark.asyncio
async def test_unknown_cost_holds_epic_reservation_across_jobs(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_provider_attempts=3, max_cost_minor=9)
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    first_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=first.job_id
    )
    assert first_outcome.held_reservations.estimated_api_cost_minor == 9
    conversation_id, version = await service.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key="cost-conversation",
        text="Another cost",
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="cost-job",
    )
    assert await worker.run_once() is None
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert outcome.state == "failed" and outcome.failure == "budget_exhausted"


@pytest.mark.asyncio
async def test_active_reservation_waits_then_wakes_other_job(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(brainstorm_session_factory)
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="capacity-conversation", text="Two"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="capacity-job",
    )
    release = asyncio.Event()
    entered = asyncio.Event()

    class WaitingGateway(FakeGateway):
        async def execute(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().execute(*args, **kwargs)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="capacity-a",
        gateway_factory=lambda _: WaitingGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    other = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="capacity-b",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await other.run_once() is None
    waiting = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert waiting.state == "capacity_wait" and waiting.failure is None
    assert waiting.reservation is None
    third_conversation, third_version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="capacity-third", text="Three"
    )
    third_turn = (
        await service.turns(
            epic_id=epic_id, project_id=project_id, conversation_id=third_conversation
        )
    )[0]
    third = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=third_conversation,
        prompt_turn_id=third_turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=third_version,
        actor=actor,
        key="capacity-third-job",
    )
    assert await other.run_once() is None
    third_wait = await service.observe(epic_id=epic_id, project_id=project_id, job_id=third.job_id)
    assert third_wait.state == "capacity_wait"
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=third.job_id,
        expected_job_version=third_wait.job_version,
        actor=actor,
        key="cancel-capacity",
    )
    _, _, _, _, independent = await prepared(
        brainstorm_session_factory, repository="example/independent"
    )
    assert await other.run_once() == independent.job_id
    release.set()
    assert await task == first.job_id
    assert await other.run_once() == second.job_id
    assert (
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=third.job_id)
    ).state == "cancelled"


@pytest.mark.asyncio
async def test_ceiling_conflict_isolated_from_other_queued_epic(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(brainstorm_session_factory)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="conflict-worker",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="conflict-conversation", text="Two"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    incompatible = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="conflict-job",
    )
    async with brainstorm_session_factory() as session, session.begin():
        ledger = await session.get(BrainstormBudgetLedger, epic_id, with_for_update=True)
        assert ledger is not None
        ledger.ceiling = {**ledger.ceiling, "max_tool_calls": 1}
    _, _, _, _, other = await prepared(brainstorm_session_factory, repository="example/other")
    assert await worker.run_once() is None
    assert await worker.run_once() == other.job_id
    outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=incompatible.job_id
    )
    assert outcome.state == "failed" and outcome.failure == "input_conflict"
    assert outcome.reservation is None
    assert (
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    ).cumulative_usage.input_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_cancel", [False, True])
async def test_restart_reconciles_only_confirmed_gone_physical_peer(
    brainstorm_session_factory, operator_cancel
):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        token = _process_token(process.pid)
        assert token
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            claimed = await repository.claim("dead-worker", 5)
            assert claimed is not None
            row, attempt = claimed
            attempt.launch_intent = True
            attempt.launch_id = str(uuid4())
            attempt.process_started = True
            attempt.process_pid = process.pid
            attempt.process_identity = token
            attempt.origin_host = worker_host_scope()
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            admission = await session.get(BrainstormQuotaAdmission, attempt.id)
            assert admission is not None
            admission.probe = True
            pool = await repository.quota_pool(repository.decode_snapshot(row))
            pool.blocked = True
            pool.probe_attempt_id = attempt.id
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="new-worker",
            gateway_factory=lambda _: FakeGateway(),
            reader_factory=lambda _: object(),
        )
        assert await worker.reconcile_settled() is None
        live = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
        assert live.state == "reconciling" and not live.process_settled
        if operator_cancel:
            await service.cancel(
                epic_id=epic_id,
                project_id=project_id,
                job_id=receipt.job_id,
                expected_job_version=live.job_version,
                actor=actor,
                key="cancel-reconciling",
            )
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.process_identity = "reused-identity"
        assert await worker.reconcile_settled() is None
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.process_identity = token
            job = await session.get(BrainstormJobRow, receipt.job_id)
            job.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.process_identity = None
            job = await session.get(BrainstormJobRow, receipt.job_id)
            job.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() is None
        missing = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert missing.state == "reconciling" and not missing.process_settled
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.process_identity = token
            attempt.origin_host = None
            job = await session.get(BrainstormJobRow, receipt.job_id)
            job.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() is None
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.origin_host = "foreign-host"
            job = await session.get(BrainstormJobRow, receipt.job_id)
            job.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() is None
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt is not None
            attempt.origin_host = worker_host_scope()
            job = await session.get(BrainstormJobRow, receipt.job_id)
            job.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() == receipt.job_id
        settled = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert settled.state == ("cancelled" if operator_cancel else "failed")
        assert settled.failure == ("cancelled" if operator_cancel else "lost_result")
        assert settled.process_settled and settled.usage_known is False
        assert settled.usage.duration_ms is None
        assert settled.held_reservations.duration_ms > 0
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            pool = await repository.quota_pool(
                repository.decode_snapshot(
                    await repository.job(epic_id, project_id, receipt.job_id)
                )
            )
            assert pool.probe_attempt_id is None
            pool.blocked = False
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_oldest", [False, True])
async def test_live_or_uncertain_oldest_orphan_does_not_starve_later_recovery(
    brainstorm_session_factory, uncertain_oldest
):
    first_service, first_epic, first_project, _, first = await prepared(
        brainstorm_session_factory, repository="example/recovery-first"
    )
    second_service, second_epic, second_project, _, second = await prepared(
        brainstorm_session_factory, repository="example/recovery-second"
    )
    live = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    gone = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    try:
        live_token, gone_token = _process_token(live.pid), _process_token(gone.pid)
        assert live_token and gone_token
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            first_claim = await repository.claim("dead-worker", 5)
            second_claim = await repository.claim("dead-worker", 5)
            assert first_claim is not None and second_claim is not None
            assert first_claim[0].id == first.job_id and second_claim[0].id == second.job_id
            for (_, attempt), process, token in (
                (first_claim, live, live_token + "-reused" if uncertain_oldest else live_token),
                (second_claim, gone, gone_token),
            ):
                attempt.launch_intent = True
                attempt.launch_id = str(uuid4())
                attempt.process_started = True
                attempt.process_pid = process.pid
                attempt.process_identity = token
                attempt.origin_host = worker_host_scope()
                attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        gone.terminate()
        await asyncio.wait_for(gone.wait(), 5)
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="recover-other",
            gateway_factory=lambda _: FakeGateway(),
            reader_factory=lambda _: object(),
        )
        assert await worker.reconcile_settled() == second.job_id
        first_outcome = await first_service.observe(
            epic_id=first_epic, project_id=first_project, job_id=first.job_id
        )
        second_outcome = await second_service.observe(
            epic_id=second_epic, project_id=second_project, job_id=second.job_id
        )
        assert first_outcome.state == "reconciling" and not first_outcome.process_settled
        assert first_outcome.held_reservations.duration_ms > 0
        assert second_outcome.state == "failed" and second_outcome.process_settled
        _, _, _, _, independent = await prepared(
            brainstorm_session_factory, repository="example/recovery-independent"
        )
        assert await worker.run_once() == independent.job_id
    finally:
        for process in (live, gone):
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)


@pytest.mark.asyncio
async def test_late_apply_cannot_replace_terminal_measurement_or_duplicate_turn(
    brainstorm_session_factory,
):
    service, epic_id, project_id, _actor, receipt = await prepared(brainstorm_session_factory)
    gateway = FakeGateway()
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="terminal-worker",
        gateway_factory=lambda _: gateway,
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    before = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert before.proposal is not None
    assert before.proposal.turn_id != gateway.proposal.turn_id
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None and row.current_attempt_id is not None
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        assert attempt is not None
        snapshot = PostgresBrainstormRepository.decode_snapshot(row)
        attempt_id, fence = attempt.id, attempt.fence
    await worker._apply(
        snapshot,
        attempt_id,
        fence,
        BrainstormGatewayResult(
            proposal=BrainstormProposal(turn_id=before.proposal.turn_id, problem="Forged replay"),
            telemetry=AttemptTelemetry(input_tokens=0, output_tokens=0, duration_ms=0),
        ),
        "unavailable",
        asyncio.Event(),
    )
    after = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=snapshot.conversation_id
    )
    assert after == before and len(turns) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_capped_unknown_token_policy_is_honest(brainstorm_session_factory, allowed):
    budget = TaskBudget(
        max_input_tokens=10,
        max_output_tokens=10,
        unknown_telemetry_policy=UnknownTelemetryPolicy(allow_unknown_tokens=allowed),
    )
    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=budget
    )

    class UnknownGateway(FakeGateway):
        async def execute(self, *args, **kwargs):
            result = await super().execute(*args, **kwargs)
            return replace(
                result,
                telemetry=AttemptTelemetry(input_tokens=None, output_tokens=5, duration_ms=10),
            )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="unknown-worker",
        gateway_factory=lambda _: UnknownGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == ("proposed" if allowed else "failed")
    assert outcome.failure == (None if allowed else "invalid_output")
    assert outcome.held_reservations.input_tokens == 10


@pytest.mark.asyncio
async def test_measured_provider_overrun_fails_with_budget_category(brainstorm_session_factory):
    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_input_tokens=2)
    )

    class OverrunGateway(FakeGateway):
        async def execute(self, *args, **kwargs):
            result = await super().execute(*args, **kwargs)
            return replace(
                result, telemetry=AttemptTelemetry(input_tokens=4, output_tokens=5, duration_ms=10)
            )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="overrun-worker",
        gateway_factory=lambda _: OverrunGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "budget_exhausted"
    assert outcome.cumulative_usage.input_tokens == 4 and outcome.proposal is None


@pytest.mark.asyncio
async def test_cancel_active_supervised_peer_reaps_before_settlement(brainstorm_session_factory):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    started = asyncio.Event()

    class SleepingGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            class NotifyLifecycle:
                async def launch_intent(self, launch_id):
                    await lifecycle.launch_intent(launch_id)

                async def started(self, process_receipt):
                    await lifecycle.started(process_receipt)
                    started.set()

                async def finished(self, process_receipt, result):
                    await lifecycle.finished(process_receipt, result)

            async def slow_revoke():
                await asyncio.sleep(1.6)

            peer = await ClientProcessSupervisor().start(
                ClientLaunchSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        "import sys,time; sys.stdin.readline(); time.sleep(30)",
                    ),
                    cwd=".",
                    environment={},
                    duration_seconds=40,
                ),
                lifecycle=NotifyLifecycle(),
                before_stop=slow_revoke,
            )
            try:
                await peer.send({"prompt": turns[-1].text})
                await peer.close_stdin()
                while await peer.receive() is not None:
                    pass
            finally:
                await peer.close()
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: SleepingGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 5)
    current = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="cancel-active",
    )
    assert await asyncio.wait_for(task, 15) == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "cancelled" and outcome.process_settled


@pytest.mark.asyncio
async def test_confirmed_exhaustion_blocks_pool_before_process_settles(brainstorm_session_factory):
    _, _, _, _, receipt = await prepared(brainstorm_session_factory)
    reset_at = datetime.now(UTC) + timedelta(hours=1)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=reset_at)
        assert not attempt.process_settled
    async with brainstorm_session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        pool = await PostgresBrainstormRepository(session).quota_pool(
            PostgresBrainstormRepository.decode_snapshot(job)
        )
        assert pool.blocked and pool.retry_basis == "known_reset"
        assert pool.next_eligible_at == reset_at
    # The base migration deliberately refuses to discard durable quota evidence.
    # Restore this disposable test database so its downgrade can complete.
    from sqlalchemy import delete

    async with brainstorm_session_factory() as session, session.begin():
        await session.execute(
            delete(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_attempt_id == attempt.id
            )
        )
        pool = await session.get(
            SubscriptionQuotaPool, (pool.provider, pool.account, pool.pool), with_for_update=True
        )
        assert pool is not None
        pool.blocked = False
        pool.probe_attempt_id = None


@pytest.mark.asyncio
async def test_expired_admission_without_launch_recovers_and_releases_budget(
    brainstorm_session_factory,
):
    service, epic_id, project_id, _actor, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("dead-worker")
        assert claimed is not None
        _, attempt = claimed
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="replacement",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.process_settled
    assert outcome.held_reservations.duration_ms > 0
    assert outcome.usage.duration_ms is None


@pytest.mark.asyncio
async def test_job_cancel_does_not_stop_shared_poller(brainstorm_session_factory):
    service, epic_id, project_id, actor, receipt = await prepared(brainstorm_session_factory)
    started = asyncio.Event()

    class WaitingGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            started.set()
            await asyncio.Event().wait()

    shared_stop = asyncio.Event()
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: WaitingGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once(stop_event=shared_stop))
    await asyncio.wait_for(started.wait(), 5)
    current = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="cancel-shared",
    )
    assert await asyncio.wait_for(task, 5) == receipt.job_id
    assert not shared_stop.is_set()


@pytest.mark.asyncio
async def test_second_invocation_receives_remaining_epic_capacity(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_provider_attempts=2, max_input_tokens=10, max_output_tokens=12),
    )
    budgets = []

    class RecordingGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            budgets.append(job.budget)
            return await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: RecordingGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="capacity-create", text="Follow-up"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="capacity-submit",
    )
    assert await worker.run_once() == second.job_id
    assert [(b.max_input_tokens, b.max_output_tokens) for b in budgets] == [(10, 12), (6, 7)]


@pytest.mark.asyncio
async def test_follow_up_gateway_sees_full_prior_proposal(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(brainstorm_session_factory)
    seen = []

    class ProposalGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            seen.append(turns)
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            proposal = result.proposal.model_copy(
                update={
                    "decisions": ("Keep the existing API",),
                    "open_questions": ("Choose rollout date",),
                }
            )
            return replace(result, proposal=proposal)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: ProposalGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, first.job_id)
        conversation_id = row.conversation_id
    history = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
    )
    assert history[-1].proposal is not None and history[-1].proposal.decisions == (
        "Keep the existing API",
    )
    new_version = await service.append(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        expected_version=len(history) + 1,
        actor=actor,
        key="followup-turn",
        text="What about the rollout date?",
    )
    latest = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[-1]
    second = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=latest.turn_id,
        expected_epic_version=1,
        expected_conversation_version=new_version,
        actor=actor,
        key="followup-job",
    )
    assert await worker.run_once() == second.job_id
    assert seen[-1][-2].proposal.open_questions == ("Choose rollout date",)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_caller_cancellation_reaps_physical_peer_and_propagates(
    brainstorm_session_factory, shutdown
):
    service, epic_id, project_id, actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(
            max_provider_attempts=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=2),
        ),
    )
    started = asyncio.Event()

    class SleepingGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            class NotifyLifecycle:
                async def launch_intent(self, launch_id):
                    await lifecycle.launch_intent(launch_id)

                async def started(self, process_receipt):
                    await lifecycle.started(process_receipt)
                    started.set()

                async def finished(self, process_receipt, result):
                    await lifecycle.finished(process_receipt, result)

            await ClientProcessSupervisor().run(
                ClientLaunchSpec(
                    argv=(
                        sys.executable,
                        "-c",
                        "import sys,time; sys.stdin.readline(); time.sleep(30)",
                    ),
                    cwd=".",
                    environment={},
                    duration_seconds=40,
                ),
                {"prompt": turns[-1].text},
                lifecycle=NotifyLifecycle(),
            )
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: SleepingGateway(),
        reader_factory=lambda _: object(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_once(stop_event=stop))
    await asyncio.wait_for(started.wait(), 5)
    if shutdown:
        stop.set()
        assert await asyncio.wait_for(task, 15) == receipt.job_id
    else:
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "interrupted"
    assert outcome.process_settled
    retried = await service.retry(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=outcome.job_version,
        actor=actor,
        key="retry-interrupted",
    )
    assert retried.state == "queued"
    resumed = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-b",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await resumed.run_once() == receipt.job_id


@pytest.mark.asyncio
async def test_zero_uncertain_allowance_still_admits_measured_attempt(brainstorm_session_factory):
    _, _, _, _, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(
            max_provider_attempts=1,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=0),
        ),
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id


@pytest.mark.asyncio
async def test_provider_failure_text_is_closed_before_persistence(brainstorm_session_factory):
    service, epic_id, project_id, _actor, receipt = await prepared(brainstorm_session_factory)

    class LeakingGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            return replace(result, proposal=None, failure="credential sk-private from stderr")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: LeakingGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "unavailable"


@pytest.mark.asyncio
async def test_invalid_gateway_currency_keeps_numeric_charges_without_persisting_credential(
    brainstorm_session_factory,
):
    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_provider_attempts=2, max_cost_minor=20),
    )

    class LeakingTelemetry(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            return replace(
                result,
                telemetry=replace(
                    result.telemetry,
                    input_tokens=7,
                    output_tokens=8,
                    estimated_api_cost_minor=5,
                    currency="api_key=secret-provider-value",
                ),
                quota_reset_at="api_key=secret-reset-value",
            )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="currency-worker",
        gateway_factory=lambda _: LeakingTelemetry(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "invalid_output"
    assert outcome.cumulative_usage.estimated_api_cost_minor == 5
    assert outcome.held_reservations.estimated_api_cost_minor == 15
    assert outcome.currency is None and outcome.usage_known is False
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None and row.current_attempt_id is not None
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        assert attempt is not None
        assert attempt.usage["input_tokens"] == 7
        assert attempt.usage["output_tokens"] == 8
        assert attempt.usage["currency"] is None
        assert "secret-provider-value" not in str(attempt.usage)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dimension",
    ("input_tokens", "output_tokens", "tool_call_count", "duration_ms", "estimated_api_cost_minor"),
)
async def test_oversized_gateway_measurement_is_unknown_but_status_remains_readable(
    brainstorm_session_factory,
    dimension,
):
    limit = 2**63 - 1
    service, epic_id, project_id, actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(
            max_provider_attempts=2,
            max_input_tokens=100,
            max_output_tokens=100,
            max_cost_minor=100,
        ),
    )

    class OversizedGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            telemetry = replace(result.telemetry, **{dimension: limit + 1})
            return replace(result, telemetry=telemetry)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner=f"oversized-{dimension}",
        gateway_factory=lambda _: OversizedGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "invalid_output"
    assert outcome.process_settled and outcome.usage_known is False
    assert outcome.usage is not None
    if dimension == "tool_call_count":
        assert outcome.usage.tool_call_count == 0
        assert outcome.held_reservations.tool_call_count > 0
    elif dimension == "duration_ms":
        assert outcome.usage.duration_ms is None
        assert outcome.usage.duration_lower_bound_ms > 0
        assert outcome.held_reservations.duration_ms > 0
    else:
        assert getattr(outcome.usage, dimension) is None
        assert getattr(outcome.held_reservations, dimension) > 0
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None and row.current_attempt_id is not None
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        assert attempt is not None and str(limit + 1) not in str(attempt.usage)
    if dimension == "input_tokens":
        second = await submit_second_on_epic(
            service, epic_id, project_id, actor, prefix="oversized-input"
        )
        assert await worker.run_once() is None
        blocked = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=second.job_id
        )
        assert blocked.state == "failed" and blocked.failure == "budget_exhausted"


@pytest.mark.asyncio
async def test_safe_individual_measurements_with_overflowing_sum_keep_status_readable(
    brainstorm_session_factory,
):
    limit = 2**63 - 1
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_provider_attempts=2)
    )
    second = await submit_second_on_epic(
        service, epic_id, project_id, actor, prefix="aggregate-overflow"
    )
    amounts = iter((limit - 1, 2))

    class LargeButValidGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            return replace(result, telemetry=replace(result.telemetry, input_tokens=next(amounts)))

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="aggregate-worker",
        gateway_factory=lambda _: LargeButValidGateway(),
        reader_factory=lambda _: object(),
    )
    assert {await worker.run_once(), await worker.run_once()} == {first.job_id, second.job_id}
    for job in (first, second):
        outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=job.job_id)
        assert outcome.cumulative_usage.input_tokens == limit
        assert outcome.held_reasons.input_tokens == "unsettled_or_unknown"
        assert outcome.state == "proposed"
    async with brainstorm_session_factory() as session:
        repository = PostgresBrainstormRepository(session)
        charged, held, _unknown = repository._charges(await repository._epic_attempts(epic_id))
        assert charged["input_tokens"] == limit + 1
        assert held["input_tokens"] == 0


@pytest.mark.asyncio
async def test_maximum_representable_measurement_remains_exact(brainstorm_session_factory):
    limit = 2**63 - 1
    service, epic_id, project_id, _actor, receipt = await prepared(brainstorm_session_factory)

    class MaximumGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            return replace(result, telemetry=replace(result.telemetry, input_tokens=limit))

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="maximum-worker",
        gateway_factory=lambda _: MaximumGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "proposed"
    assert outcome.usage.input_tokens == limit
    assert outcome.cumulative_usage.input_tokens == limit
    assert outcome.held_reasons.input_tokens is None


@pytest.mark.asyncio
async def test_preexisting_malformed_usage_and_reservation_cannot_poison_epic_status(
    brainstorm_session_factory,
):
    limit = 2**63 - 1
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_provider_attempts=2, max_input_tokens=100),
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="legacy-worker",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, first.job_id)
        assert row is not None and row.current_attempt_id is not None
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        assert attempt is not None
        attempt.usage = {**attempt.usage, "input_tokens": limit + 1}
        attempt.usage_known = True
    second = await submit_second_on_epic(service, epic_id, project_id, actor, prefix="legacy")
    for job in (first, second):
        outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=job.job_id)
        assert outcome.cumulative_usage.input_tokens == 0
        assert outcome.held_reservations.input_tokens == 100
        assert outcome.uncertain_attempts == 1
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, first.job_id)
        assert row is not None and row.current_attempt_id is not None
        attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
        assert attempt is not None
        attempt.reservation = {**attempt.reservation, "input_tokens": limit + 1}
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert outcome.reservation is None
    assert outcome.held_reservations.input_tokens == limit
    assert outcome.held_reasons.input_tokens == "unsettled_or_unknown"
    async with brainstorm_session_factory() as session:
        repository = PostgresBrainstormRepository(session)
        _charged, held, _unknown = repository._charges(await repository._epic_attempts(epic_id))
        assert held["input_tokens"] == limit + 1


@pytest.mark.asyncio
async def test_oversized_telemetry_keeps_quota_wait_and_later_cancel_readable(
    brainstorm_session_factory,
):
    service, epic_id, project_id, actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_provider_attempts=2, max_input_tokens=100),
    )

    class ExhaustedGateway(FakeGateway):
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            result = await super().execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
            return replace(
                result,
                proposal=None,
                failure="quota_exhausted",
                telemetry=replace(result.telemetry, input_tokens=2**63),
            )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="quota-overflow-worker",
        gateway_factory=lambda _: ExhaustedGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == receipt.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "quota_wait" and outcome.failure == "quota_exhausted"
    assert outcome.usage.input_tokens is None
    assert outcome.held_reservations.input_tokens == 100
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=outcome.job_version,
        actor=actor,
        key="cancel-bad-telemetry",
    )
    cancelled_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
    )
    assert cancelled_outcome.state == "cancelled"
    assert cancelled_outcome.usage.input_tokens is None
    assert cancelled_outcome.cumulative_usage.input_tokens == 0
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None and row.current_attempt_id is not None
        repository = PostgresBrainstormRepository(session)
        pool = await repository.quota_pool(repository.decode_snapshot(row))
        await session.execute(
            delete(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_attempt_id == row.current_attempt_id
            )
        )
        pool.blocked = False
        pool.probe_attempt_id = None


async def submit_second_on_epic(service, epic_id, project_id, actor, *, prefix):
    conversation_id, version = await service.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key=f"{prefix}-create",
        text="Second idea",
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    return await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key=f"{prefix}-submit",
    )


@pytest.mark.asyncio
async def test_read_before_launch_failure_charges_epic_tool_capacity(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_provider_attempts=2, max_tool_calls=1)
    )

    class Reader:
        def excludes_paths(self, paths):
            return False

    class ReadThenFail:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            await reader.excludes_paths(("README.md",))
            await asyncio.sleep(0.02)
            raise RuntimeError("client unavailable before launch")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: ReadThenFail(),
        reader_factory=lambda _: Reader(),
    )
    assert await worker.run_once() == first.job_id
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert outcome.state == "failed" and outcome.process_settled
    assert outcome.usage.tool_call_count == 1
    assert outcome.usage.duration_ms > 0
    assert outcome.cumulative_usage.tool_call_count == 1
    second = await submit_second_on_epic(
        service, epic_id, project_id, actor, prefix="read-before-launch"
    )
    assert await worker.run_once() is None
    blocked = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert blocked.state == "failed" and blocked.failure == "budget_exhausted"


@pytest.mark.asyncio
async def test_expired_read_before_launch_recovery_keeps_tool_charge(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_provider_attempts=2, max_tool_calls=1)
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("dead-worker")
        assert claimed is not None
        _, attempt = claimed
        attempt.tool_calls_used = 1
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="replacement",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    assert await worker.run_once() == first.job_id
    recovered = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert recovered.usage.tool_call_count == 1
    assert recovered.cumulative_usage.tool_call_count == 1
    second = await submit_second_on_epic(service, epic_id, project_id, actor, prefix="expired-read")
    assert await worker.run_once() is None
    blocked = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert blocked.state == "failed" and blocked.failure == "budget_exhausted"


@pytest.mark.asyncio
async def test_cancel_after_read_before_launch_keeps_measured_usage(brainstorm_session_factory):
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_provider_attempts=2, max_tool_calls=1)
    )
    read = asyncio.Event()

    class Reader:
        def excludes_paths(self, paths):
            return False

    class ReadThenWait:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            await reader.excludes_paths(("README.md",))
            read.set()
            await asyncio.Event().wait()

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="worker-a",
        gateway_factory=lambda _: ReadThenWait(),
        reader_factory=lambda _: Reader(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(read.wait(), 5)
    current = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=first.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="cancel-after-read",
    )
    assert await asyncio.wait_for(task, 5) == first.job_id
    cancelled = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert cancelled.state == "cancelled" and cancelled.usage.tool_call_count == 1
    second = await submit_second_on_epic(
        service, epic_id, project_id, actor, prefix="cancelled-read"
    )
    assert await worker.run_once() is None
    blocked = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert blocked.failure == "budget_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ("stop", "deadline", "caller", "repeated_caller"))
async def test_late_gateway_failure_after_bounded_exit_is_consumed(
    brainstorm_session_factory, monkeypatch, interruption: str
) -> None:
    import forge.worker.epic_brainstorm as worker_module

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_duration_seconds=1) if interruption == "deadline" else None,
    )
    monkeypatch.setattr(worker_module, "_OPERATION_GRACE_SECONDS", 0.02, raising=False)
    entered, release = asyncio.Event(), asyncio.Event()
    contexts = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))

    class ResistantGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise RuntimeError("SYNTHETIC_PROVIDER_PRIVATE_TEXT")

    stop = asyncio.Event()
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="late-failure-worker",
        gateway_factory=lambda _: ResistantGateway(),
        reader_factory=lambda _: object(),
    )
    try:
        task = asyncio.create_task(worker.run_once(stop_event=stop))
        await asyncio.wait_for(entered.wait(), 5)
        if interruption == "stop":
            stop.set()
        elif interruption in ("caller", "repeated_caller"):
            task.cancel()
            if interruption == "repeated_caller":
                await asyncio.sleep(0)
                task.cancel()
        if interruption in ("caller", "repeated_caller"):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
        else:
            assert await asyncio.wait_for(task, 15) == receipt.job_id
        release.set()
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        assert not contexts
        assert not worker._active_operations
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert outcome.proposal is None
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("length", (117, 118, 128, 255))
def test_brainstorm_owner_composition_is_bounded_and_distinct(length: int) -> None:
    from forge.worker.epic_brainstorm import brainstorm_worker_owner

    base = "x" * length
    owner = brainstorm_worker_owner(base)
    assert len(owner) <= 128
    if length == 117:
        assert owner == base + "-brainstorm"
    else:
        assert owner != brainstorm_worker_owner("x" * (length - 1) + "y")
    if length > 117:
        with pytest.raises(ValueError):
            EpicBrainstormWorker(
                None,
                owner=base + "-brainstorm",
                gateway_factory=lambda _: None,
                reader_factory=lambda _: None,
            )


@pytest.mark.parametrize("invalid", ("", "x" * 129, "a\x00b", "a\ud800b"))
def test_invalid_direct_brainstorm_owner_is_rejected_before_claim(invalid: str) -> None:
    with pytest.raises(ValueError, match="worker identity"):
        EpicBrainstormWorker(
            None,
            owner=invalid,
            gateway_factory=lambda _: None,
            reader_factory=lambda _: None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("late_result", ("quota", "exception", "success"))
async def test_duration_expiry_revokes_tools_launch_and_preserves_timeout(
    brainstorm_session_factory, late_result: str
) -> None:
    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(max_duration_seconds=1, max_tool_calls=2),
    )
    observations = {}

    class Reader:
        def excludes_paths(self, paths):
            return False

    class ResistantGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            observations["before_tool"] = await reader.excludes_paths(("README.md",))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                observations["cancelled_after_deadline"] = await cancelled()
                try:
                    await reader.excludes_paths(("README.md",))
                except BrainstormConflict:
                    observations["tool_denied"] = True
                else:
                    observations["tool_denied"] = False
                try:
                    await lifecycle.launch_intent(str(uuid4()))
                except BrainstormConflict:
                    observations["launch_denied"] = True
                else:
                    observations["launch_denied"] = False
                if late_result == "exception":
                    raise RuntimeError("SYNTHETIC_LATE_FAILURE")
                if late_result == "success":
                    return BrainstormGatewayResult(
                        proposal=BrainstormProposal(
                            turn_id=uuid4(), problem="Late proposal", requirements=("One",)
                        ),
                        telemetry=AttemptTelemetry(input_tokens=1, output_tokens=1, duration_ms=1),
                    )
                return BrainstormGatewayResult(
                    proposal=None, telemetry=None, failure="quota_exhausted"
                )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="deadline-worker",
        gateway_factory=lambda _: ResistantGateway(),
        reader_factory=lambda _: Reader(),
    )
    assert await worker.run_once() == receipt.job_id
    assert observations == {
        "before_tool": False,
        "cancelled_after_deadline": True,
        "tool_denied": True,
        "launch_denied": True,
    }
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "timeout"
    assert outcome.proposal is None and outcome.process_settled
    if late_result == "quota":
        # The disposable migration refuses to drop durable quota evidence.
        async with brainstorm_session_factory() as session, session.begin():
            job = await session.get(BrainstormJobRow, receipt.job_id)
            assert job is not None
            await session.execute(
                delete(SubscriptionQuotaObservation).where(
                    SubscriptionQuotaObservation.source_attempt_id == job.current_attempt_id
                )
            )
            pool = await PostgresBrainstormRepository(session).quota_pool(
                PostgresBrainstormRepository.decode_snapshot(job)
            )
            assert pool.blocked and pool.next_eligible_at is not None
            pool.blocked = False
            pool.probe_attempt_id = None


@pytest.mark.asyncio
async def test_caller_cancellation_during_expired_cleanup_keeps_timeout(
    brainstorm_session_factory, monkeypatch
) -> None:
    import forge.worker.epic_brainstorm as worker_module

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=1)
    )
    monkeypatch.setattr(worker_module, "_OPERATION_GRACE_SECONDS", 2.0)
    cancelled_by_deadline = asyncio.Event()
    release = asyncio.Event()

    class ResistantGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_by_deadline.set()
                await release.wait()
                return BrainstormGatewayResult(proposal=None, telemetry=None)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="deadline-caller-worker",
        gateway_factory=lambda _: ResistantGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(cancelled_by_deadline.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert (outcome.state, outcome.failure) == ("failed", "timeout")
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_callback_rechecks_expiry_after_database_wait(
    brainstorm_session_factory, monkeypatch
) -> None:
    import forge.worker.epic_brainstorm as worker_module
    from sqlalchemy.ext.asyncio import AsyncSession

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=3)
    )
    original_get = AsyncSession.get
    entered, release = asyncio.Event(), asyncio.Event()
    callback_results = []
    job_reads = 0
    expired = False
    original_expired = worker_module._invocation_expired
    monkeypatch.setattr(
        worker_module,
        "_invocation_expired",
        lambda admitted, reservation: expired or original_expired(admitted, reservation),
    )

    async def delayed_get(self, entity, ident, *args, **kwargs):
        nonlocal job_reads
        if entity is BrainstormJobRow and ident == receipt.job_id:
            job_reads += 1
            if job_reads == 2:
                entered.set()
                await release.wait()
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", delayed_get)

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            callback_results.append(await cancelled())
            callback_results.append(await cancelled())
            return BrainstormGatewayResult(proposal=None, telemetry=None)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="callback-expiry-worker",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expired = True
        release.set()
        assert await asyncio.wait_for(task, 5) == receipt.job_id
        assert callback_results == [False, True]
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert outcome.failure == "timeout"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_callback_rechecks_expiry_after_session_close(
    brainstorm_session_factory, monkeypatch
) -> None:
    import forge.worker.epic_brainstorm as worker_module
    from sqlalchemy.ext.asyncio import AsyncSession

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=3)
    )
    original_exit = AsyncSession.__aexit__
    original_expired = worker_module._invocation_expired
    entered, release = asyncio.Event(), asyncio.Event()
    expired = False
    callback_task = None
    callback_results = []
    monkeypatch.setattr(
        worker_module,
        "_invocation_expired",
        lambda admitted, reservation: expired or original_expired(admitted, reservation),
    )

    async def delayed_exit(self, *args):
        result = await original_exit(self, *args)
        if asyncio.current_task() is callback_task:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncSession, "__aexit__", delayed_exit)

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            nonlocal callback_task
            callback_task = asyncio.current_task()
            callback_results.append(await cancelled())
            return BrainstormGatewayResult(proposal=None, telemetry=None)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="callback-close-worker",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expired = True
        release.set()
        assert await asyncio.wait_for(task, 5) == receipt.job_id
        assert callback_results == [True]
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert (outcome.state, outcome.failure) == ("failed", "timeout")
    finally:
        release.set()


@pytest.mark.asyncio
async def test_poll_uses_timeout_when_cancel_check_crosses_deadline(
    brainstorm_session_factory, monkeypatch
) -> None:
    import forge.worker.epic_brainstorm as worker_module
    from sqlalchemy.ext.asyncio import AsyncSession

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=3)
    )
    original_get = AsyncSession.get
    original_expired = worker_module._invocation_expired
    entered, release = asyncio.Event(), asyncio.Event()
    expired = False
    intercepted = False
    monkeypatch.setattr(
        worker_module,
        "_invocation_expired",
        lambda admitted, reservation: expired or original_expired(admitted, reservation),
    )

    async def delayed_get(self, entity, ident, *args, **kwargs):
        nonlocal intercepted
        if entity is BrainstormJobRow and ident == receipt.job_id and not intercepted:
            intercepted = True
            entered.set()
            await release.wait()
            return None  # A stale absence also reports cancellation to the poll.
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", delayed_get)

    class ResistantGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return BrainstormGatewayResult(proposal=None, telemetry=None)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="poll-deadline-worker",
        gateway_factory=lambda _: ResistantGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expired = True
        release.set()
        assert await asyncio.wait_for(task, 5) == receipt.job_id
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert (outcome.state, outcome.failure) == ("failed", "timeout")
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("launched", (False, True))
async def test_predeadline_stop_keeps_interrupted_cause_across_expiry_and_recovery(
    brainstorm_session_factory, monkeypatch, launched: bool
) -> None:
    import forge.worker.epic_brainstorm as worker_module

    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=3)
    )
    monkeypatch.setattr(worker_module, "_OPERATION_GRACE_SECONDS", 0.2)
    entered, aged, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()
    peer = None

    class ResistantGateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            nonlocal peer
            if launched:
                peer = await ClientProcessSupervisor().start(
                    ClientLaunchSpec(
                        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                        cwd=".",
                        environment={},
                        duration_seconds=30,
                    ),
                    lifecycle=lifecycle,
                )
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                async with brainstorm_session_factory() as session, session.begin():
                    row = await session.get(BrainstormJobRow, receipt.job_id)
                    assert row is not None and row.current_attempt_id is not None
                    attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
                    assert attempt is not None
                    attempt.created_at = datetime.now(UTC) - timedelta(seconds=4)
                aged.set()
                await release.wait()
                return BrainstormGatewayResult(proposal=None, telemetry=None)

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="predeadline-stop-worker",
        gateway_factory=lambda _: ResistantGateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once(stop_event=stop))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        await asyncio.wait_for(aged.wait(), 5)
        assert await asyncio.wait_for(task, 5) == receipt.job_id
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert (outcome.state, outcome.failure) == (
            ("reconciling", "interrupted") if launched else ("failed", "interrupted")
        )
        if launched:
            assert outcome.held_reservations.tool_call_count > 0
            async with brainstorm_session_factory() as session, session.begin():
                row = await session.get(BrainstormJobRow, receipt.job_id)
                assert row is not None
                row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
            restarted = EpicBrainstormWorker(
                brainstorm_session_factory,
                owner="reopened-worker",
                gateway_factory=lambda _: None,
                reader_factory=lambda _: object(),
            )
            assert await restarted.reconcile_settled() is None
            pending = await service.observe(
                epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
            )
            assert (pending.state, pending.failure) == ("reconciling", "interrupted")
            assert peer is not None
            await peer.close()
            async with brainstorm_session_factory() as session, session.begin():
                row = await session.get(BrainstormJobRow, receipt.job_id)
                assert row is not None
                row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
            assert await restarted.reconcile_settled() == receipt.job_id
            settled = await service.observe(
                epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
            )
            assert (settled.state, settled.failure) == ("failed", "interrupted")
    finally:
        release.set()
        if peer is not None:
            await peer.close()


@pytest.mark.asyncio
async def test_recovery_preserves_expired_duration_cause_and_holds(brainstorm_session_factory):
    service, epic_id, project_id, _actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=1)
    )
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("deadline-recovery", 5)
        assert claimed is not None
        row, attempt = claimed
        snapshot = repository.decode_snapshot(row)
        attempt.created_at = datetime.now(UTC) - timedelta(seconds=3)
        attempt.launch_intent = True
        attempt.launch_id = str(uuid4())
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        attempt_id, fence = attempt.id, attempt.fence
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="deadline-recovery",
        gateway_factory=lambda _: None,
        reader_factory=lambda _: object(),
    )
    await worker._apply(snapshot, attempt_id, fence, None, "timeout", asyncio.Event())
    initial = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert initial.state == "reconciling" and initial.failure == "timeout"
    assert initial.cumulative_usage.duration_ms + initial.held_reservations.duration_ms >= 1000
    assert await worker.reconcile_settled() is None
    again = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert again.state == "reconciling" and again.failure == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("human_cancel", (False, True))
async def test_expired_duration_denies_new_start_but_accepts_real_terminal_proof(
    brainstorm_session_factory, human_cancel: bool
) -> None:
    service, epic_id, project_id, actor, receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=2)
    )
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("deadline-process", 5)
        assert claimed is not None
        row, attempt = claimed
        snapshot = repository.decode_snapshot(row)
        attempt_id, fence = attempt.id, attempt.fence
    lifecycle = DurableBrainstormProcessLifecycle(
        brainstorm_session_factory, attempt_id, fence, "deadline-process"
    )
    peer = await ClientProcessSupervisor().start(
        ClientLaunchSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=".",
            environment={},
            duration_seconds=40,
        ),
        lifecycle=lifecycle,
    )
    try:
        async with brainstorm_session_factory() as session, session.begin():
            stored = await session.get(BrainstormAttemptRow, attempt_id)
            assert stored is not None and stored.process_started
            stored.created_at = datetime.now(UTC) - timedelta(seconds=3)
        with pytest.raises(BrainstormConflict, match="expired"):
            await lifecycle.launch_intent(peer.receipt.launch_id)
        with pytest.raises(BrainstormConflict, match="expired"):
            await lifecycle.started(peer.receipt)
        if human_cancel:
            observed = await service.observe(
                epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
            )
            await service.cancel(
                epic_id=epic_id,
                project_id=project_id,
                job_id=receipt.job_id,
                expected_job_version=observed.job_version,
                actor=actor,
                key="deadline-cancel",
            )
        terminal = await peer.close()
        assert terminal.stop_confirmed
        async with brainstorm_session_factory() as session:
            stored = await session.get(BrainstormAttemptRow, attempt_id)
            assert stored is not None and stored.process_settled and stored.terminal_proof
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="deadline-process",
            gateway_factory=lambda _: None,
            reader_factory=lambda _: object(),
        )
        await worker._apply(snapshot, attempt_id, fence, None, "timeout", asyncio.Event())
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert outcome.process_settled
        assert (outcome.state, outcome.failure) == (
            ("cancelled", "cancelled") if human_cancel else ("failed", "timeout")
        )
    finally:
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ("expired", "cancelled"))
async def test_delayed_real_start_receipt_is_persisted_before_revoked_launch_rejection(
    brainstorm_session_factory,
    revocation: str,
) -> None:
    service, epic_id, project_id, actor, job_receipt = await prepared(
        brainstorm_session_factory, budget=TaskBudget(max_duration_seconds=2)
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("late-start", 5)
        assert claimed is not None
        _, attempt = claimed
        attempt_id, fence = attempt.id, attempt.fence
    durable = DurableBrainstormProcessLifecycle(
        brainstorm_session_factory, attempt_id, fence, "late-start"
    )
    received = []

    class DelayedLifecycle:
        async def launch_intent(self, launch_id):
            await durable.launch_intent(launch_id)

        async def started(self, receipt):
            received.append(receipt)
            if revocation == "expired":
                async with brainstorm_session_factory() as session, session.begin():
                    stored = await session.get(BrainstormAttemptRow, attempt_id)
                    assert stored is not None
                    stored.created_at = datetime.now(UTC) - timedelta(seconds=3)
            else:
                observed = await service.observe(
                    epic_id=epic_id, project_id=project_id, job_id=job_receipt.job_id
                )
                await service.cancel(
                    epic_id=epic_id,
                    project_id=project_id,
                    job_id=job_receipt.job_id,
                    expected_job_version=observed.job_version,
                    actor=actor,
                    key="delayed-start-cancel",
                )
            await durable.started(receipt)

        async def finished(self, receipt, result):
            await durable.finished(receipt, result)

    with pytest.raises(BrainstormConflict, match="expired"):
        await ClientProcessSupervisor().start(
            ClientLaunchSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=".",
                environment={},
                duration_seconds=40,
            ),
            lifecycle=DelayedLifecycle(),
        )
    assert received
    async with brainstorm_session_factory() as session:
        stored = await session.get(BrainstormAttemptRow, attempt_id)
        assert stored is not None
        assert stored.process_started and stored.process_settled and stored.terminal_proof
        assert stored.process_pid == received[0].pid
