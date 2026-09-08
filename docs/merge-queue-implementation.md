# Merge queue implementation contract

Status: implementation pending. This closes the actual queue path of R25-03;
it does not replace the Task 25 direct expected-head merge path.

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

The full v0.1 acceptance gate remains required. This contract is preparation,
not implementation or test evidence.
