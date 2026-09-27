import asyncio
from uuid import uuid4

import httpx
import pytest
from forge.application.ports.jev import JevProviderResponse, JevRequest
from forge.application.services.jev import JevService
from forge.domain.policy import JevPolicy
from forge.domain.run import RunSnapshot
from forge.persistence.models import Project, ProjectPolicyVersion, Task
from forge.persistence.models.jev import JevEvaluation
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.ranking.jev import TypeSafeJevProvider
from sqlalchemy import delete


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, *, state, questions, model, timeout_seconds):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return JevProviderResponse(
            answers={"m0": {"score": 2, "confidence": 0.9}},
            actual_model=model,
            input_units=9000,
            output_units=5,
            duration_ms=10,
            request_id="safe-id",
        )


async def create_jev_run(session_factory, policy):
    project_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=f"/tmp/forge-{project_id}",
            github_repository=f"Owner/repo-{project_id}",
            default_branch="main",
        )
        session.add(project)
        await session.flush()
        session.add(
            ProjectPolicyVersion(
                project_id=project_id,
                version=1,
                policy_digest="a" * 64,
                document_schema_version=1,
                document={"jev": policy.model_dump(mode="json")},
            )
        )
        session.add(
            Task(id=task_id, project_id=project_id, normalized_text="task", task_digest="b" * 64)
        )
        await session.flush()
        project.current_policy_version = 1
    run = RunSnapshot(id=run_id, project_id=project_id, task_id=task_id, policy_version=1)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(run)
        await work.commit()
    return run


@pytest.mark.asyncio
async def test_concurrent_replay_never_resends_or_refunds_overestimated_usage(session_factory):
    policy = JevPolicy(
        mode="on", allow_remote=True, max_requests_per_run=1, max_input_units_per_run=10_000
    )
    persisted_run = await create_jev_run(session_factory, policy)
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    request = JevRequest(
        run_id=persisted_run.id,
        policy_version=1,
        operation_key="one",
        kind="semantic_search",
        worktree_digest="a" * 64,
        state={"objective": "needle"},
        questions={
            "m0": {
                "type": "score",
                "instructions": "relevance",
                "criteria": ["none", "some", "high"],
            }
        },
    )
    first = asyncio.create_task(service.evaluate(request, policy=policy))
    await provider.started.wait()
    replay = await service.evaluate(request, policy=policy)
    assert replay.status == "unknown"
    competing = JevRequest(
        run_id=request.run_id,
        policy_version=1,
        operation_key="competing",
        kind=request.kind,
        worktree_digest=request.worktree_digest,
        state=request.state,
        questions=request.questions,
    )
    assert (await service.evaluate(competing, policy=policy)).status == "unknown"
    assert provider.calls == 1
    provider.release.set()
    assert (await first).status == "succeeded"
    assert (await service.evaluate(request, policy=policy)).status == "cached"
    changed_kind_replay = JevRequest(
        run_id=request.run_id,
        policy_version=1,
        operation_key=request.operation_key,
        kind="review_focus",
        worktree_digest=request.worktree_digest,
        state=request.state,
        questions=request.questions,
    )
    with pytest.raises(ValueError, match="replay conflicts"):
        await service.evaluate(changed_kind_replay, policy=policy)
    second = JevRequest(
        run_id=request.run_id,
        policy_version=1,
        operation_key="two",
        kind=request.kind,
        worktree_digest=request.worktree_digest,
        state=request.state,
        questions=request.questions,
    )
    assert (await service.evaluate(second, policy=policy)).status == "cached"
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(request.run_id, policy=policy)
        assert summary["calls"] == 1
        assert summary["reserved_input_units"] == 9000
        assert summary["actual_input_units"] == 9000
        assert summary["cache_hits"] == 2
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == request.run_id))


@pytest.mark.asyncio
async def test_stored_policy_prevents_relaxed_callers(session_factory, persisted_run):
    provider = FakeProvider()
    provider.release.set()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    request = JevRequest(
        run_id=persisted_run.id,
        policy_version=1,
        operation_key="one",
        kind="search_ranking",
        worktree_digest="a" * 64,
        state={},
        questions={"m0": {"type": "score", "instructions": "x", "criteria": ["no", "yes"]}},
    )
    with pytest.raises(ValueError, match="does not authorize"):
        await service.evaluate(request, policy=JevPolicy(mode="on", allow_remote=True))
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_partial_scores_remain_unknown_and_cannot_be_retried(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True, max_requests_per_run=1)
    run = await create_jev_run(session_factory, policy)

    class PartialProvider(FakeProvider):
        async def evaluate(self, *, state, questions, model, timeout_seconds):
            self.calls += 1
            return JevProviderResponse(
                answers={},
                actual_model=model,
                input_units=9000,
                output_units=0,
                duration_ms=1,
                request_id=None,
            )

    provider = PartialProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    request = JevRequest(
        run_id=run.id,
        policy_version=1,
        operation_key="partial",
        kind="review_focus",
        worktree_digest="b" * 64,
        state={},
        questions={"m0": {"type": "score", "instructions": "relevance", "criteria": ["no", "yes"]}},
    )
    assert (await service.evaluate(request, policy=policy)).status == "unknown"
    assert (await service.evaluate(request, policy=policy)).status == "unknown"
    assert provider.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["unknown"] == 1
        assert summary["reserved_input_units"] == 9000
        assert summary["actual_input_units"] == 9000
        assert summary["review_focus_available"] is False
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))


