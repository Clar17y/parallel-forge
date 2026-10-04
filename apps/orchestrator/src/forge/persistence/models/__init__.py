"""Complete v0.1 SQLAlchemy model inventory."""

from sqlalchemy import ForeignKeyConstraint

from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
from forge.persistence.models.auth import ApprovalChallenge, OperatorSession
from forge.persistence.models.base import Base
from forge.persistence.models.capability_evidence import CapabilityEvidence
from forge.persistence.models.capability_probe_diagnostics import CapabilityProbeDiagnosticRecord
from forge.persistence.models.epic_brief import Epic, EpicBriefRevision
from forge.persistence.models.epic_items import EpicGraphRevision
from forge.persistence.models.evaluation import EvaluationCase, EvaluationSuite
from forge.persistence.models.execution import (
    AgentExecution,
    AgentExecutionEvidenceInput,
    Approval,
    Artifact,
    ArtifactLineage,
    ArtifactLineageParent,
    EvidenceSet,
    ModelUsage,
    OperationIntent,
    Review,
    RunCommand,
    RunEvent,
    Step,
    ToolCall,
    ValidationResult,
)
from forge.persistence.models.jev import JevEvaluation
from forge.persistence.models.project import Project, ProjectPolicyVersion, Task
from forge.persistence.models.recovery import RecoveryBarrier
from forge.persistence.models.release import PullRequest
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerCapacityPolicy,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    ProjectSubscriptionProfile,
    SubscriptionAttempt,
    SubscriptionBudgetPool,
    SubscriptionBudgetReservation,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionOperationBinding,
    SubscriptionProfileVersion,
    SubscriptionTask,
    SubscriptionTaskDependency,
)
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.models.subscription_plan_gate import SubscriptionPlanGate
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaAdmission,
    SubscriptionQuotaObservation,
    SubscriptionQuotaPool,
)
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionContractRevision,
    SubscriptionRecoveryReceipt,
    SubscriptionRecoverySigningKey,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_runtime_status import SubscriptionWorkerStatus
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)

# Register the cross-lane selection binding at the shared integration boundary.
# The brief module remains independent of the graph implementation.
Base.metadata.tables["epics"].append_constraint(
    ForeignKeyConstraint(
        ("id", "accepted_graph_revision_id", "accepted_graph_digest"),
        (
            "epic_graph_revisions.epic_id",
            "epic_graph_revisions.id",
            "epic_graph_revisions.graph_digest",
        ),
        name="fk_epics_accepted_graph",
        ondelete="RESTRICT",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

__all__ = [
    "AgentExecution",
    "AgentExecutionEvidenceInput",
    "ApiMutation",
    "Approval",
    "ApprovalChallenge",
    "Artifact",
    "ArtifactLineage",
    "ArtifactLineageParent",
    "Base",
    "CapabilityEvidence",
    "CapabilityProbeDiagnosticRecord",
    "Epic",
    "EpicBriefRevision",
    "EpicGraphRevision",
    "EvaluationCase",
    "EvaluationSuite",
    "EvidenceSet",
    "JevEvaluation",
    "ModelUsage",
    "OperationIntent",
    "OperatorAuditEvent",
    "OperatorSession",
    "Project",
    "ProjectPolicyVersion",
    "ProjectSubscriptionProfile",
    "PullRequest",
    "RecoveryBarrier",
    "Review",
    "Run",
    "RunCommand",
    "RunEvent",
    "Step",
    "SubscriptionApplicationDiagnostic",
    "SubscriptionAttempt",
    "SubscriptionAttemptConsumption",
    "SubscriptionAttemptReservation",
    "SubscriptionAttemptResult",
    "SubscriptionBudgetPool",
    "SubscriptionBudgetReservation",
    "SubscriptionClientLaunch",
    "SubscriptionContractRevision",
    "SubscriptionDecisionRecord",
    "SubscriptionEnvelope",
    "SubscriptionHandoffFence",
    "SubscriptionOperationBinding",
    "SubscriptionPlanGate",
    "SubscriptionProfileVersion",
    "SubscriptionQuotaAdmission",
    "SubscriptionQuotaObservation",
    "SubscriptionQuotaPool",
    "SubscriptionRecoveryReceipt",
    "SubscriptionRecoverySigningKey",
    "SubscriptionRecoveryWorker",
    "SubscriptionRepairDebit",
    "SubscriptionScheduledEffect",
    "SubscriptionScheduledTask",
    "SubscriptionSchedulerCapacityPolicy",
    "SubscriptionSchedulerRun",
    "SubscriptionTask",
    "SubscriptionTaskDependency",
    "SubscriptionTaskFeedback",
    "SubscriptionTaskStop",
    "SubscriptionWorkerStatus",
    "Task",
    "ToolCall",
    "ValidationResult",
]
