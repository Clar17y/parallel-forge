# Subscription workflow correctness and operator recovery

- Date: 2026-09-28
- Status: implemented locally; affected validation and review repairs complete;
  no deployment or live incident recovery performed
- Reference commit: `ace0d9f8c12609c0237cbcd7e4d910fe81b065f3`
- Planning branch: `codex/workflow-recovery`

## Outcome

A provider result must lead to progress, a bounded corrective attempt, or an
inspectable blocked state. It must not leave a healthy worker silently retrying
an impossible transition while the dashboard appears to show an active tool.

Provide a supported operator recovery path with a reviewable preview, named
actions, and durable receipts. Reuse Forge's PostgreSQL state, controlled tools,
physical-stop evidence, budgets, and approval gates. This work does not introduce
a new workflow engine or a general database/state editor.

The work has three deliverables: prevent the observed failure, expose failed
result processing, and allow guarded operator recovery. Deliver them in that
order. After rollout and authorization for the concrete action, recover the
incident run through the completed recovery service.

## Incident and confirmed cause

Historical observations for run `b4d46ec8-ff5a-4017-b7e3-0357dbb2154b`:

- `git.diff` succeeded at 15:58:23 UTC on 28 September 2026 in 8.916 seconds.
- The provider stopped and Forge retained its result at 15:58:52 UTC.
- The task and attempt were `reconciling`; the saved result was
  `decision_pending`, `accepted=false`; the run remained `IMPLEMENTING`.
- The primary returned a completed `TaskHandoff` containing another plan, with
  no changed paths or checks. Its task still had the `approved-plan` criterion:
  "Produce an evidence-bound plan for the requested task."
- The worker was reporting a heartbeat and no active database lock was observed.

These observations are evidence for the regression fixture, not assertions about
the run's state when implementation or recovery starts.

The owning components are:

| Component, under `apps/orchestrator/src/forge/` | Gap |
| --- | --- |
| `application/services/subscription_planning.py` | Creates a planning-only primary acceptance criterion. |
| `persistence/repositories/subscription_plan_gate.py`, `resume_prepared` | Updates paths and checks but retains that criterion. |
| `application/services/subscription_requests.py` | Builds the next invocation from the inconsistent prepared contract. |
| `agents/subscription_protocol.py` | Offers and decodes primary handoffs and scope requests that the application layer forbids. |
| `persistence/repositories/subscription_execution.py` | Can save such a result as pending application. |
| `persistence/repositories/subscription_decisions.py` | Correctly rejects worker handoffs from a primary or parentless task. |
| `application/services/subscription_decision_recovery.py`, `worker/main.py` | Repeated failure is deferred without an actionable per-attempt explanation. |

Ordinary pause/resume preserves a pending result for guarded application; it is
not a correction mechanism. Restarting the worker also cannot correct the saved
contract/result disagreement.

## Design decisions

### 1. Make the approved implementation contract explicit

During guarded worktree preparation, atomically replace the current task's
planning objective with an implementation objective derived from the approved
plan. Preserve the primary task identity, frozen route, cumulative usage and
budget, and existing task-version checks.

The implementation contract must bind the approved plan artifact/digest,
producing attempt, approval receipt, approved paths, and required checks. Its
acceptance criterion is to implement the approved outcomes and supply the
required validation evidence. Put the approved plan content and reference into
the invocation context so the agent does not have to reconstruct the objective
from historical tool activity. Plan text remains task context, not authority to
change routes, tools, paths, budgets, or approval rules.

Keep immutable history: retain planning attempts and their original contract
payloads. Record the implementation contract revision and its provenance before
queuing the next attempt. Replay of preparation must recognize the same revision
and must not duplicate it. Do not invent writable paths or missing approval for
a legacy/unscoped plan; expose an explicit intervention reason when a supported
implementation contract cannot be derived.

### 2. Share decision legality across boundaries

Add a small Forge-owned rule for decisions allowed by role and run phase. Use it
in output-schema construction, decoding, settlement, and retained-result
classification. Keep the existing evidence and target-identity checks in the
application layer; structural legality is not proof of acceptance.

Primary `handoff` and `scope_request` are forbidden. The primary continues
through the existing delegation, wait, scope-response, reassignment, feedback,
review-selection and acceptance paths as applicable. Direct primary work still
uses the existing candidate and validation path. Workers retain their controlled
handoff and scope-request paths. Preserve the newer Codex response transport,
including decision-kind selection, `kind` ordering, and named-check constraints.

