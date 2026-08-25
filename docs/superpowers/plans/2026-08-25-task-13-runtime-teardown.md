# Task 13 Runtime Teardown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add exact, durable, retry-safe persisted-run worktree and database teardown while retaining branches.

**Architecture:** `WorktreeProvisioner.teardown` uses a deterministic `worktree.teardown` operation intent around exact Git removal, then delegates exact database cleanup to the existing `DatabaseProvisioner.teardown` intent boundary. Atomic run resource events record worktree absence before database cleanup and record `REMOVED` only after database cleanup succeeds; reconciliation inspects exact absence and never guesses ownership.

**Tech Stack:** Python 3.13, asyncio, pytest, Typer-independent application services, SQLAlchemy repository ports, existing Forge operation intents and controlled resource adapters.

## Global Constraints

- Worktree removal must be verified before any enabled database cleanup starts.
- The disabled database path makes zero database, administrator-secret, resolver, or secret-store calls and retains `ResourceState.DISABLED`.
- Branches are retained; Task 13 adds no branch deletion operation.
- Every external effect has an intent before the effect and a causal resource event after exact verification.
- Errors, events, intents, and outcomes contain no secret bytes, administrator URL, scoped database URL, environment contents, or raw dependency diagnostics.
- Ambiguous, foreign, linked/reparse-point, or mismatched resources fail closed.
- Production behavior is implemented only after a focused test is observed failing for the intended reason.

---

### Task 1: Exact persisted-run teardown happy paths

**Files:**
- Modify: `apps/orchestrator/src/forge/application/ports/worktrees.py`
- Modify: `apps/orchestrator/src/forge/tools/worktree.py`
- Modify: `apps/orchestrator/tests/tools/test_worktree.py`

**Interfaces:**
- Consumes: `RunRepository.update_resource`, `OperationExecutor`, `ControlledGitPort.remove_worktree/prune`, `DatabaseProvisionerPort.teardown`, and `WorktreeIdentity.for_run`.
- Produces: `WorktreeProvisionerPort.teardown(run_id: UUID, policy: ProjectPolicy) -> RunSnapshot` and the matching concrete method.

- [ ] **Step 1: Add a failing disabled-resource teardown test**

Extend the existing `_Git` fake with exact `remove_worktree` and `prune` behavior and extend `_Database` with a teardown method that records any forbidden call. Add a test that starts with the exact prepared worktree path and `ResourceState.DISABLED`, calls:

```python
removed = await provisioner.teardown(RUN_ID, policy)
```

Assert the literal mutation order is:

```python
[
    "operation:worktree.teardown",
    "git:inspect",
    "git:remove",
    "git:inspect",
    "git:prune",
    "event:resource.worktree_removed",
]
```

Also assert `removed.worktree_path is None`, `removed.database_state is ResourceState.DISABLED`, no database call occurred, the fake branch remains present, and exactly one `resource.worktree_removed` event names the teardown intent without secret material.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "disabled_teardown"
```

Expected: FAIL because `WorktreeProvisioner` has no `teardown` method.

- [ ] **Step 3: Add the public contract and deterministic worktree teardown request**

Add to `WorktreeProvisionerPort`:

```python
async def teardown(self, run_id: UUID, policy: ProjectPolicy) -> RunSnapshot: ...
```

In `worktree.py`, add versioned constants for `worktree.teardown`, `resource.worktree_removed`, and `resource.database_removed`. Build the request only from stable exact identity fields:

```python
payload = {
    "project_id": str(run.project_id),
    "run_id": str(run.id),
    "policy_version": policy.version,
    "branch_digest": hashlib.sha256(identity.branch.encode("utf-8")).hexdigest(),
    "worktree_name": identity.worktree_name,
    "base_sha": require_sha(run.base_sha),
}
```

Use an idempotency key scoped by protocol version, operation kind, project UUID, run UUID, and policy version. Do not include mutable resource state in the request identity.

- [ ] **Step 4: Implement the minimal worktree-removal adapter and disabled path**

Add a teardown-specific context loader that validates the run/project/policy/repository, recomputed identity, absolute exact path, and database shape without requiring `PREPARING_WORKTREE`. It accepts a fully absent disabled resource as already complete and requires causal teardown evidence before accepting `worktree_path=None` with an enabled remaining database.

The adapter must:

```python
present = await owner._inspect_teardown_target(context.expected)
if present is None:
    return NEEDS_RECONCILIATION