@pytest.mark.asyncio
async def test_official_score_response_is_normalized_through_transport_and_service(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True)
    run = await create_jev_run(session_factory, policy)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "request_id": "provider-123",
                "answers": {
                    "m0": {
                        "type": "score",
                        "score": 1.05,
                        "legend": {"0": "none", "1": "some", "2": "high"},
                        "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05}
                        if calls == 1
                        else {"0": 1.0},
                        "confidence": 0.92,
                    }
                },
                "usage": {"input_tokens": 9000, "output_tokens": 18},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = JevService(
            lambda: PostgresUnitOfWork(session_factory),
            TypeSafeJevProvider(api_key="test-key", client=client),
        )
        request = JevRequest(
            run_id=run.id,
            policy_version=1,
            operation_key="official",
            kind="search_ranking",
            worktree_digest="c" * 64,
            state={"text": "example"},
            questions={
                "m0": {
                    "type": "score",
                    "instructions": "relevance",
                    "criteria": ["none", "some", "high"],
                }
            },
        )
        result = await service.evaluate(request, policy=policy)
        malformed = JevRequest(
            run_id=run.id,
            policy_version=1,
            operation_key="malformed",
            kind="search_ranking",
            worktree_digest="c" * 64,
            state={"text": "different"},
            questions=request.questions,
        )
        unknown = await service.evaluate(malformed, policy=policy)
    assert result.status == "ranked"
    assert result.answers == {"m0": {"score": 1.05, "confidence": 0.92}}
    assert result.actual_model == "jev-1.13.0"
    assert unknown.status == "unknown"
    assert unknown.actual_model == "jev-1.13.0"
    assert unknown.input_units == 9000
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["actual_input_units"] == 18000
        assert summary["calls"] == 2
        assert summary["unknown"] == 1
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))


@pytest.mark.asyncio
async def test_unavailable_is_observed_without_budget_and_cache_identity_is_scoped(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True, max_requests_per_run=5)
    run = await create_jev_run(session_factory, policy)
    questions = {
        "m0": {"type": "score", "instructions": "relevance", "criteria": ["none", "some", "high"]}
    }

    def request(key, *, scope="a" * 64, kind="review_focus", state=None):
        return JevRequest(
            run_id=run.id,
            policy_version=1,
            operation_key=key,
            kind=kind,
            worktree_digest="d" * 64,
            scope_digest=scope,
            state=state or {"text": "same"},
            questions=questions,
        )

    factory = lambda: PostgresUnitOfWork(session_factory)
    assert (
        await JevService(factory, None).evaluate(request("missing"), policy=policy)
    ).status == "unavailable"
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["availability"] == "degraded"
        assert summary["calls"] == 0
        assert summary["remaining_requests"] == 5
        assert summary["review_focus_available"] is False
    provider = FakeProvider()
    provider.release.set()
    service = JevService(factory, provider)
    assert (await service.evaluate(request("first"), policy=policy)).status == "succeeded"
    assert (await service.evaluate(request("same-content"), policy=policy)).status == "cached"
    assert provider.calls == 1
    assert (
        await service.evaluate(request("changed-scope", scope="e" * 64), policy=policy)
    ).status == "succeeded"
    assert (
        await service.evaluate(
            request("changed-content", state={"text": "different"}), policy=policy
        )
    ).status == "succeeded"
    assert (
        await service.evaluate(request("changed-kind", kind="semantic_search"), policy=policy)
    ).status == "succeeded"
    assert provider.calls == 4
    other_policy = JevPolicy(mode="on", allow_remote=True, model="future-jev-alias")
    other_run = await create_jev_run(session_factory, other_policy)
    other_request = JevRequest(
        run_id=other_run.id,
        policy_version=1,
        operation_key="different-model",
        kind="review_focus",
        worktree_digest="d" * 64,
        scope_digest="a" * 64,
        state={"text": "same"},
        questions=questions,
    )
    assert (await service.evaluate(other_request, policy=other_policy)).status == "succeeded"
    assert provider.calls == 5
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["calls"] == 4
        assert summary["cache_hits"] == 1
        assert summary["review_focus_available"] is True
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == other_run.id))