Reject new invalid responses at the protocol boundary. Also validate settlement
so fake gateways, other adapters, and already persisted results cannot bypass the
rule. Preserve the original result or bounded failure evidence and its digest;
record rejection separately. Never mark an invalid result accepted to release a
task. Check adjacent illegal role/phase/target combinations in the same batch.

Use existing bounded protocol correction where available. A fresh corrective
provider attempt follows the existing attempt, repair and quota accounting. A
repeated identical role/phase violation must not create an infinite cycle: allow
at most one automatic corrective attempt for that violation on the same contract
revision, within the remaining limits, then require operator attention. Record
the feedback supplied to the correction and link it to the rejected attempt.

### 3. Make result-processing failures durable and actionable

Store diagnostics separately from immutable provider/proposal payloads. Maintain
a current per-attempt projection backed by causal application/recovery events:
reason code, classification, first/last failure time, failed application count,
next retry time, and current resolution. Use bounded, redacted messages from
known reason codes. Raw exception/provider text must not enter dashboard fields.

| Classification | Behavior |
| --- | --- |
| Temporary infrastructure failure | Retry the saved application with persisted backoff; no provider call. Proposed default: three retries after 5, 15 and 60 seconds, then attention required. |
| Invalid decision | Record rejection; use the bounded correction policy if stopped, current and eligible; otherwise expose the reason. |
| Valid decision waiting for a prerequisite | Show the actual prerequisite and retry when its relevant state changes. Do not treat ordinary waiting as repeated failure. |
| Uncertain provider/tool/external effect | Retain the existing ownership fences and require reconciliation evidence. |
| Unsupported application or invariant failure | Expose operator attention immediately; do not keep retrying the same unchanged source. |

Reuse existing run/task intervention states where they fit. A blocked primary
must become visible at run level. A failed child should report its outcome to
the primary without unnecessarily stopping independent work. Define how resume
and cancellation interact with the new diagnostic projection.

Worker liveness, last successful tool activity, and workflow progress are
different facts. Neither elapsed time nor an expired lease proves that a process
or effect has stopped. Persist retry scheduling so restart cannot reset the
allowance or repeatedly apply the same failure. If the database is unavailable,
emit a bounded safe operational error and do not claim diagnostics were saved.

### 4. Add named, previewed operator recovery

One application service serves the dashboard, HTTP and local CLI. Use the
existing authenticated operator role and audited mutation receipts. Recovery
does not grant release, merge, budget expansion, or new project authority.

| Proposed action | Preconditions and effect |
| --- | --- |
| `retry_application` | Reprocess the exact saved result after its prerequisite or relevant implementation changed. Apply normal evidence guards. Spend no provider attempt or repair unit. A known unchanged invalid result is ineligible. |
| `reject_and_retry_step` | Prove the result unusable and the old execution/effects stopped; retain rejection evidence and schedule one fresh attempt under the current valid contract and remaining limits. |
| `repair_approved_plan_contract` | Match the known planning-objective defect, verify its original approval, record a deterministic implementation-contract revision and rejection of the incompatible result, then queue the bounded retry if eligible. |
| Cancel | Reuse existing run/task cancellation and preservation behavior. |

Use a side-effect-free preview request to select an action and explain its exact
changes, retained work, prerequisites, and budget impact. The preview returns a
short-lived binding over run/task/attempt identities and versions, result digest,
contract revision, action/version, relevant candidate/worktree identity, and
approval/evidence references. A preview starts no provider, grants no lease, and
does not reserve execution capacity. An unforgeable binding or authoritative
server recomputation is required; client-supplied preview claims are not proof.

Apply requires a reason and idempotency key, revalidates under the existing lock
order, and commits one recovery receipt with the transition. Repeated requests
return the same receipt. Reusing a key with different content fails. A changed
source produces a conflict and requires a fresh preview. Unknown HTTP outcomes
retry the same request/key rather than creating another recovery.

For the primary, reuse run-control stopping semantics; existing specialist task
controls intentionally exclude it. Stop proof includes provider identity,
settled usage, and controlled effects, not just a terminal-looking task row.
Pause/cancel takes precedence. A retry records all real usage and follows quota
admission; manual recovery never clears quota or silently replenishes budgets.
Charge a fresh-attempt repair exactly once through the existing accounting
transition, and explicitly test crash/replay at that boundary.