await owner._remove_exact_worktree(context.expected)
if await owner._inspect_teardown_target(context.expected) is not None:
    return NEEDS_RECONCILIATION
await owner._prune_after_absence()
await owner._record_worktree_removed(context, intent.id)
return exact_teardown_outcome
```

The real helpers use `asyncio.to_thread` plus `await_deferred_cancellation`, compare the complete `ManagedWorktree` handle, redact dependency failures, and never call a branch operation. `teardown` skips every database boundary for disabled policy and returns the freshly reloaded snapshot.

- [ ] **Step 5: Run the disabled test and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Add a failing active-database teardown test**

Start from an exact `ACTIVE` binding and prepared worktree. The database fake returns:

```python
DatabaseBinding(state=ResourceState.REMOVED)
```

Assert Git removal and `resource.worktree_removed` occur before `database:teardown`, then `resource.database_removed` persists `worktree_path=None`, `database_state=REMOVED`, and null database name/role/secret. Assert the branch still exists.

- [ ] **Step 7: Run the active test and verify RED**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "active_database_teardown"
```

Expected: FAIL because enabled database cleanup/final persistence is not implemented.

- [ ] **Step 8: Implement exact database teardown after worktree absence**

Construct `DatabaseBinding` from the freshly reloaded run, validate it through `DatabaseProvisionerPort.validate_binding`, and call:

```python
binding = await self._database.teardown(
    context.identity,
    context.policy.database,
    resource,
    policy_version=require_policy_version(context.run),
)
```

Accept only an exact `ResourceState.REMOVED` result with null identity. Atomically persist `resource.database_removed` with `worktree_path=None`, `database_state=REMOVED`, and null name/role/secret. A database exception leaves the exact prior database state and identity untouched and raises `WorktreeReconciliationRequired` without raw chaining.

- [ ] **Step 9: Run both happy-path tests and verify GREEN**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "teardown"
```

Expected: PASS.

### Task 2: Failure, retry, reconciliation, and cancellation safety

**Files:**
- Modify: `apps/orchestrator/src/forge/tools/worktree.py`
- Modify: `apps/orchestrator/tests/tools/test_worktree.py`
- Modify: `apps/orchestrator/tests/tools/test_worktree_setup.py`

**Interfaces:**
- Consumes: the Task 1 teardown request, adapter, events, and database teardown boundary.
- Produces: `WorktreeProvisioner.reconcile` dispatch for both `worktree.create` and `worktree.teardown`, plus retry-safe partial-state behavior.

- [ ] **Step 1: Add a table-driven failing identity and ordering suite**

Use literal cases for a wrong path, wrong branch handle, foreign registration, unsafe/non-absolute path, linked/reparse-point boundary error, wrong database name, wrong role, wrong secret ID, and locked worktree removal. For every case assert no database teardown, no prune before exact absence, no branch deletion, no false resource event, and a stable redacted public error.

- [ ] **Step 2: Run the safety suite and verify RED**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "teardown_rejects or teardown_locked"
```

Expected: at least one case FAILS because teardown validation/recovery is incomplete.

- [ ] **Step 3: Complete fail-closed validation**

Separate setup-state validation from teardown-state validation. Teardown accepts exact `PROVISIONING`, `FAILED`, or `ACTIVE` enabled bindings, exact `REMOVED` terminal state, and exact disabled state. It rejects partial identifiers that `DatabaseProvisioner.validate_binding` rejects. Update resource-record validation to permit only the exact teardown transitions:

```text
exact path + current database state -> null path + unchanged database state
null path + enabled remaining identity -> null path + REMOVED/null identity
null path + DISABLED -> unchanged idempotent completion
null path + REMOVED -> unchanged idempotent completion
```

- [ ] **Step 4: Add failing interruption/retry tests**

Cover interruption after removal but before its event, after the worktree event but before database teardown, after database teardown but before `REMOVED` persistence, prune failure after exact absence, and repeated complete teardown. Assert a retry never calls `remove_worktree` after exact absence, never starts database teardown before verified worktree absence, and adopts the existing database teardown intent.

