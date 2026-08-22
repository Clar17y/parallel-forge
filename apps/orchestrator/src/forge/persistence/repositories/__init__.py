"""PostgreSQL repository adapters."""

from forge.persistence.repositories.audit import AuditRepositoryError, PostgresAuditRepository
from forge.persistence.repositories.commands import (
    CommandError,
    CommandLeaseError,
    CommandNotFound,
    CommandStateConflict,
    PostgresCommandRepository,
)
from forge.persistence.repositories.mutations import (
    MutationConflict,
    MutationIncomplete,
    MutationNotFound,
    MutationRepositoryError,
    PostgresMutationRepository,
    hash_idempotency_key,
)
from forge.persistence.repositories.operations import (
    OperationError,
    OperationNotFound,
    OperationStateConflict,
    PostgresOperationRepository,
)
from forge.persistence.repositories.projects import (
    PolicyNotFound,
    PolicyVersionConflict,
    PostgresProjectRepository,
    ProjectIdentityConflict,
    ProjectNotFound,
    ProjectRepositoryError,
)
from forge.persistence.repositories.runs import (
    ConcurrencyConflict,
    PersistenceDataError,
    PersistenceError,
    PostgresRunRepository,
    RunCreationError,
    RunNotFound,
)
from forge.persistence.repositories.tasks import (
    PostgresTaskRepository,
    TaskIdentityConflict,
    TaskNotFound,
    TaskProjectNotFound,
    TaskRepositoryError,
    compute_task_digest,
    derive_normalized_text,
)

__all__ = [
    "AuditRepositoryError",
    "CommandError",
    "CommandLeaseError",
    "CommandNotFound",
    "CommandStateConflict",
    "ConcurrencyConflict",
    "MutationConflict",
    "MutationIncomplete",
    "MutationNotFound",
    "MutationRepositoryError",
    "OperationError",
    "OperationNotFound",
    "OperationStateConflict",
    "PersistenceDataError",
    "PersistenceError",
    "PolicyNotFound",
    "PolicyVersionConflict",
    "PostgresAuditRepository",
    "PostgresCommandRepository",
    "PostgresMutationRepository",
    "PostgresOperationRepository",
    "PostgresProjectRepository",
    "PostgresRunRepository",
    "PostgresTaskRepository",
    "ProjectIdentityConflict",
    "ProjectNotFound",
    "ProjectRepositoryError",
    "RunCreationError",
    "RunNotFound",
    "TaskIdentityConflict",
    "TaskNotFound",
    "TaskProjectNotFound",
    "TaskRepositoryError",
    "compute_task_digest",
    "derive_normalized_text",
    "hash_idempotency_key",
]