If candidate, scope, policy, or approval evidence changed, refuse the old preview
and require the applicable existing approval/revalidation path. Do not discard
partial edits, alter historical evidence, clear leases, run arbitrary commands,
or convert an unknown side effect into a claimed success. New classes of
workflow defect can add a named repair handler to this service.

Proposed surfaces, to finalize with the shared service contract:

- An attempt recovery status projection on the existing task inspection API.
- `POST /runs/{run_id}/subscription-tasks/{task_id}/attempts/{attempt_id}/recovery/preview`
  for a structured, read-only preview request.
- `POST /runs/{run_id}/subscription-tasks/{task_id}/attempts/{attempt_id}/recovery`
  to apply the selected preview.
- `recovery-preview` and `recover` under the existing subscription task CLI app.
- A **Recover run** entry for the primary and contextual recovery in task
  inspection, showing preview before apply. Existing cancellation stays available.

These names are proposed additions, not commands or endpoints available today.

## Implementation tasks and ownership

Each behavioral task follows focused red-green-refactor: add one discriminating
test, observe the expected failure, implement the smallest complete behavior,
rerun it, then run the affected suite. All writes belong to an explicitly managed
worktree. Owners keep responsibility for related repairs and self-review.

### Task 1: Reproduce the complete subscription transition

**Owner:** backend owner. **Dependencies:** reconcile implementation base first.

Use the current operational reference above, then reconcile any newer integrated
changes before implementation. The unrelated operational modification to
`persistence/queries/dashboard.py` must remain with its owner. This plan's
worktree was aligned to `ace0d9f8` without copying that modification.

Add a regression in the subscription runtime harness covering planning output,
operator approval, managed preparation, generated implementation request, actual
incident-shaped primary output, durable settlement and recovery. Capture both
the stale objective and schema/application disagreement. Use a fake official
client/gateway and isolated PostgreSQL; no live provider or production control
database is needed. Do not rely solely on handcrafted persistence decisions or
the legacy planner harness.

**Acceptance:** the current behavior fails for the expected cause; the fixture
retains planning provenance and can be reused through Tasks 2-4.

### Task 2: Fix contract preparation and decision legality

**Owner:** the same backend owner. **Dependencies:** Task 1.

Own the affected domain contracts, `subscription_planning.py`,
`subscription_requests.py`, `subscription_plan_gate.py`,
`subscription_protocol.py`, `subscription_execution.py`, and
`subscription_decisions.py`, plus their focused tests. A proposed
`domain/subscription_decision_policy.py` can hold the small shared legality rule.

Implement the contract revision and approved-plan binding, schema/decoder checks,
and settlement classification described above. Document the transition and its
replay behavior. Do not loosen worker handoff guards. Preserve all prior attempt
payloads and the modern official-client response transport.

**Acceptance:** implementation requests have implementation outcomes; primary
handoffs/scope requests are absent from the schema and rejected at every entry
boundary; valid primary and worker flows still work; replay changes nothing twice.

### Task 3: Add durable failure handling and bounded corrections

**Owner:** the same backend owner. **Dependencies:** Task 2.

Own `application/services/subscription_decision_recovery.py`,
`worker/subscription_invocation.py`, `worker/main.py`, relevant ports/repositories
and unit-of-work wiring, plus an additive migration, diagnostic/revision models,
queries and tests. Proposed new files should follow existing subscription model
and repository conventions. Allocate the migration number at implementation
time under `apps/orchestrator/migrations/versions/`; the last inspected migration
was `20260920_0024_capability_probe_diagnostics.py`.

Classify both new and retained invalid decisions, persist scheduling and safe
reason codes, and implement bounded correction with exact accounting. Keep
pending prerequisites and uncertain effects distinct from permanent errors.
The incident result must stop cycling and become correction-eligible or visibly
blocked, even after restart.

**Acceptance:** no unchanged impossible decision retries forever; every deferred
result has an explainable status; unknown effects retain their fences; usage,
repair debits and rejection receipts survive interruption without duplication.

### Task 4: Build the recovery service and operator adapters

**Owner:** backend owner for state transitions; adapter owner after interface
handoff. **Dependencies:** Task 3.

