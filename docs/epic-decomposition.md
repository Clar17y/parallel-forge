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

## Production Integration Status

Assisted decomposition (#73) is integrated into Forge's production runtime alongside brainstorming (#72) and the epic run bridge (#71):

1. **Shared Authoring Runtime**:
   - `AuthoringJobSnapshot` supports `kind="decomposition"` alongside `kind="brainstorm"`.
   - `AuthoringOutcome.proposal` and `BrainstormTurn.proposal` accept `DecompositionProposal`.
   - `BoundEpicAuthoringAdapter.submit` handles decomposition job kinds.
   - Authoring repository and worker filter claims to configured kinds and preserve fail-closed process supervision, lease tracking, and budget accounting.

2. **API & Worker Composition**:
   - `forge.api.app.create_app` composes `epic_decomposition_budget` and `epic_decomposition_service` with `PostgresEpicDecompositionUnitOfWork` and mounts the `/api/epics/{id}/decomposition-*` router.
   - `forge.worker.main.run_worker` accepts `decomposition_gateway_factory`, configuring both authoring kinds with shared worker polling, drain handling, and cancellation.

3. **Supervised Local CLI Provider Protocols**:
   - `epic_brainstorm_protocol.py` dynamically selects schema and prompt actions based on `job.kind` (`brainstorm` vs `decomposition`), preserving brief and project binding.
   - Provider exchange functions (`codex_exchange`, `claude_exchange`, `gemini_exchange`, `antigravity_exchange`) and `epic_brainstorm_gateway.py` wire decomposition schemas and system prompts across all four supported local CLI providers.
   - Output validation enforces schema integrity, brief digest matching, requirement ID containment, and turn ID matching before any proposal becomes adoptable.

4. **Discoverability & Web Navigation**:
   - Epics navigation link added under `Operate -> Epics` (`/epics`) in `apps/web/src/components/layout/navigation.tsx`.
   - Project detail page (`/projects/[projectId]`) includes an entry point to the project's epics list (`/epics?project_id=...`).
   - OpenAPI specifications and generated TypeScript bindings (`apps/web/openapi.json`, `apps/web/src/lib/api/schema.d.ts`) reflect all decomposition routes and schema models with zero drift.

5. **Verification & Smoke Testing**:
   - Comprehensive test suite in `apps/orchestrator/tests/epic_decomposition`: 56 unit/integration tests running with zero skips.
   - Dedicated protocol suite (`test_protocols.py`): validates schema selection, prompt generation, proposal parsing, turn matching, and provider exchanges for Codex, Claude, Gemini, and Antigravity.
   - Providerless production smoke test (`test_smoke.py`): exercises complete flow from project creation, brief adoption, decomposition submission, supervised worker execution, proposal observation, atomic graph adoption, to child item launch via `EpicRunBridgeService`, asserting ordinary `Task`, `Run`, and `start_planning` `RunCommand` creation with human approval gates intact.
