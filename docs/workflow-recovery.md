# Subscription workflow recovery

Use this procedure when a subscription run cannot apply a retained result or its
approved-plan instructions need correction. A completed tool entry, such as
`git.diff`, is historical activity. Inspect the current task and latest attempt
before treating that tool as the operation that needs recovery.

The run overview updates when a workflow needs attention. Open its recovery
details in Tasks to inspect the affected primary coordinator or specialist.
The attempt records a reason, classification, application failures and any next
retry time. An unknown process outcome is distinct from a rejected result:
it requires settlement evidence before another writer can start.

## Choose a supported action

The service supplies eligible actions from current durable state. A preview
explains the intended changes, retained evidence and budget impact. Actions may
be unavailable because work is still active or uncertain, the run is paused or
cancelled, prerequisites changed, budget is exhausted, or the worker cannot
support the recovery version.

| Action | Use | Cost |
| --- | --- | --- |
| Retry saved result (`retry_application`) | Reapply the same retained result after resolving its application failure. | No provider attempt or repair unit. |
| Retry step (`reject_and_retry_step`) | Reject an unusable current result and request a fresh attempt after stopped work is confirmed and the task remains eligible. | Subject to remaining provider, repair and quota limits. |
| Repair approved-plan instructions (`repair_approved_plan_contract`) | Correct a confirmed historical primary contract that still asks for a plan after implementation was approved. | Correcting queued instructions adds no provider attempt or repair unit. Replacing a rejected attempt uses the remaining limits shown in the preview. |

Review the preview, enter the operator reason, then confirm. The service checks
the current state again when applying the action. If the source changed or the
preview expired, refresh and obtain a new preview. If a response is lost, use
**Retry same request** to retrieve the receipt for the original request; do not
create a second recovery to discover whether the first succeeded.

Keep the receipt ID with the incident record. A receipt proves that the recovery
transition was recorded. Check the refreshed task and subsequent attempt to
establish whether execution progressed. Existing pause and cancellation controls
remain available according to their normal rules.

Retrying a saved result is useful when its prerequisite can be restored, such as
an unavailable verification service. When a saved handoff observation has expired
and automatic application retries are exhausted, a permitted **Retry step** obtains
fresh evidence in a new attempt. The service must first establish that the result
is unapplied and that its parent or dependents have not started using it.

An approved-plan instruction repair can also apply to a safely queued primary
after a verified pause/resume or completed coordination, such as delegation or a
wait. Follow the normal resume control before previewing recovery for a paused
run; recovery remains
disabled while paused. The scheduler blocks the stale planning instructions before
another provider attempt starts. Existing resume charges remain recorded, and
correcting only the queued instructions adds no repair charge. Missing or changed
proof still prevents recovery. Pending requests for scope changes and operator
feedback remain available for the next coordinator step.

A child's earlier result cannot be reopened after its parent or dependent work
has started using that outcome. Inspect the current task and its recovery reason
instead of forcing the old child back into execution. Older wait records can
also be refused when they lack the evidence needed to establish safe recovery.

## Local operator CLI

Run the CLI with the same configured control database as the API and worker.
Replace the uppercase placeholders below with the selected run, task and attempt
IDs. Use the same operator context for preview and apply.

```text
forge subscription-tasks recovery-preview --action repair_approved_plan_contract --run-id RUN_UUID --task-id TASK_UUID --attempt-id ATTEMPT_UUID
```

Review the JSON preview and retain its `preview_token`. Apply only while the
preview is eligible and current, using a new key saved with the incident record:

```text
forge subscription-tasks recover --action repair_approved_plan_contract --run-id RUN_UUID --task-id TASK_UUID --attempt-id ATTEMPT_UUID --preview-token PREVIEW_TOKEN --reason "Correct approved-plan instructions after review" --idempotency-key RECOVERY_KEY
```

Use the other named actions from the table when they fit the recorded failure.
If the outcome is unconfirmed, repeat the same command and key. A conflict
requires inspection and a fresh preview; it is not an instruction to force the
stored state forward. `--help` lists the command options without applying a repair.

## Operator API

API and CLI recovery use the same application service as the dashboard. Use an
authenticated operator session and the normal CSRF protection for HTTP writes.
The attempt route prefix is
`/runs/{run_id}/subscription-tasks/{task_id}/attempts/{attempt_id}`.

1. POST to `/recovery/preview` below that prefix with `{ "action": "..." }`.
   Inspect `eligible`, `reason_code`, `message`, `changes`, `retained_evidence`,
   `budget_impact` and `expires_at`. Preview does not queue work or spend budget.
2. POST to `/recovery` with `action`, the returned `preview_token` and `reason`.
   Send an `Idempotency-Key` unique to this intended action. The returned receipt
   binds the run, task, attempt and action.
3. If the response is uncertain, repeat exactly the same body and key. If the
   server rejects a stale preview, read current state and preview again before
   making a new request. Reusing a key with different content is a conflict.

A preview is a snapshot, not permission to bypass current eligibility. The
service checks versions, evidence, controls and effect settlement at apply time.
Do not script a loop that creates fresh keys on failures.

## Evidence and authority

Recovery preserves the full original contract, returned result, tool evidence,
partial repository work and cumulative usage. An accepted planning result stays
accepted when its queued implementation instructions are corrected. Rejection
and contract correction have separate causal records. Repeated invalid output
has a bounded next step; it must not produce indefinite silent application retries.

The primary coordinator cannot complete by returning a specialist handoff or
scope request. A contract repair changes the approved implementation objective;
it does not relax role boundaries, expand scope, reset budgets, approve a PR or
authorize a merge.

Do not clear database leases, pending effects, diagnostics or receipts by hand.
Silence, an old heartbeat or an expired lease is not proof that a provider or
repository writer has stopped. Where no action is eligible, preserve the reason
and evidence and resolve the stated prerequisite through its supported control.

## Rollout and rollback

Deploy the additive schema and readers before enabling recovery mutations.
Confirm that the serving worker supports the contract and recovery versions;
stop incompatible workers through their normal lifecycle before allowing new
recovery work. Migrations preserve historical contracts and do not repair
unselected runs automatically.

For rollback, disable new recovery mutations and stop incompatible execution.
Retain diagnostics, contract revisions and receipts. Do not downgrade storage or
workers so that already committed recovery records become unreadable.

Changes to a live run require a current preview and operator authorization for
that action. Development tests use disposable PostgreSQL data and simulated
providers; they do not recover a live run or establish provider availability.