The backend owner retains the recovery repository/service and all overlapping
settlement/accounting files through preview, apply and crash/replay tests. Use a
proposed `application/services/subscription_recovery.py` with a corresponding
typed port/domain contract rather than duplicating HTTP and CLI logic.

After those interfaces stabilize, one adapter owner takes
`api/routes/subscription_tasks.py`, `api/schemas/subscription_tasks.py`,
`api/app.py`, `cli/subscription_tasks.py`, API/CLI tests and generated OpenAPI
types in `apps/web/src/lib/api/schema.d.ts`. Register proposed routes/commands,
reuse authentication/CSRF and idempotency handling, and expose consistent safe
conflict/blocked explanations. Shared registration files have one owner.

**Acceptance:** preview causes no execution; apply is authenticated, causally
bound and once-only; stale previews and reused keys with different bodies fail;
all three named actions enforce the same rules through HTTP and CLI.

### Task 5: Display recovery status and usable controls

**Owner:** UI owner after adapter handoff. **Dependencies:** Tasks 3-4.

Own `apps/web/src/components/subscription-tasks/task-controls.tsx`,
`task-inspector.tsx`, their tests, and a proposed run recovery panel. Keep
`persistence/queries/subscription_tasks.py` with the backend owner; the UI consumes
its stable API contract. Generated API types stay with the adapter owner until
an explicit handoff.

Show current execution/result-processing state separately from last completed
tool activity. Display why attention is required, what each eligible action does,
and why an action is unavailable. Require a reviewable preview and preserve the
same idempotency key on uncertain responses. Refresh on conflict. Include keyboard,
loading, error, long-text and narrow-screen behavior.

Coordinate with the existing `useful-activity` work before touching its files:
`tool_recovery.py`, `tools.py`, activity event tests, `globals.css`,
`audit-event-card.tsx`, `phase-timeline.tsx`, `relative-time.tsx`, and
`lib/activity-presentation.ts`, including their tests. Feed recovery events and
projection fields into that presentation; do not recreate its generic timeline.
Transfer ownership explicitly if integration requires edits to those paths.

**Acceptance:** the incident appears as an explanation of failed result handling,
not as an indefinitely running diff. UI/API/CLI agree about action eligibility.

### Task 6: Integrate, verify and roll out

**Owner:** primary for integration/acceptance; existing owners for repairs.
**Dependencies:** Tasks 1-5.

Review the coherent candidate once independently because this work changes
concurrency, durable state and recovery authority. Use the installed Claude-first
review route and prescribed Astra fallback; check local availability before
dispatch. Batch substantiated related repairs with their existing owner and rerun
affected checks. Add another gate only for a named unresolved concern.

Use the installed routing policy for implementation: the coupled backend outcome
fits `complex_implementer` (Sol medium); routine adapter/UI work checks Gemini
first and uses the authorized Luna fallback when required. No overlapping
writers or nested delegation. Reuse matching validation evidence rather than
rerunning everything after documentation changes.

Deploy additive storage/read support before enabling recovery mutations. A new
API must refuse actions unless the serving worker supports that recovery and
contract version. Stop incompatible old workers using normal lifecycle handling;
do not allow mixed-version writers to reinterpret new contracts. Test retained
old rows and migrate only schemas/projections automatically, never an unselected
run's contract. Known legacy defects require a previewed repair.

Rollback disables new mutations and stops incompatible execution while retaining
new receipts, contract revisions and diagnostics. Do not roll back a database
schema or worker version that cannot read already committed recovery state.

Full CI dispatches/reruns, including PR-triggered full CI, each need explicit
authorization for that run. Merge authorization remains separate and immediate.
The plan does not authorize deployment, provider execution or live-run mutation.

## Verification matrix and commands

Use Python 3.14 and Node.js 24. Focused commands run in the managed implementation
worktree with its frozen dependencies and explicitly isolated test database.
Use `rtk` for noisy lint, type checks and broad suites. The commands below were
the original verification outline. Completed local checks and their exact
candidate identities are recorded in `.llm-output/recovery-run.md`.

