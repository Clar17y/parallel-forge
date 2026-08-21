# ADR-001: Human approval gates

Status: Accepted  
Date: 2026-08-21

## Context

Forge coordinates probabilistic agents that can change valuable repositories
and, through trusted application code, publish and merge those changes. Agent
output may be incorrect or manipulated by malicious task or repository
content. A single generic final approval would make it unclear which scope and
evidence the operator authorized.

Forge should be autonomous inside an explicit mandate while keeping material
changes of authority under human control.

## Decision

Forge requires three distinct human approval gates.

### Plan approval

Before any repository modification, the operator approves the structured plan,
task/base identity, project policy, dependency scope, required checks, budgets,
and local autonomy limits.

### PR publication approval

Before the first remote write, the operator approves the exact candidate
commit, diff, validation and review evidence, target repository/base, proposed
PR metadata, and remote-remediation limit.

This gate authorizes the deterministic Release Controller to push the managed
branch, create or reconcile one pull request, and push bounded remediation
commits for the same approved task. It does not authorize merge.

### Merge approval

After required checks are green and blocking findings are resolved, the
operator approves one exact remote PR head, observed base commit, check/review
evidence, policy version, and merge method. The Release Controller refetches
GitHub state immediately before merging, supplies the approved head as
GitHub's atomic expected-head precondition, and requires strict up-to-date-base
protection or a merge queue. Any material mismatch invalidates approval.

Only the authenticated operator actor class can create approvals. The API
derives that actor from a server-side session; it does not trust actor input.
Each approval consumes a short-lived, single-use challenge bound to its exact
gate and evidence. Agents and system actors cannot approve themselves.
Approvals are persisted, content-addressed, attributable, idempotent, and tied
to an expected run version.

## Consequences

Positive:

- a compromised or mistaken agent cannot independently publish or merge;
- the operator sees the concrete evidence at each authority boundary;
- Forge can remediate CI autonomously after publication without requesting
  approval for every bounded fix;
- stale approvals fail closed;
- decisions are auditable.

Negative:

- the workflow has deliberate pauses;
- approval evidence and invalidation logic add domain complexity;
- an operator can still approve unsafe work.

## Alternatives considered

### One approval before implementation

Rejected because it would grant remote publication and merge authority before
the resulting code and remote checks exist.

### Approval before every commit or remediation push

Rejected because it makes the CI remediation loop too manual while adding
little protection when scope and retry budgets are already bound.

### Fully automatic merge when green

Rejected for v0.1 because green checks do not prove correctness or intent, and
the user explicitly retains final merge authority.
