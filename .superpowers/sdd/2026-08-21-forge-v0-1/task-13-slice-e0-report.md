# Task 13 Slice E0 report: atomic operation-intent causal events

## Scope and base

- Base: `1c404e148483e0799923f1cdaf7f00297a480762`.
- Production scope is limited to `PostgresOperationRepository.begin`.
- No constructor, public operation/executor API, lease/effect behavior, migration, unit of work,
  worker wiring, or E1 lifecycle code changed.

## TDD evidence

The four required persistence regressions were added before production code. The first focused run
used `-x -q` and failed for the intended missing behavior:

```text
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence/test_operation_intents.py \
  -k "new_operation_begin_appends_one_safe_causal_event or duplicate_begin_preserves_owner_and_lease_without_another_event or event_append_failure_rolls_back_pair_and_retry_creates_one_pair or concurrent_same_key_begins_create_one_intent_event_pair" \
  -x -q

FAILED test_new_operation_begin_appends_one_safe_causal_event
assert len(events) == 1
E assert 0 == 1
1 failed, 15 deselected in 2.10s
```

After the minimal repository change, the exact four nodes passed together:

```text
4 passed in 4.96s
```

## Implementation

- The repository locks and reads the authoritative run through
  `PostgresRunRepository(session).get_for_update` before the candidate insert.
- Only the successful `INSERT ... RETURNING` winner constructs and appends one
  `operation.intent_created` event.
- The existing `PostgresEventRepository` receives the same private session inside the existing
  `session.begin()` transaction, preserving sequence, redaction, correlation, and rollback behavior.
- The event uses the locked run version, system actor, schema version 1, candidate timestamp, and only
  the exact safe intent ID/kind/digest/request-schema metadata.
- Duplicate begins append no event and retain the winning owner, lease expiry, and attempt count.
- A failure injected after event append/flush rolls back both rows; the next retry creates exactly one
  pair. Concurrent same-key begins likewise resolve to one pair without assuming which owner wins.

## Verification

- Required E0 regression nodes: `4 passed in 4.96s`.
- Complete operation-intent module: `19 passed in 20.84s`.
- Existing run-event and atomic run-service modules: `23 passed in 24.47s`.
- Complete persistence suite: `103 passed in 103.24s`.
- Ruff check across `apps/orchestrator`: passed.
- Ruff format check for the two touched Python files: passed (`2 files already formatted`).
- Mypy across `apps/orchestrator/src`: passed (`107 source files`).
- `git diff --check`: passed.

## Concerns

None. The disposable PostgreSQL-backed persistence suite was available and exercised the sequential,
rollback/retry, and concurrent unique-key paths.

## Final independent gates

- Independent specification and quality review at
  `9ae02f1c8cd32479fad75fa471843ef2eac33b65`: approved with no P0-P2 findings.
- Independent concurrency and atomicity audit: no findings. The run-before-insert lock ordering,
  winner-only event creation, rollback behavior, and duplicate claim preservation were confirmed.
- Fresh exact-candidate operation-intent module: `19 passed in 21.96s`.
- Fresh exact-candidate full suite: `1347 passed, 46 skipped in 321.01s`.
- Ruff check passed; candidate-scoped Ruff format check passed (`2 files already formatted`).
- Mypy passed (`107 source files`); range `git diff --check` passed.
- HEAD, worktree, and index remained exact and clean, with no lingering Python or Git processes.