@pytest.mark.asyncio
async def test_cancellation_and_timeout_keep_unknown_admission(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True, timeout_seconds=1)
    run = await create_jev_run(session_factory, policy)
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)

    def request(key, state=None):
        return JevRequest(
            run_id=run.id,
            policy_version=1,
            operation_key=key,
            kind="semantic_search",
            worktree_digest="f" * 64,
            state=state or {},
            questions={
                "m0": {
                    "type": "score",
                    "instructions": "relevance",
                    "criteria": ["none", "some", "high"],
                }
            },
        )

    first = asyncio.create_task(service.evaluate(request("cancel"), policy=policy))
    await provider.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await service.evaluate(request("cancel"), policy=policy)).status == "unknown"
    assert (await service.evaluate(request("same-content"), policy=policy)).status == "unknown"
    assert provider.calls == 1
    assert (
        await service.evaluate(request("timeout", {"different": True}), policy=policy)
    ).status == "unknown"
    assert provider.calls == 2
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["unknown"] == 2
        assert summary["calls"] == 2
        assert summary["reserved_input_units"] > 0
        assert summary["duration_ms"] >= 900
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))


@pytest.mark.asyncio
async def test_cache_disabled_never_reuses_scores(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True, cache_ttl_seconds=0)
    run = await create_jev_run(session_factory, policy)
    provider = FakeProvider()
    provider.release.set()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)

    def request(key):
        return JevRequest(
            run_id=run.id,
            policy_version=1,
            operation_key=key,
            kind="search_ranking",
            worktree_digest="a" * 64,
            state={},
            questions={
                "m0": {
                    "type": "score",
                    "instructions": "relevance",
                    "criteria": ["none", "some", "high"],
                }
            },
        )

    assert (await service.evaluate(request("first"), policy=policy)).status == "ranked"
    assert (await service.evaluate(request("first"), policy=policy)).status == "unknown"
    assert (await service.evaluate(request("second"), policy=policy)).status == "ranked"
    assert provider.calls == 2
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))


@pytest.mark.parametrize("reason", ["budget_exhausted", "candidate_limit", "invalid_questions", "request_limit"])
async def test_refusals_are_recorded_without_remote_usage(session_factory, reason):
    policy = JevPolicy(mode="on", allow_remote=True, max_requests_per_run=1, max_candidates=1)
    run = await create_jev_run(session_factory, policy)
    provider = FakeProvider()
    provider.release.set()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    questions = {"m0": {"type": "score", "instructions": "x", "criteria": ["none", "some", "high"]}}
    request = JevRequest(run_id=run.id, policy_version=1, operation_key="accepted",
                         kind="semantic_search", worktree_digest="a" * 64, state={}, questions=questions)
    assert (await service.evaluate(request, policy=policy)).status == "succeeded"
    state = {"different": True}
    if reason == "candidate_limit":
        questions = {**questions, "m1": questions["m0"]}
    elif reason == "invalid_questions":
        questions = {"m0": {"type": "score", "instructions": "x", "criteria": ["only one"]}}
    elif reason == "request_limit":
        state = {"items": ["x" * 4000 for _ in range(100)]}
    refused = JevRequest(run_id=run.id, policy_version=1, operation_key="refused",
                         kind="semantic_search", worktree_digest="a" * 64, state=state, questions=questions)
    result = await service.evaluate(refused, policy=policy)
    assert result.status == ("budget_exhausted" if reason == "budget_exhausted" else "unavailable")
    assert (await service.evaluate(refused, policy=policy)).status == result.status
    assert provider.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        summary = await work.jev.summary(run.id, policy=policy)
        assert summary["attempts"] == 2
        assert summary["calls"] == 1
        assert summary["actual_input_units"] == 9000
        assert summary["availability"] == "degraded"
        assert summary["by_diagnostic"][reason] == 1
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))


async def test_agent_result_limit_does_not_discard_valid_provider_scores(session_factory):
    policy = JevPolicy(mode="on", allow_remote=True, max_result_chars=1)
    run = await create_jev_run(session_factory, policy)
    provider = FakeProvider()
    provider.release.set()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    request = JevRequest(run_id=run.id, policy_version=1, operation_key="small-result",
                         kind="semantic_search", worktree_digest="b" * 64, state={},
                         questions={"m0": {"type": "score", "instructions": "x", "criteria": ["none", "some", "high"]}})
    assert (await service.evaluate(request, policy=policy)).status == "succeeded"
    async with session_factory() as session, session.begin():
        await session.execute(delete(JevEvaluation).where(JevEvaluation.run_id == run.id))
