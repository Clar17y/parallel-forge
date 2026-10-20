"""Checks projections include controlled subscription executions without legacy rows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.operation import canonical_digest
from forge.domain.tool import ToolName
from forge.persistence.models import (
    ArtifactLineageParent,
    OperationIntent,
    Step,
    ToolCall,
    ValidationResult,
)
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.queries.check_history import check_history
from forge.persistence.queries.dashboard import DashboardQuery
from sqlalchemy import delete, null, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_named_check import _subscription_named_case


async def completed_case(
    session_factory, tmp_path, *, exit_code=1, cancelled=False, timed_out=False
):
    case, broker, task_id, attempt_id = await _subscription_named_case(session_factory, tmp_path)
    run_terminal = case.factory.run_terminal

    async def failed(request):
        terminal = await run_terminal(request)
        return replace(
            terminal,
            caller_cancelled=cancelled,
            result=replace(
                terminal.result, exit_code=exit_code, timed_out=timed_out, duration_ms=17
            ),
        )

    case.factory.run_terminal = failed
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    return case, receipt, task_id, attempt_id


@pytest.mark.parametrize("legacy", [False, True])
async def test_pages_without_subscription_rows_skip_subscription_hydration(
    session_factory, tmp_path, monkeypatch, legacy
):
    case, _, _, _ = await _subscription_named_case(session_factory, tmp_path)
    if legacy:
        async with session_factory() as session, session.begin():
            step = Step(run_id=case.context.run_id, kind="validate", attempt=3, status="FAILED")
            session.add(step)
            await session.flush()
            session.add(
                ValidationResult(
                    run_id=case.context.run_id,
                    step_id=step.id,
                    check_name="legacy",
                    command_name="unit",
                    command_version=1,
                    status="FAILED",
                    exit_code=2,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
    execute = AsyncSession.execute
    subscription_queries = []

    async def counted(session, statement, *args, **kwargs):
        entities = [item.get("entity") for item in getattr(statement, "column_descriptions", ())]
        if entities == [ToolCall, OperationIntent, SubscriptionAttempt]:
            subscription_queries.append(statement)
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", counted)
    items, more = await check_history(session_factory, case.context.run_id, 0, 25)
    assert not more
    assert [(item["name"], item["status"], item["exit_code"]) for item in items] == (
        [("legacy", "FAILED", 2)] if legacy else []
    )
    assert not subscription_queries


async def test_failed_subscription_check_is_visible_in_history_and_overview(
    session_factory, tmp_path
):
    case, receipt, task_id, attempt_id = await completed_case(session_factory, tmp_path)
    assert receipt.result["status"] == "failed"
    items, more = await check_history(session_factory, case.context.run_id, 0, 25)
    assert len(items) == 1
    check = items[0]
    assert check["id"] == receipt.operation_id
    assert check["status"] == "FAILED" and check["exit_code"] == 1
    assert check["attempt"] == 1 and check["head_sha"] is not None
    assert check["subscription_task_id"] == task_id
    assert check["subscription_attempt_id"] == attempt_id
    assert check["evidence_digest"] and check["output_artifact_digest"]
    assert not more
    overview = await DashboardQuery(session_factory).run_projection(case.context.run_id)
    assert overview["checks"][0]["id"] == receipt.operation_id
    assert overview["checks"][0]["status"] == "FAILED"


async def add_running_attempt(session_factory, call_id, *, status="RUNNING"):
    async with session_factory() as session, session.begin():
        source = await session.get(ToolCall, call_id)
        original = await session.get(OperationIntent, call_id)
        old_attempt = await session.get(SubscriptionAttempt, source.subscription_attempt_id)
        attempt_id, new_id = uuid4(), uuid4()
        session.add(
            SubscriptionAttempt(
                id=attempt_id,
                run_id=source.run_id,
                task_row_id=source.subscription_task_id,
                attempt_number=old_attempt.attempt_number + 1,
                idempotency_key=f"attempt:{attempt_id}",
                route_payload=old_attempt.route_payload,
                status="running",
            )
        )
        await session.flush()
        request = {
            **original.request_payload,
            "tool_call_id": str(new_id),
            "subscription_attempt_id": str(attempt_id),
        }
        session.add(
            OperationIntent(
                id=new_id,
                run_id=source.run_id,
                operation_kind="named_check",
                idempotency_key=f"named_check:{new_id}",
                request_schema_version=1,
                request_payload=request,
                request_digest=canonical_digest(request),
                status="PENDING",
            )
        )
        metadata = {
            key: source.result_metadata[key]
            for key in (
                "policy_version",
                "request_digest",
                "resource_id",
                "invocation_schema_version",
            )
        }
        metadata.update(operation_intent_id=str(new_id), correlation_id=str(new_id))
        session.add(
            ToolCall(
                id=new_id,
                run_id=source.run_id,
                subscription_task_id=source.subscription_task_id,
                subscription_attempt_id=attempt_id,
                subscription_purpose=source.subscription_purpose,
                tool_name=source.tool_name,
                normalized_arguments=source.normalized_arguments,
                arguments_schema_version=1,
                result_metadata_schema_version=1,
                result_metadata=metadata,
                status=status,
                authorized=True,
                started_at=source.started_at + timedelta(seconds=2),
            )
        )
    return new_id, attempt_id


async def test_mixed_history_preserves_failed_and_fresh_running_check_with_stable_pages(
    session_factory, tmp_path
):
    case, receipt, _, _ = await completed_case(session_factory, tmp_path)
    running, attempt = await add_running_attempt(session_factory, receipt.operation_id)
    async with session_factory() as session, session.begin():
        source = await session.get(ToolCall, receipt.operation_id)
        step = Step(run_id=case.context.run_id, kind="validate", attempt=3, status="FAILED")
        session.add(step)
        await session.flush()
        legacy = ValidationResult(
            run_id=case.context.run_id,
            step_id=step.id,
            check_name="legacy",
            command_name="unit",
            command_version=1,
            status="FAILED",
            exit_code=2,
            started_at=source.started_at + timedelta(seconds=1),
            completed_at=source.started_at + timedelta(seconds=2),
        )
        session.add(legacy)
        await session.flush()
        legacy_id = legacy.id
    pages = [await check_history(session_factory, case.context.run_id, n, 1) for n in range(4)]
    assert [page[0][0]["id"] for page in pages[:3]] == [receipt.operation_id, legacy_id, running]
    assert [page[1] for page in pages] == [True, True, False, False]
    assert pages[3][0] == []
    fresh = pages[2][0][0]
    assert fresh["status"] == "RUNNING" and fresh["exit_code"] is None
    assert fresh["subscription_attempt_id"] == attempt and fresh["attempt"] == 2
    assert fresh["evidence_digest"] is None and fresh["duration_ms"] is None
    overview = await DashboardQuery(session_factory).run_projection(case.context.run_id)
    assert [item["id"] for item in overview["checks"]] == [running, legacy_id, receipt.operation_id]


@pytest.mark.parametrize(
    "exit_code,cancelled,timed_out,status",
    [
        (0, False, False, "SUCCEEDED"),
        (1, False, False, "FAILED"),
        (None, False, True, "FAILED"),
        (0, True, False, "CANCELLED"),
    ],
)
async def test_receipt_outcome_is_not_operation_success(
    session_factory, tmp_path, exit_code, cancelled, timed_out, status
):
    case, receipt, _, _ = await completed_case(
        session_factory, tmp_path, exit_code=exit_code, cancelled=cancelled, timed_out=timed_out
    )
    async with session_factory() as session:
        assert (await session.get(OperationIntent, receipt.operation_id)).status == "SUCCEEDED"
    items, _ = await check_history(session_factory, case.context.run_id, 0, 25)
    assert items[0]["status"] == status and items[0]["exit_code"] == exit_code
    assert items[0]["evidence_digest"] and items[0]["stderr_artifact_digest"]


@pytest.mark.parametrize("corruption", ["receipt", "metadata", "artifact_edge", "interrupted"])
async def test_missing_or_inconsistent_terminal_evidence_never_passes(
    session_factory, tmp_path, corruption
):
    case, receipt, _, _ = await completed_case(session_factory, tmp_path, exit_code=0)
    async with session_factory() as session, session.begin():
        call = await session.get(ToolCall, receipt.operation_id)
        op = await session.get(OperationIntent, receipt.operation_id)
        if corruption == "receipt":
            op.outcome_payload = {**op.outcome_payload, "receipt_digest": "f" * 64}
            call.result_metadata = {**call.result_metadata, "receipt_digest": "f" * 64}
        elif corruption == "metadata":
            call.result_metadata = {**call.result_metadata, "exit_code": 2}
        elif corruption == "artifact_edge":
            await session.execute(
                delete(ArtifactLineageParent).where(ArtifactLineageParent.run_id == call.run_id)
            )
        else:
            await session.execute(
                update(OperationIntent)
                .where(OperationIntent.id == op.id)
                .values(
                    status="FAILED",
                    last_error="owner acknowledged interruption",
                    outcome_payload=null(),
                    outcome_schema_version=None,
                )
            )
            call.status = "CANCELLED"
            call.result_metadata = {
                key: value
                for key, value in call.result_metadata.items()
                if key
                in {
                    "operation_intent_id",
                    "correlation_id",
                    "resource_id",
                    "request_digest",
                    "policy_version",
                    "invocation_schema_version",
                }
            }
    fresh, _ = await add_running_attempt(session_factory, receipt.operation_id)
    items, _ = await check_history(session_factory, case.context.run_id, 0, 25)
    assert items[0]["status"] == "UNKNOWN" and items[0]["exit_code"] is None
    assert items[0]["duration_ms"] is None
    assert items[1]["id"] == fresh and items[1]["status"] == "RUNNING"


@pytest.mark.parametrize("corruption", ["authorization", "attempt", "run", "operation", "digest"])
async def test_inexact_subscription_authority_is_excluded(session_factory, tmp_path, corruption):
    case, receipt, _, _ = await completed_case(session_factory, tmp_path)
    fresh, _ = await add_running_attempt(session_factory, receipt.operation_id)
    async with session_factory() as session, session.begin():
        call = await session.get(ToolCall, receipt.operation_id)
        op = await session.get(OperationIntent, receipt.operation_id)
        if corruption == "authorization":
            call.authorized = False
        elif corruption == "operation":
            call.result_metadata = {**call.result_metadata, "operation_intent_id": str(uuid4())}
        elif corruption == "digest":
            op.request_digest = "f" * 64
        else:
            key = "run_id" if corruption == "run" else "subscription_attempt_id"
            op.request_payload = {**op.request_payload, key: str(uuid4())}
            op.request_digest = canonical_digest(op.request_payload)
    items, more = await check_history(session_factory, case.context.run_id, 0, 1)
    assert [item["id"] for item in items] == [fresh] and not more