```text
python -m pytest apps/orchestrator/tests/agents/test_subscription_protocol.py -q
python -m pytest apps/orchestrator/tests/persistence/test_subscription_codex_planning_composition.py apps/orchestrator/tests/persistence/test_subscription_preparation.py apps/orchestrator/tests/persistence/test_subscription_plan_gate.py -q
python -m pytest apps/orchestrator/tests/persistence/test_subscription_pending_decision_roles.py apps/orchestrator/tests/persistence/test_subscription_decision_recovery.py apps/orchestrator/tests/application/test_subscription_decision_recovery_dispatch.py -q
python -m pytest apps/orchestrator/tests/api/test_subscription_task_controls_api.py apps/orchestrator/tests/cli/test_subscription_task_controls_cli.py -q
python -m pytest tests/integration/test_worker_planning_e2e.py tests/integration/test_preparation_resume.py tests/integration/test_pending_control_decisions.py -q
npm run api:generate
npm run api:check
npm run test:web -- --run src/components/subscription-tasks/task-controls.test.tsx src/components/subscription-tasks/task-inspector.test.tsx
```

The implemented recovery service/API/CLI/UI have focused tests in the affected
suites. Browser proof uses the existing process harness in
`tests/acceptance/test_workflow_recovery_browser_process.py` and
`tests/acceptance/workflow_recovery_browser.mjs`. It exercises inspect, preview,
apply, receipt replay, stale previews, live attention updates and API restart
through Chromium, the authenticated API and isolated PostgreSQL. The coupled
subscription regression from Task 1 is mandatory even where existing
integration tests cover only legacy paths.

| Scenario | Required assertion |
| --- | --- |
| Plan approval and preparation, including replay | Implementation objective and approved checks replace planning-only acceptance; planning history is unchanged. |
| Schema versus decoder versus persistence | Legal role/phase decisions agree; primary handoff/scope request and illegal worker decisions cannot become endlessly pending. |
| Retained incident-shaped result | A typed rejection plus bounded correction or attention state replaces silent deferral. |
| Temporary failure, missing prerequisite, unknown effect | Correct class, persisted timing, no unbounded retries or unsafe ownership release. |
| Repair/budget exhaustion and quota blocking | No hidden allowance reset, duplicate debit, unapproved route or provider launch. |
| Worker and operator act concurrently; double click | One receipt, one transition, at most one new attempt. |
| Missing operator authority, invalid CSRF or forged preview | No mutation or provider launch; consistent API/CLI authority checks and safe errors. |
| Crash before/after rejection, contract update, debit, queue or commit | Atomic rollback or exact replay; no lost evidence or duplicate execution. |
| Pause/cancel during preview, apply or launch | Control precedence and physical-stop proof are preserved. |
| Candidate, worktree, approval or policy changes | Old preview refused; applicable evidence/approval revalidated. |
| Old stored results and incompatible worker | Readable history, explicit unsupported reason, no automatic rewrite or misinterpretation. |
| Dashboard and API/CLI responses | Accurate current reason/action eligibility; completed tools remain historical activity; no secret leakage. |

## Recovering the incident after rollout

After the fix and recovery controls pass their checks, obtain operator
authorization for the concrete incident recovery. Inspect the run's current
state first; do not assume it still matches the observations above.

1. Bind the current run, primary task, attempt, result, approved plan, candidate,
   worktree, usage and pending-effect identities. Stop through existing controls
   if execution is active, and verify stopped effects before proceeding.
2. Preview `repair_approved_plan_contract` for the exact retained source. Show the
   replacement objective, unchanged approved scope, rejection to be recorded,
   and remaining attempt/repair budget.
3. Apply once if the preview remains valid. Preserve the original plan and
   returned handoff; record the correction/rejection and schedule one eligible
   attempt. If bindings, approval or budget no longer permit it, return an
   actionable refusal rather than force progress.
4. Verify that the corrected invocation uses the implementation objective and
   reaches a supported decision or an explicit blocked state. Record its causal
   recovery receipt and the observed outcome. No PR or merge approval is implied.

## Completion criteria

- The full subscription regression no longer reaches the observed silent loop.
- Every failed result application has a durable reason and a bounded next step.
- Operator recovery is previewed, authenticated, idempotent and tested through
  concurrent actions, restarts, and pause/cancel.
- Historical work, evidence, usage and human gates remain intact.
- The dashboard describes actual progress and offers only eligible actions.
- Migration/compatibility tests, affected checks and integrated review pass.
- The incident is recoverable through the supported path; actual live recovery
  and its outcome are recorded separately after authorization.
