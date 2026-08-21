# ADR-002: Explicit persisted orchestration state machine

Status: Accepted  
Date: 2026-08-21

## Context

Forge workflows span model calls, filesystem changes, builds, human decisions,
GitHub state, failures, and process restarts. An open-ended LLM conversation
cannot reliably establish which actions are legal, which evidence was
approved, or how to recover after interruption.

## Decision

Forge represents workflow state explicitly in Domain and persists the current
state and optimistic version in PostgreSQL. Legal transitions are validated by
deterministic application code.

The primary states are:

- CREATED
- PLANNING
- AWAITING_PLAN_APPROVAL
- PREPARING_WORKTREE
- IMPLEMENTING
- VALIDATING
- REVIEWING
- REMEDIATING
- AWAITING_PR_APPROVAL
- PUBLISHING_PR
- MONITORING_PR
- AWAITING_HUMAN_INTERVENTION
- AWAITING_MERGE_APPROVAL
- MERGING
- PAUSED
- COMPLETED
- FAILED
- CANCELLED

Typed durable commands request transitions. One worker lease advances a run at
a time. State updates and append-only run events are committed together where
possible. Agent output and tool results are inputs to transition decisions;
they never transition the workflow directly.

Approvals bind the state version and exact evidence. External operations are
idempotent and reconciled after restart before the run advances.

## Consequences

Positive:

- legal operations are clear and testable;
- approval gates cannot be bypassed through conversation;
- the dashboard can show authoritative current and historical state;
- restart, duplicate delivery, pause, and cancellation have defined behavior;
- audit and evaluation data share stable run/step identities.

Negative:

- new workflow behavior requires explicit schema and transition changes;
- external-state reconciliation must be designed for each side effect;
- state migration needs care as the product evolves.

## Alternatives considered

### Agent-directed conversational loop

Rejected because the agent would implicitly control authority, recovery, and
termination, making safety and reproducibility weak.

### Full event sourcing

Rejected for v0.1 because it adds projection and migration complexity without a
demonstrated need. Forge uses current-state records plus an append-only audit
event log.

### In-memory workflow with logs

Rejected because the approved product requires durable progress, approvals,
auditability, and restart recovery from the beginning.
