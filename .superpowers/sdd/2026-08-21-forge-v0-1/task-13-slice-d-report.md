# Task 13 Slice D report: isolated PostgreSQL provisioner

## Scope and base

- Base: `24491b834e7681afa9d8cde7b0265d2ef747d4d9`.
- Scope stayed limited to the database provisioner, its application ports, focused tests, and this report.
- No E0 worktree orchestration, CLI, scripts, migrations, or generic SQL utility was added.

## TDD evidence

The focused test module was written before the production module. The required first run was:

```text
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/tools/test_database_provisioner.py -q
```

Initial RED was collection failure: `ModuleNotFoundError: No module named 'forge.tools.database'`.
After the interrupted candidate had a production module, the immediate resumed RED was the incorrect
`OperationAdapter` import from `forge.domain.operation`; it was corrected to
`forge.application.ports.operations`. The existing draft then drove the minimal implementation to
GREEN before the adversarial cases were expanded.

## Implementation

- Added `AdminSecretResolverPort`, immutable `DatabaseBinding`, and `DatabaseProvisionerPort`.
- Added `DatabaseProvisioner` with private asyncpg-compatible protocols and injected operation executor,
  resolver, local secret store, 32-byte token source, and connection factory.
- Recomputes and compares the exact persisted `WorktreeIdentity`; rejects non-persisted runs and forged
  deterministic names before any dependency call.
- Disabled provision/teardown requires the null disabled shape, returns an immutable empty environment,
  and makes zero executor/resolver/store/password/driver calls.
- Creates versioned `database.provision` and `database.teardown` intents with redacted payloads and
  outcomes. Administrator resolution occurs only after the intent boundary; already-succeeded provision
  resolves only the administrator URL and reads the exact local password.
- Canonical `secret://` reference validation rejects empty/dot segments, query/fragment, credentials,
  ports, percent encoding, and backslashes.
- Uses SQLAlchemy structured URL parsing/building, preserves query options, strips fragments, accepts
  PostgreSQL/asyncpg drivers, forces maintenance database `postgres`, and emits scoped URLs transiently.
- Generates exactly 32 random bytes, stores URL-safe padded Base64 ASCII, creates the exact safe LOGIN
  role/database, verifies role flags, both membership directions, role/database settings, owner, and
  local secret existence.
- Provision recovery is inspection-only. Teardown uses the exact fail-closed matrix, parameterized
  inspection/session termination values, validated quoted identifiers, database → role → secret order,
  and no `CASCADE`/`DROP OWNED`.
- Connection closure is owned by the adapters on success, ordinary failure, and cancellation; stable
  errors clear raw cause/context/traceback details.

## Security/TDD coverage

`test_database_provisioner.py` covers disabled zero-call and forged identity/resource rejection,
canonical reference corpus, structured URL round trips/rejections, intent-before-effect order,
password entropy/encoding and repr redaction, owner mismatch and inspection-only reconciliation,
the full teardown matrix, unsafe role flags/memberships/settings, failures after destructive steps,
secret-create races, cancellation close-once, succeeded-intent reuse, SQL capture/parameterization,
and stable errors without driver details.

The guarded real PostgreSQL case uses only generated deterministic names validated against the strict
identifier grammar. It verifies scoped-role connectivity, owner/current database identity, session
termination, and exact role/database/secret absence after teardown. Its fallback cleanup is limited to
those same validated names and parameterized session termination.

## Checks

- Focused database provisioner plus guarded integration: `52 passed`.
- Operation/recovery and secret suites: `54 passed, 15 skipped`.
- Affected security suites: `14 passed, 1 skipped`.
- Ruff check and format check: passed for the touched files and all `107` orchestrator source files.
- Mypy: passed for all `107` orchestrator source files and touched tests.
- `git diff --check`: passed before final commit verification.

## Material concern

The integration test is intentionally guarded and skips when the local PostgreSQL endpoint is absent;
the current environment had `127.0.0.1:5435` available and exercised it successfully.
