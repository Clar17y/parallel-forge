"""Persistence model definitions and bindings for epic decomposition."""

from __future__ import annotations

from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormAuditRow,
    BrainstormBudgetLedger,
    BrainstormConversation,
    BrainstormJobRow,
    BrainstormQuotaAdmission,
    BrainstormReceiptRow,
    BrainstormTurnRow,
)
from forge.persistence.models.epic_brief import Epic, EpicBriefRevision
from forge.persistence.models.epic_items import EpicGraphRevision

# Type aliases reflecting decomposition usage
DecompositionJobRow = BrainstormJobRow
DecompositionConversationRow = BrainstormConversation
DecompositionTurnRow = BrainstormTurnRow

__all__ = [
    "BrainstormAttemptRow",
    "BrainstormAuditRow",
    "BrainstormBudgetLedger",
    "BrainstormConversation",
    "BrainstormJobRow",
    "BrainstormQuotaAdmission",
    "BrainstormReceiptRow",
    "BrainstormTurnRow",
    "DecompositionConversationRow",
    "DecompositionJobRow",
    "DecompositionTurnRow",
    "Epic",
    "EpicBriefRevision",
    "EpicGraphRevision",
]
