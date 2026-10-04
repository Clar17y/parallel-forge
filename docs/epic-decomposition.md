# Epic decomposition integration

## Overview

Issue #73 introduces an assisted decomposition lane for epics in Forge. The decomposition capability translates an accepted, immutable epic brief into a bounded, editable directed acyclic graph (DAG) of work items, accompanied by stale-safe atomic adoption into Forge's durable PostgreSQL state.

## Core Architectural Boundaries

1. **Brief-Bound Validation**:
   - Proposals are strictly bound to an `accepted_brief_revision_id` and its immutable `accepted_brief_digest`.
   - Every work item requirement reference (`source_requirement_ids`) must exist in the accepted brief's frozen requirements.
   - Any open questions or assumptions in the brief are preserved visibly. The model cannot silently resolve pending operator choices.

2. **Work Item Graph Invariants**:
   - Work items are bounded (maximum 128 items per proposal, byte limits on text and criteria).
   - Graph validation rejects cycles, self-dependencies, duplicate IDs, and dangling dependency edges.
   - At adoption time, active (required) items cannot depend on deferred nodes.
   - Operators can freely edit, split, combine, reorder, or defer proposed items prior to explicit adoption.

3. **Shared Authoring Reuse**:
   - Decomposition reuses the durable authoring engine established in `#72` (`epic_brainstorm_*` tables, lease management, budget/quota settlement, fail-closed process supervision).
   - `kind="decomposition"` snapshots route through the shared authoring tables and ports without duplicating the ~1,000 lines of quota/lease/process supervision code.

4. **Stale-Safe Atomic Adoption**:
   - Adoption is authenticated, CSRF-protected, idempotent, and audited.
   - The adoption transaction locks `epics` `FOR UPDATE`, then locks `epic_brainstorm_jobs` `FOR UPDATE`.
   - It verifies:
     - Project ID matches epic's project.
     - Epic version matches both the caller and the original frozen job snapshot.
     - Current draft digest, accepted brief ID and digest, and graph selection match the job snapshot.
     - Job state is `proposed`, its current attempt is settled with real process proof, and the stored payload recomputes to its recorded digest.
     - No operator turn was appended after the frozen prompt, and the exact assistant proposal turn is present.
     - The proposal has not already been adopted.
   - In a single PostgreSQL transaction:
     - Saves a new immutable `EpicGraphRevision` and links it as the epic's current graph revision.
     - Increments the epic's version for saving and selecting the revision.
     - Keeps the authoring job `proposed` and records `adopted_revision_id`.
     - Inserts an idempotency mutation receipt in `api_mutations`.
     - Emits an `operator_audit_events` audit record with the adopted graph identity.
   - Replay with the same idempotency key returns the original receipt; an altered payload triggers a 409 conflict.
   - Concurrent stale jobs or human modifications fail-closed without partial writes.

5. **Child Planning Boundary**:
   - Decomposition does NOT create delivery runs, run tasks, or approvals.
   - It does NOT call run planners or grant delivery execution authority.
   - Child planning occurs solely via the existing planner after ready child admission (`#71`/`#74`).

---

## Domain & Persistence Models

### Domain (`forge.domain.epic_decomposition`)
- `DecompositionProposal`: Closed Pydantic model (`schema_version=1`) with bounded items, brief references, and evidence.
- `DecompositionEvidence`: Bounded file path, content digest, and excerpt references.
- `validate_decomposition_proposal`: Domain validation function checking graph acyclicity, item criteria, and requirement reference integrity.

### Repositories (`forge.persistence.repositories.epic_decomposition`)
- `PostgresEpicDecompositionJobRepository`: Locks and validates the original job, attempt, and conversation before recording `adopted_revision_id`.
- `PostgresEpicDecompositionUnitOfWork`: Coordinates the atomic adoption transaction across `PostgresEpicBriefRepository`, `PostgresEpicItemsRepository`, `PostgresMutationRepository`, `PostgresAuditRepository`, and the jobs repository.

### Application Services (`forge.application.services.epic_decomposition`)
- `EpicDecompositionService`: Exposes operations for starting conversations, appending turns, submitting jobs, and atomic adoption.

### API Routes (`forge.api.routes.epic_decomposition`)
- `POST /epics/{id}/decomposition-conversations`: Start conversation.
- `POST /epics/{id}/decomposition-conversations/{cid}/turns`: Append turn.
- `POST /epics/{id}/decomposition-conversations/{cid}/jobs`: Submit decomposition job.
- `GET /epics/{id}/decomposition-jobs/{jid}`: Observe job status and proposal.
- `POST /epics/{id}/decomposition-jobs/{jid}/cancel`: Request cancellation.
- `POST /epics/{id}/decomposition-jobs/{jid}/retry`: Retry failed/interrupted job.
- `POST /epics/{id}/decomposition-jobs/{jid}/adopt`: Stale-safe atomic adoption.

---

## Integration Proposals

Shared integration is delivered as patches in `apps/orchestrator/tests/epic_decomposition/integration_proposals/`. From the active `a08ec70` baseline, apply all #72 prerequisites in their manifest order: `epic_brief_source.patch`, `registration.patch`, `epic_brainstorm_migration.patch`, then `generated_contracts.patch`. The #72 Alembic `20261004_0033` migration follows `20261003_0032`; recheck the active head when integrating. Then apply the three #73 patches below in manifest order. The authoring API requires an explicit `TaskBudget`; the worker requires a configured subject gateway and controlled repository reader factory. Live provider registration remains an integration-owned setting.

The normal baseline test collection runs six pure-domain cases and explicitly skips the 35 integration cases until the declared shared hooks are present. To exercise the entire lane before integration, apply the ordered patches in a disposable source overlay and run from the worktree root with `PYTHONPATH=<overlay>/apps/orchestrator/src python -m pytest apps/orchestrator/tests/epic_decomposition -q -rs`. The composed lane must run all 41 cases without readiness skips.

1. `shared_authoring_compatibility.patch`:
   - Enables `AuthoringJobSnapshot` to accept `kind: Literal["brainstorm", "decomposition"]`.
   - Extends `AuthoringOutcome.proposal` and `BrainstormTurn.proposal` to accept `DecompositionProposal`.
   - Updates `BoundEpicAuthoringAdapter.submit` to allow decomposition job kinds.
   - Dispatches and parses proposals by frozen job kind, validates actual gateway output, and limits claims to configured kinds.

2. `registration.patch`:
   - Adds decomposition service and router registration to `forge.api.app.create_app`.
   - Composes the real decomposition unit of work and shared authoring service with an explicit budget.
   - Wires decomposition gateway dispatch, polling, and drain through the shared authoring worker.

3. `generated_contracts.patch`:
   - Updates `apps/web/openapi.json` and `apps/web/src/lib/api/schema.d.ts` for the registered decomposition routes.

4. `manifest.json`:
   - Lists baseline commit `a08ec70c367aeba1b4ef23598a0a630d1d8e5f8e`, full #72 prerequisite order and hashes, then the #73 application order, SHA-256 checksums, and byte lengths.
