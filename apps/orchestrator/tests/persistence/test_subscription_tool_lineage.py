"""Subscription lineage remains immutable across every tool-call replay API."""

from dataclasses import replace
from uuid import uuid4

import pytest
import test_subscription_authority_persistence as actual
from forge.application.ports.tools import ToolCallRecord
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import ToolCallStatus, ToolName
from forge.persistence.repositories.tool_calls import (
    PostgresToolCallRepository,
    ToolCallConflict,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401


def _changed_lineage(record: ToolCallRecord, field: str) -> ToolCallRecord:
    value = (
        SpecialistPurpose.PRIMARY.value
        if field == "subscription_purpose"
        else uuid4()
    )
    return replace(record, **{field: value})


@pytest.mark.integration
@pytest.mark.parametrize(
    "field",
    ["subscription_task_id", "subscription_attempt_id", "subscription_purpose"],
)
async def test_public_replays_reject_changed_subscription_lineage_and_preserve_original(
    session_factory, tmp_path, monkeypatch, field
):
    seeded = []
    seed = actual._seed_test_database

    async def capture_seed(*args, **kwargs):
        result = await seed(*args, **kwargs)
        seeded.append(result)
        return result

    monkeypatch.setattr(actual, "_seed_test_database", capture_seed)
    await actual.test_subscription_effect_uses_actual_lineage_and_recovers_without_legacy_execution(
        session_factory,
        tmp_path,
        monkeypatch,
        ToolName.REPOSITORY_READ_FILE,
        False,
    )
    run_id = seeded[0][1]
    async with session_factory() as session, session.begin():
        repository = PostgresToolCallRepository(session)
        terminal = (await repository.list_for_run(run_id))[0]

        reservation = replace(
            terminal,
            id=uuid4(),
            status=ToolCallStatus.RUNNING,
            completed_at=None,
            result_metadata=None,
            result_metadata_schema_version=None,
            duration_ms=None,
            artifact_digests=(),
            correlation_id=None,
            operation_intent_id=None,
        )
        original_reservation = await repository.reserve(reservation)
        changed_terminal = _changed_lineage(terminal, field)
        conflicts = []
        for name, replay in (
            ("reserve", lambda: repository.reserve(_changed_lineage(reservation, field))),
            ("record", lambda: repository.record(changed_terminal)),
            ("finalize", lambda: repository.finalize(changed_terminal)),
        ):
            try:
                await replay()
            except ToolCallConflict:
                conflicts.append(name)
        assert conflicts == ["reserve", "record", "finalize"]
        session.expire_all()
        assert await repository.get(reservation.id) == original_reservation
        assert await repository.get(terminal.id) == terminal
