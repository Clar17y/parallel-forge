# Merge queue implementation contract

Status: HTTP adapter, immutable receipt, enqueue operation, frozen mode selection
transactional admission, durable observation and merge completion are implemented
with production worker composition. Complete crash/control handling, uncertain
outcome continuation and integrated review remain pending.
This closes the actual queue path of R25-03; it does not replace the Task 25 direct
expected-head merge path. See `docs/v0.1-progress.md` for exact checkpoint evidence.

## Verified gap and source

At `4b82e371a42ac6962b55e2db2cc51939863c600f`, MergeProtection accepts queue
protection, MergeController.merge issues only a direct REST merge, and MergeService
interprets successful operation payloads as merged pull requests. A queue entry
must therefore never be returned as success from that existing merge operation.

GitHub GraphQL reference, inspected 8 September 2026:
https://docs.github.com/en/graphql/reference/pulls

EnqueuePullRequestInput has pullRequestId, expectedHeadOid, clientMutationId and
jump. MergeQueueEntry exposes its ID, pullRequest, headCommit and baseCommit.
The queue configuration supplies its merge method. Queue insertion is distinct
from completion and must be persisted as such. No live GitHub mutation is
necessary or authorized for deterministic development checks.

## Required implementation boundaries

1. Extend Forge-owned GitHub contracts with bounded queue submission and queue
   observation. Bind repository, immutable PR node ID, approved head and source
   operation identity; always supply expectedHeadOid and never jump the queue.
   Treat clientMutationId as correlation, not an assumed idempotency guarantee.
2. Include verified queue method/configuration in protection evidence so operator
   approval binds the actual method. Do not allow queue submission to ignore an
   approved method that differs from repository queue configuration. Preserve
   existing direct-merge evidence compatibility explicitly, with tests.
3. Persist an enqueue intent before the mutation and its canonical receipt after
   submission. Preserve existing source-command and approval ownership fences.
   Recovery observes the same PR/entry and never blindly re-enqueues after an
   uncertain result. A queue receipt is not a merged-commit receipt.
4. Keep MERGING while queued. Schedule bounded durable observation commands with
   immutable source approval, enqueue receipt and deadline bindings. No in-process
   busy loop and no completion while merely queued. Record authoritative merged
   commit before transitioning to COMPLETED; preserve worktree/database resources.
5. Handle entry disappearance, head drift, rejected/unknown enqueue response,
   protection or queue-method drift, expiration and external merge. Unknown effects
   retain reconciliation authority; conclusive rejection has a terminal receipt.
   Queue eviction or changed authority requires fresh evidence/approval before a
   new enqueue. Do not silently substitute direct merge on a queue-required branch.
6. Wire startup recovery, redelivery, pause/resume and operator-visible queue state.
   Review current MERGING control restrictions before adding any queue control;
   no implicit authority to cancel or remove a remotely queued merge.

Queue observation commands use `merge_command_id` for the approved merge delivery.
The separate `source_command_id` and `resume_command_id` fields are reserved for
the existing stopped-delivery/resume chain. Admission events retain their own
`source_command_id` audit binding; resuming must never overwrite the merge reference.

## Test evidence required

- Offline HTTP/GraphQL tests: exact expected head, PR node and method/configuration,
  bounded response parsing, malformed/null/foreign entry rejection, rate limiting,
  definite refusal versus uncertainty, and credential redaction.
- Controller tests: queue required versus strict direct merge, zero direct PUT for
  queue-required protection, zero mutation for stale head/base/method/approval.
- PostgreSQL crash matrix: before admission; intent committed before call; call
  accepted before receipt; receipt before poll scheduling; waiting observation;
  remote merged before final commit; eviction and replay. At most one authorized
  enqueue effect; queue admission never completes the run.
- Source/lease/deadline substitution tests, queue method drift, and final merged
  SHA reconstruction. Run existing release and recovery suites after integration.
- One integrated release repair recheck against original R25-03 and remaining
  R24/R25 findings; scoped correctness question covers queue authority and crashes.

## Operator interpretation

The cockpit's PR / Merge section reports the latest persisted queue admission:
pending, accepted, rejected, or uncertain. Accepted means Forge recorded a queue
entry or reconciled the accepted operation. It is historical admission evidence,
not a live guarantee that the PR remains in GitHub's queue. Eviction, protection
drift and deadline expiry can subsequently require intervention.

Only a recorded merge commit indicates that Forge has persisted merge completion.
While admission is uncertain, inspect the existing PR on GitHub and retain the
operation and resources for startup reconciliation. Do not treat an absent queue
entry as proof that the enqueue had no effect: the PR may already have merged.
Review the run's activity and current GitHub protections when intervention is
requested. Manual GitHub action is a separate operator decision; the cockpit does
not grant authority to retry a merge or remove a queued entry.

The full v0.1 acceptance gate remains required. Current implementation and test
evidence, including outstanding review gates, are recorded in the progress ledger.
