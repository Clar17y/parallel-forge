"""Provision only discovery-owned tables in a disposable migrated PostgreSQL DB."""

import pytest_asyncio
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


@pytest_asyncio.fixture
async def brainstorm_session_factory(session_factory):
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
    yield session_factory
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