- [ ] **Step 5: Run retry tests and verify RED**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "teardown_retry or repeated_teardown"
```

Expected: FAIL because teardown reconciliation dispatch and checkpoint adoption are incomplete.

- [ ] **Step 6: Implement inspection-led teardown reconciliation**

Before `RecoveryService.reconcile`, load the requested intent and select `_WorktreeAdapter` only for `worktree.create` and `_WorktreeTeardownAdapter` only for `worktree.teardown`; every other kind fails closed. Teardown reconciliation:

- returns reconciliation-required while the exact worktree is still present;
- when exact absence is observed, prunes stale metadata, writes the missing `resource.worktree_removed` checkpoint if needed, and returns the exact succeeded outcome;
- never invokes `remove_worktree`, database teardown, or any branch mutation;
- accepts an existing checkpoint only when its intent ID and stable request payload match exactly.

After worktree intent convergence, ordinary `teardown` resumes database cleanup. The existing database provisioner owns reconciliation of its deterministic teardown intent.

- [ ] **Step 7: Add a failing deferred-cancellation test**

Block the fake Git removal thread, cancel the caller twice, release the mutation, and assert exact absence is inspected and `resource.worktree_removed` is committed before `CancelledError` propagates. Assert no database call starts. A later retry must reconcile the worktree intent and complete database cleanup exactly once.

- [ ] **Step 8: Run the cancellation test and verify RED**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py -q -k "teardown_cancellation"
```

Expected: FAIL until cancellation is deferred through verification/checkpointing.

- [ ] **Step 9: Implement terminal-result cancellation handling**

Mirror the reviewed setup pattern: await the Git thread through repeated cancellation, inspect the terminal exact state, record the causal event, then propagate cancellation. If exact state cannot be verified, retain the intent as reconciliation-required and do not start database cleanup.

- [ ] **Step 10: Run the complete worktree lifecycle tests**

Run:

```powershell
python -m pytest apps/orchestrator/tests/tools/test_worktree.py apps/orchestrator/tests/tools/test_worktree_setup.py -q
```

Expected: PASS with no warnings or leaked raw diagnostics.

### Task 3: Static checks, regression verification, and slice commit

**Files:**
- Verify: all files changed by Tasks 1–2.

**Interfaces:**
- Consumes: the complete Slice F candidate.
- Produces: one independently reviewable runtime-teardown commit.

- [ ] **Step 1: Run affected adapter tests**

```powershell
python -m pytest apps/orchestrator/tests/tools/test_git.py apps/orchestrator/tests/tools/test_database_provisioner.py apps/orchestrator/tests/tools/test_secret_store.py apps/orchestrator/tests/tools/test_worktree.py apps/orchestrator/tests/tools/test_worktree_setup.py -q
```

Expected: PASS.

- [ ] **Step 2: Run static checks**

```powershell
python -m ruff check apps/orchestrator/src/forge/application/ports/worktrees.py apps/orchestrator/src/forge/tools/worktree.py apps/orchestrator/tests/tools/test_worktree.py apps/orchestrator/tests/tools/test_worktree_setup.py
python -m mypy apps/orchestrator/src/forge/application/ports/worktrees.py apps/orchestrator/src/forge/tools/worktree.py
git diff --check
```

Expected: all commands succeed with no findings.

- [ ] **Step 3: Run the full orchestrator suite**

```powershell
python -m pytest apps/orchestrator/tests -q
```

Expected: PASS. If the known schema-test infrastructure flake appears, rerun that exact test once and record both outputs without changing lifecycle code unless a deterministic regression is proven.

- [ ] **Step 4: Review the candidate against the design**

Inspect the complete diff for exact-identity enforcement, intent-before-effect ordering, worktree-before-database ordering, idempotence, cancellation, branch retention, redaction, and test mutations. Resolve every P1/P2 finding before commit.

- [ ] **Step 5: Commit Slice F**

```powershell
git add apps/orchestrator/src/forge/application/ports/worktrees.py apps/orchestrator/src/forge/tools/worktree.py apps/orchestrator/tests/tools/test_worktree.py apps/orchestrator/tests/tools/test_worktree_setup.py
git commit -m "feat(orchestrator): durably teardown run resources"
```
