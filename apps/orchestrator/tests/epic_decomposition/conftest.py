"""Disposable PostgreSQL fixtures and explicit integration readiness."""

import inspect
from pathlib import Path
from typing import get_args

import pytest
import pytest_asyncio
from forge.api.app import create_app
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import AuthoringJobSnapshot
from forge.persistence.models.base import Base
from forge.persistence.models.epic_brainstorm import (  # noqa: F401
    BrainstormAttemptRow,
    BrainstormAuditRow,
    BrainstormBudgetLedger,
    BrainstormConversation,
    BrainstormJobRow,
    BrainstormQuotaAdmission,
    BrainstormReceiptRow,
    BrainstormTurnRow,
)
from sqlalchemy import text


def _shared_authoring_ready() -> bool:
    """The #73 lane executes only after its declared #72/#73 hooks are applied."""
    from forge.worker.main import run_worker

    return (
        "decomposition" in get_args(AuthoringJobSnapshot.model_fields["kind"].annotation)
        and "kind" in inspect.signature(EpicBrainstormService.submit).parameters
        and hasattr(EpicBrainstormService, "require_kind")
        and "epic_decomposition_budget" in inspect.signature(create_app).parameters
        and "decomposition_gateway_factory" in inspect.signature(run_worker).parameters
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _shared_authoring_ready():
        return
    for item in items:
        if item.path.parent == Path(__file__).parent and item.path.name != "test_domain.py":
            item.add_marker(pytest.mark.skip(reason="requires applied #72/#73 authoring hooks"))


@pytest_asyncio.fixture
async def decomposition_session_factory(session_factory):
    tables = [
        Base.metadata.tables[name]
        for name in (
            "epic_brainstorm_conversations",
            "epic_brainstorm_turns",
            "epic_brainstorm_jobs",
            "epic_brainstorm_attempts",
            "epic_brainstorm_receipts",
            "epic_brainstorm_audit",
            "epic_brainstorm_quota_admissions",
            "epic_brainstorm_budget_ledgers",
        )
    ]

    engine = session_factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    try:
        yield session_factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
            await connection.execute(
                text(
                    "TRUNCATE epic_graph_revisions, epic_brief_revisions, epics, api_mutations, operator_audit_events, projects CASCADE;"
                )
            )
