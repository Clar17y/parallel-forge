# Task 13 Slice E2a report

## Scope

E2a only: policy-controlled environment path validation, database environment
rematerialization, opaque worktree capabilities, protected environment staging,
native path safety, and digest-only evidence. E2b container readability and E2c
setup/orchestration remain deferred.

## Security mechanics

- Policy paths use one canonical validator, ordered effective-secret-path union,
  case-collision detection, reserved-component rejection, Windows ADS/device /
  trailing-dot-space rejection, and no-follow bounded reads.
- `WorktreeCapability` and `EnvironmentStagingPlan` are owner-sealed and hide
  handles, source bytes, output bytes, and private evidence. Revalidation proves
  the deterministic `WorktreeIdentity.for_run`, registration/target identity,
  exact base SHA, current HEAD, and base ancestry.
- Publication is bounded, exclusive, no-replace, single-link, permission-checked,
  flush/fsync verified, reopened, content-verified, idempotent for exact retries,
  and refuses a different existing destination. Temporary names are exact and
  cleaned up on injected write/link/flush/reopen/cleanup faults.
- Windows uses native handle-relative no-follow opens, protected DACL checks,
  explicit sharing, and reparse rejection. Linux Docker ACLs use native
  dynamically loaded `libacl` and require the exact UID-10001 read entry; missing
  or unsupported ACL support fails closed. Linux owner checks require the current
  POSIX UID. Linux native ACL/read tests are platform-gated on this Windows host.
- Inspection requests existing-only mutation-lock access, so it fails closed if
  the Forge lock is absent without creating lock or registration metadata.
- Database ACTIVE rematerialization reuses the authoritative proof and performs
  exact read-only live verification without operation-row mutation; disabled
  rematerialization is an exact empty binding. Secret and URL values are not
  present in representations, exception causes, contexts, or evidence.

## Verification

- Focused staging module: `43 passed, 2 skipped`.
- Expanded security staging group: `48 passed, 4 skipped`.
- Affected E2a suites (staging, paths, Git, database provisioner, policy,
  repository, secret-store, redaction, telemetry): `456 passed, 46 skipped` in
  222.85 seconds.
- Strict mypy over `apps/orchestrator/src/forge`: passed, 110 source files.
- Ruff check over source: passed.
- Ruff format check over source: passed, 110 files already formatted.
- Full configured pytest suite: `1425 passed, 50 skipped` in 330.23 seconds.

## Platform/deferred limits

The current host is Windows, so Linux native `libacl` and POSIX descriptor
tests are honest skips; the production implementation fails closed when that
mechanism is unavailable. No container was started and no fixed-UID container
read claim is made; that completion gate belongs solely to E2b. E2c setup,
transitions, teardown, migrations, CLI, push, PR, and merge are outside E2a.
