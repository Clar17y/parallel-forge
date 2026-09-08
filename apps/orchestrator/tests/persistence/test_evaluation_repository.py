"""Evaluation runs preserve their manifest on idempotent replay."""

import pytest
from forge.persistence.repositories.evaluations import EvaluationConflict, EvaluationRepository

pytestmark = pytest.mark.asyncio


async def test_suite_replay_preserves_manifest_and_refuses_incomplete_success(session_factory):
    async with session_factory() as session, session.begin():
        repository = EvaluationRepository(session)
        suite = await repository.begin_suite(
            name="deterministic", fixture_version="fixtures-v1", metric_version="metrics-v1",
            idempotency_key="evaluation-1", cases={"planner-basic": "planner"},
        )
    async with session_factory() as session, session.begin():
        repository = EvaluationRepository(session)
        replay = await repository.begin_suite(
            name="deterministic", fixture_version="fixtures-v1", metric_version="metrics-v1",
            idempotency_key="evaluation-1", cases={"planner-basic": "planner"},
        )
        assert replay == suite
        with pytest.raises(EvaluationConflict):
            await repository.finish_suite(suite.id)
        for changes in ({"fixture_version": "fixtures-v2"}, {"cases": {"other": "reviewer"}}):
            with pytest.raises(EvaluationConflict):
                await repository.begin_suite(**({
                    "name": "deterministic", "fixture_version": "fixtures-v1",
                    "metric_version": "metrics-v1", "idempotency_key": "evaluation-1",
                    "cases": {"planner-basic": "planner"},
                } | changes))

@pytest.mark.parametrize("passed", [True, False])
async def test_case_settlement_requires_bound_evidence_and_replays_exactly(
    tmp_path, session_factory, persisted_run, passed,
):
    from uuid import uuid4

    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.observability.usage import UsageRecord
    from forge.persistence.models import AgentExecution
    from forge.persistence.repositories.artifacts import ArtifactRepository
    from forge.persistence.repositories.usage import UsageRepository

    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(AgentExecution(
            id=execution_id, run_id=persisted_run.id, role="planner", instruction_version="v1",
            provider="fake", model="deterministic", status="SUCCEEDED",
        ))
        suite = await EvaluationRepository(session).begin_suite(
            name="deterministic", fixture_version="v1", metric_version="m1",
            idempotency_key="result-1", cases={"planner": "planner"},
        )
    usage = await UsageRepository(session_factory).add_priced(
        persisted_run.id, execution_id,
        UsageRecord(provider="fake", model="deterministic", input_tokens=1, output_tokens=1,
                    pricing_version="fake-v1", currency="USD", estimated_cost_minor=0),
    )
    store = FilesystemArtifactStore(tmp_path)
    artifacts = ArtifactRepository(session_factory)
    source = await artifacts.record(
        await store.put_bytes(b'{"input":true}', media_type="application/json"),
        run_id=persisted_run.id, producer_type="evaluation_input", producer_id=execution_id,
    )
    output = await artifacts.record(
        await store.put_bytes(b'{"output":true}', media_type="application/json"),
        run_id=persisted_run.id, producer_type="agent_execution", producer_id=execution_id,
        parent_digests=(source.digest,),
    )
    async with session_factory() as session, session.begin():
        execution = await session.get(AgentExecution, execution_id)
        execution.input_artifact_id, execution.output_artifact_id = source.artifact_id, output.artifact_id
    arguments = {
        "suite_id": suite.id, "case_key": "planner", "run_id": persisted_run.id, "usage_id": usage.id,
        "input_digest": source.digest, "output_digest": output.digest,
        "passed": passed, "metrics": {"schema_validity": 1.0},
    }
    async with session_factory() as session, session.begin():
        repository = EvaluationRepository(session)
        for changes in ({"usage_id": uuid4()}, {"run_id": uuid4()},
                        {"output_digest": "a" * 64}, {"output_digest": source.digest}):
            with pytest.raises(EvaluationConflict):
                await repository.record_case(**(arguments | changes))
        execution = await session.get(AgentExecution, execution_id)
        for field, value in (("role", "reviewer"), ("status", "RUNNING"), ("status", "FAILED"),
                             ("input_artifact_id", output.artifact_id),
                             ("output_artifact_id", source.artifact_id)):
            original = getattr(execution, field)
            setattr(execution, field, value)
            await session.flush()
            with pytest.raises(EvaluationConflict):
                await repository.record_case(**(arguments | {"passed": True}))
            setattr(execution, field, original)
            await session.flush()
        await repository.record_case(**arguments)
        assert (await repository.finish_suite(suite.id)).status == ("passed" if passed else "failed")
    async with session_factory() as session, session.begin():
        repository = EvaluationRepository(session)
        await repository.record_case(**arguments)
        with pytest.raises(EvaluationConflict):
            await repository.record_case(**(arguments | {"passed": not passed}))
        with pytest.raises(EvaluationConflict):
            await repository.record_case(**(arguments | {"metrics": {"schema_validity": 0.0}}))
        assert (await repository.finish_suite(suite.id)).status == ("passed" if passed else "failed")

async def test_concurrent_suite_creation_converges(session_factory):
    import asyncio

    async def create():
        async with session_factory() as session, session.begin():
            return await EvaluationRepository(session).begin_suite(
                name="deterministic", fixture_version="v1", metric_version="m1",
                idempotency_key="concurrent", cases={"one": "planner", "two": "reviewer"},
            )

    first, second = await asyncio.gather(create(), create())
    assert first == second


@pytest.mark.parametrize("metrics", [{}, {"bad key": 1}, {"score": float("nan")},
                                     {"score": float("inf")}, {"score": 10**1000},
                                     {"score": "claimed-success"}])
async def test_invalid_metrics_are_rejected_before_storage(session_factory, metrics):
    from uuid import uuid4

    async with session_factory() as session:
        with pytest.raises(ValueError):
            await EvaluationRepository(session).record_case(
                suite_id=uuid4(), case_key="one", run_id=uuid4(), usage_id=uuid4(),
                input_digest="a" * 64, output_digest="b" * 64, passed=True, metrics=metrics,
            )
