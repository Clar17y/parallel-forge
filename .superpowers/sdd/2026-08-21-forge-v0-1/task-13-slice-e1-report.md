# Task 13 Slice E1 report: durable runtime resource preparation

## Candidate

- Base: `1f1921eb36b9ecaf57f3245d4c54632f2ea8ffa9`.
- Scope: persisted-run `WorktreeProvisioner.prepare`/`reconcile`, exact Controlled Git
  inspection, authoritative operation lookup, and inspection-only active database provenance.
- Review-repair base: `2d4675c0c171926b964ae6725341ff2eeebc8b5b`.

## RED/GREEN evidence

- Authoritative operation handoff: the RED showed that a forged adapter intent could write
  `resource.worktree_preparing` before the mismatch was discovered. The repair now requires the
  adapter handoff to equal the intent freshly loaded by deterministic idempotency key. Focused GREEN:
  `1 passed, 14 deselected`.
- Database active state: the RED showed `verify_active` accepted a `FAILED` binding. It now requires
  the exact `ACTIVE` state before live inspection. Focused GREEN: `1 passed, 73 deselected`.
- Foreign provision result: the RED left the run in `PROVISIONING` because failure persistence was
  attempted with an unvalidated foreign binding. The repair tracks a separately validated binding;
  rejection records `FAILED` with only deterministic name/role and a null secret. Focused GREEN:
  `1 passed, 15 deselected`.
- Canonical repository identity: the RED accepted a `nested/..` alias through normalization. Policy
  input must now equal the Controlled Git canonical repository path exactly. Validation GREEN:
  `8 passed, 11 deselected`; complete worktree module GREEN: `19 passed`.
- Concurrent enabled preparation: two fresh reviewers found that the caller losing the final
  `resource.database_active` optimistic update raised reconciliation even after the winner had
  committed the exact same ACTIVE state. A deterministic two-caller RED produced one exact handle
  and one `WorktreeReconciliationRequired`. The repair now reloads after that conflict and converges
  only when the run, worktree checkpoint, ACTIVE checkpoint, and database-provision intent are exact,
  followed by fresh database verification and fresh Git inspection. Focused unit GREEN:
  `1 passed, 19 deselected`; combined worktree unit/integration GREEN: `23 passed`.
- PostgreSQL workflow coverage: reviewer feedback identified that the provisioner had been exercised
  only through in-memory repository doubles. Three real-PostgreSQL integration tests now prove that
  the operation intent and all-five-field partial checkpoint commit before Git, an injected event
  failure rolls the resource transaction back and invokes no Git, and concurrent enabled prepares
  share one Git effect and one durable database effect/intent while committing one exact ACTIVE
  checkpoint. Focused GREEN: `3 passed`.

## Ordering and state decisions

- The versioned `worktree.create` intent is authoritative and durably committed before the adapter
  writes a resource checkpoint or invokes Git.
- The partial resource update explicitly supplies all five resource fields and commits its real
  `RunEvent` before Git. Enabled runs reserve exact database name/role with a null secret and
  `PROVISIONING`; disabled runs persist `DISABLED` with no database identity.
- Synchronous Git creation and inspection run through deferred cancellation. First or repeated
  cancellation waits for terminal Git state, records an exact proved worktree checkpoint when
  present, propagates cancellation, and never starts database provisioning.
- Reconciliation accepts only an intent UUID through `RecoveryService`, reloads authoritative state,
  binds checkpoints to that intent, and only inspects. It does not create, remove, repair, prune, or
  provision.
- Enabled completion validates the provision result before it can influence persistence, then binds
  the `resource.database_active` event to the exact succeeded `database.provision` intent and freshly
  verifies the live database, role, ownership, settings, and local secret.
- A final ACTIVE optimistic conflict is not treated as general success. The caller may converge only
  on the exact authoritative final state described above; missing, different, or ambiguous durable
  state remains reconciliation-only and is never overwritten by a stale failure checkpoint.
- Requests, checkpoints, outcomes, and public errors contain only safe generated metadata. Branch
  text, worktree paths, administrator/scoped URLs, passwords, transient environment values, and raw
  adapter diagnostics are excluded.

## Verification

- Core E1 modules, including real-PostgreSQL worktree integration: `209 passed, 5 skipped`.
- Adjacent run persistence, worker recovery, host cancellation, redaction, and repository containment:
  `42 passed`.
- Full pytest: `1377 passed, 46 skipped`.
- Ruff check: passed for the three review-repair Python files.
- Ruff format check: all three review-repair Python files already formatted.
- Mypy: `Success: no issues found in 108 source files`.

The RTK pytest wrapper could not spawn pytest on this Windows environment (`os error 1920`), so the
full suite was run directly with the repository virtual-environment Python. No test failure was hidden
by the wrapper issue.

## Explicit exclusions and concerns

- No environment-file copying, repository-reader or runner construction, bootstrap/install/migration/
  seed, workflow transition, teardown, cleanup, prune, worktree removal, database teardown, schema
  migration, worker/API wiring, or standalone CLI behavior was added.
- No push, pull request, or merge was performed. Independent candidate review remains the next step.
