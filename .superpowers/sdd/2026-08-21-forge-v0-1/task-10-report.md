# Task 10 implementation report

Status: DONE_WITH_CONCERNS (Slice 1 only)

Base: `dbeb9dec670817b326bf2b600ec0638432f92fda`

## Slice 1 — persistence foundation

### Decisions

- Added forward migration `20260822_0002` without changing `20260821_0001`.
  Existing rows are backfilled deterministically: project name defaults to
  `Project`, canonical path identity is slash-normalized and case-folded,
  GitHub identity is case-folded, task title defaults to `Imported task`, and
  legacy normalized task text becomes the task body.
- `projects.canonical_path_key` is the unique case-insensitive identity key;
  `canonical_path` remains the resolved display path. A BEFORE UPDATE trigger
  rejects changes to canonical path/key, normalized GitHub identity, or default
  branch while allowing policy-version advancement. A BEFORE INSERT trigger
  normalizes legacy SQL inserts.
- Task source artifacts preserve exact title/body/source URL/source timestamp,
  derive NFC/LF `normalized_text`, and bind exact source plus external identity
  fields into a canonical SHA-256 `task_digest`. External uniqueness is scoped
  to `(project_id, external_source, external_id)`, and a trigger rejects task
  UPDATE/DELETE. Plain tasks cannot carry external metadata.
- `api_mutations` stores only SHA-256 idempotency-key hashes, action/scope,
  actor, request digest, lifecycle, safe response, and resource identity.
  `PostgresMutationRepository.reserve` replays an identical completed receipt,
  conflicts on a changed request, and fails closed on an incomplete receipt.
- `operator_audit_events` is append-only and recursively redacts payloads
  before persistence. Project/task/policy records and audit/mutation records
  are all session-bound to the existing PostgreSQL unit of work.
- `RunSnapshot.policy_version`, `base_ref`, and `base_sha` are optional for
  backward-compatible callers; repository mapping returns persisted values,
  and new values are validated and persisted when supplied.
- Downgrade conditionally recreates Task 9's global external identity unique
  constraint only when existing Task 10 data has no cross-project duplicates;
  this keeps downgrade/upgrade transactional for valid project-scoped data.

### TDD evidence

- Initial capsule API command RED: `python -m pytest apps/orchestrator/tests/api/test_projects.py ...`
  failed with the expected missing-test-file error because Task 10 modules/tests
  did not exist.
- Persistence contract RED: new model/snapshot tests failed with missing
  `api_mutations`/`operator_audit_events` declarations and unexpected
  `RunSnapshot` keywords.
- Migration-head RED: the forward-head test observed only
  `20260821_0001`.
- Repository RED: focused integration tests observed missing UoW repositories,
  missing mutation module, and missing audit adapter.
- Migration debugging RED: schema checks exposed Alembic check-name drift;
  repository teardown exposed downgrade failure with valid cross-project
  duplicate external identities; backfill test exposed body default replacing
  legacy normalized text. Each was fixed at the migration source and rerun.
- GREEN after implementation: `test_task10_persistence.py` — 8 passed;
  `test_schema.py` — 17 passed; existing run/atomic suites — 27 passed;
  complete persistence suite — 89 passed.

### Files and behavior

- Added `apps/orchestrator/migrations/versions/20260822_0002_task10_foundation.py`.
- Extended `persistence/models/project.py`, added `persistence/models/api.py`,
  and exported the new models.
- Added application records/protocols in `application/ports/projects.py`,
  `tasks.py`, `mutations.py`, and `audit.py`.
- Added PostgreSQL repositories in `persistence/repositories/projects.py`,
  `tasks.py`, `mutations.py`, and `audit.py`; exported them and wired one
  session-bound instance of each into `PostgresUnitOfWork` (`projects`,
  `tasks`, `mutations`, `audit`, and `audits` alias).
- Extended the run snapshot domain and mapper/create path, and updated the
  shared persistence fixture to expect the persisted policy snapshot.
- Updated schema contract tests for the new migration head/tables, task-scoped
  external uniqueness, and unversioned mutation response payload behavior.

### Verification

- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence/test_task10_persistence.py -q` — 8 passed.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence/test_schema.py -q` — 17 passed.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence/test_run_repository.py apps/orchestrator/tests/persistence/test_atomic_transition.py -q` — 27 passed.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence -q` — 89 passed.
- `.venv\\Scripts\\python.exe -m mypy apps/orchestrator/src/forge` — success, 79 files.
- `.venv\\Scripts\\ruff.exe check --fix ...` — all touched source/test files clean after formatting fixes.
- `git diff --check` — clean.
- The schema suite executes Alembic upgrade from `0001`, head inventory,
  downgrade/re-upgrade, and `alembic check`; all passed.

### Self-review

- No raw idempotency key is a model field or response value; only its hash is
  stored. Audit payloads pass through the existing bounded redactor.
- Repositories never open nested sessions or commit independently; all writes
  share `PostgresUnitOfWork`'s one transaction.
- Task and audit append-only triggers are exercised with update/delete
  attempts; project identity trigger is exercised against default-branch
  mutation.
- Existing Task 9 persistence and run-state tests remain green after snapshot
  mapper extension.

### Concerns / remaining scope

- This commit intentionally stops at the requested serial Slice 1 boundary.
  Task 10 high-level project/task services, local Git inspection adapter,
  atomic run-creation service, run-command service, API schemas/routes,
  security-route tests, and full repository verification remain for the next
  slice; no HTTP behavior was added here.
- A repository with valid Task 10 duplicate external identities cannot restore
  Task 9's global uniqueness on downgrade without data loss. The downgrade
  therefore omits that obsolete constraint only in the duplicate case and
  remains upgrade-compatible.

## Candidate commits

Slice 1 candidate: `6c0afae` (`feat: add task10 persistence foundation`).
No push, PR, merge, or Task 10 service/API work was performed.

## Slice 2 — project/task services and local repository inspection

### Scope and implementation

Slice 2 starts from candidate `6c0afae` and intentionally stops before run
creation, run commands, HTTP schemas/routes, and Task 11. It adds:

- `LocalGitRepositoryInspector` behind the existing narrow
  `RepositoryInspector` port. It walks every configured path component with
  `lstat`, rejects POSIX links and Windows reparse points, canonicalizes the
  repository and data root, rejects equality/containment in either direction,
  requires Git's exact top-level, reads only local `remote.origin.url`,
  normalizes HTTPS/SSH/SCP GitHub identities, validates the branch, and
  resolves one lowercase 40-hex commit. Every Git call is an argv list with
  `shell=False`, disabled prompting, bounded output, and a ten-second timeout;
  failures expose only the generic `repository validation failed` message.
- `ProjectService` closed registration and policy-update request models. The
  service validates `ProjectPolicy` and `CommandSpec`, stores only database
  secret references, never calls the optional resolver, and coordinates one
  UoW for project/policy persistence, hashed durable mutation receipts, and
  one redacted audit event. Registration creates policy version 1; updates
  require the exact current version, preserve identity fields, append N+1, and
  replay the original version without a second audit row.
- `TaskService` closed plain/external source request models. It preserves
  exact title/body/source fields, delegates NFC/LF normalization and the
  canonical source/external-identity digest to the repository, scopes external
  identity by project, and uses the same durable replay/conflict plus one audit
  event boundary. No metadata/environment bag was added.
- Focused unit, adapter, and PostgreSQL integration tests for path/Git
  validation, source round-trip, database policy validation, argv rejection,
  idempotent replay/conflict, policy append/versioning, project-scoped
  external identity, audit cardinality, and no secret resolution.

### TDD evidence

- RED before implementation:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_repository_inspection.py apps/orchestrator/tests/application/test_project_task_services.py -q`
  failed during collection with the expected missing `forge.application.adapters`
  and missing project-service module imports.
- First GREEN after the adapter/service implementation:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_repository_inspection.py apps/orchestrator/tests/application/test_project_task_services.py -q`
  — 10 passed, 1 skipped; subsequent closed-policy and stale-version tests
  expanded this to 15 passed, 1 skipped.
- Local Git integration GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_repository_inspection.py -q`
  — 7 passed, 1 skipped, including an actual local Git repository with an
  SCP-style origin and committed default branch.
- PostgreSQL service integration GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence/test_task10_service_integration.py -q`
  — 1 passed.
- Existing persistence regression GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence -q`
  — 90 passed.
- Migration/schema regression GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence/test_schema.py -q`
  — 17 passed, including upgrade from `0001`, downgrade/re-upgrade, and
  Alembic schema checks on a disposable PostgreSQL database.
- Focused combined GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_repository_inspection.py apps/orchestrator/tests/application/test_project_task_services.py apps/orchestrator/tests/persistence/test_task10_service_integration.py -q`
  — 15 passed, 1 skipped.
- Static checks GREEN:
  `.venv\Scripts\ruff.exe check ...` — all touched source/tests passed;
  `.venv\Scripts\ruff.exe format --check ...` — all six touched files
  already formatted; `.venv\Scripts\python.exe -m mypy apps/orchestrator/src/forge`
  — success, 83 files; `git diff --check` — clean.
- Full repository GREEN:
  `.venv\Scripts\python.exe -m pytest -q` — 829 passed, 2 skipped.

### Decisions and concerns

- Service requests are closed Pydantic models with `extra="forbid"`; the
  mutable policy model excludes repository path, GitHub identity, and default
  branch. This preserves the later HTTP 422 contract without introducing
  route code in this slice.
- Mutation request digests are computed before repository inspection so an
  identical completed registration can replay its durable resource without
  re-running Git. New registrations still inspect before creating the
  project, and all writes remain in the caller's one PostgreSQL transaction.
- Audit payloads contain request/policy/task/identity digests and bounded
  flags only. Exact task body/title, local paths, Git stderr, environment
  values, and secret values never enter the audit or mutation response.
- A skipped symlink test means the host did not permit creating a directory
  symlink; the adapter still checks both `lstat` symlinks and Windows reparse
  attributes. The actual local-Git test passed on this host.
- Run creation/command services, authenticated API routes/security tests, and
  full Task 10 HTTP verification remain intentionally unresolved for the next
  serial slice.

## Candidate commits

Slice 1 candidate: `6c0afae` (`feat: add task10 persistence foundation`).
Slice 2 candidate: `75710d5` (`feat: add task10 project and task services`).

## Slice 3 — atomic run creation and closed run commands

### Scope and implementation

Slice 3 starts from candidate `75710d5` and intentionally stops before HTTP
schemas, routes, and security wiring. It adds:

- A dedicated `RunService` and narrow `RunUnitOfWork` boundary. Creation
  reserves a durable `create_run` receipt, locks the task and project,
  freshly resolves and validates `refs/heads/{default_branch}` plus one
  lowercase 40-hex base SHA, creates an exact `CREATED`/version-0 snapshot,
  appends one safe `run.created` event, enqueues one empty-payload
  `start_planning` command with `{run_id}:start-planning`, completes the
  receipt, and commits through one PostgreSQL session.
- Replay and changed-request handling for run creation, deterministic run
  list/get query records, and optional branch-name snapshot mapping needed by
  teardown confirmation. Query records expose no run resource secret fields.
- A closed `RunCommandRequest` and `RunCommandService` for all seven command
  types. The service locks the run, enforces exact version/state/payload rules,
  hashes the raw header into an actor/run/route-scoped bounded queue key, and
  enqueues without changing state or version. Teardown branch deletion
  requires an exact persisted branch confirmation.
- Focused fake and PostgreSQL integration coverage for all seven valid and
  invalid command-state cases, feedback/teardown validation, replay/change
  conflicts, exact planning command/event payloads, safe snapshots, unchanged
  state/version, and rollback after an event has actually been inserted.

### TDD and verification evidence

- RED before implementation:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_run_services.py -q`
  failed with the expected missing run-service implementation (11 failures).
- Focused GREEN before the final audit:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/application/test_run_services.py apps/orchestrator/tests/persistence/test_task10_run_service_integration.py -q`
  — 21 passed in 3.04s.
- Focused GREEN after repairing the rollback test to fail after the real event
  insert:
  the same command — 21 passed in 3.12s.
- Affected PostgreSQL persistence GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence -q`
  — 93 passed in 71.64s. This includes the real run creation replay/conflict,
  command queue, event, receipt, and rollback integration paths.
- Migration/schema GREEN:
  `.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/persistence/test_schema.py -q`
  — 17 passed in 15.83s, including upgrade from `0001`, downgrade to base,
  re-upgrade, and schema checks.
- Static checks GREEN:
  `.venv\Scripts\ruff.exe check <eight touched source/test files>` — all
  checks passed; `.venv\Scripts\ruff.exe format --check <same files>` — 8
  files already formatted; `.venv\Scripts\python.exe -m mypy apps/orchestrator/src/forge`
  — success, 84 files; `git diff --check` — clean.
- Full repository GREEN:
  `.venv\Scripts\python.exe -m pytest -q` — 850 passed, 2 skipped in 88.91s.

### Decisions and concerns

- The run service keeps creation and command enqueueing inside the existing
  session-bound `PostgresUnitOfWork`; routes are not allowed to bypass these
  repositories.
- `RunSnapshot.branch_name` is optional at the end of the value object so all
  pre-Task-10 callers remain source-compatible while persisted teardown runs
  round-trip the branch binding.
- The rollback test now appends through the real event repository and raises
  immediately afterward; the UoW rollback leaves no run, event, command, or
  mutation receipt.
- HTTP request adapters, authenticated route dependencies, Host/Origin/session/
  CSRF enforcement, and API error mapping remain intentionally unimplemented
  for Slice 4.

## Candidate commits

Slice 1 candidate: `6c0afae` (`feat: add task10 persistence foundation`).
Slice 2 candidate: `75710d5` (`feat: add task10 project and task services`).
Slice 3 candidate: `bc4e1e6` (`feat: add atomic run and command services`).

## Slice 4 — authenticated HTTP API, route security, and service wiring

### Scope and implementation

Slice 4 adds the authenticated HTTP adapter for the Task 10 project, task,
run, and closed run-command services. It includes:

- Closed Pydantic request/response schemas for project registration and policy
  updates, plain-text task creation, run creation, and all seven run command
  types. External task import remains a service-only boundary.
- `GET`/`POST /api/projects`, `GET /api/projects/{project_id}`, and policy
  version creation; `GET`/`POST /api/tasks` plus task lookup; and
  `GET`/`POST /api/runs`, run lookup, and `POST
  /api/runs/{run_id}/commands`.
- A required bounded nonblank `Idempotency-Key` dependency for every Task 10
  POST. Existing Task 9 dependencies enforce exact configured Host and
  operator session for GETs, and exact Host/Origin/session/CSRF for POSTs.
- `create_app` service injection for tests and one default PostgreSQL
  session-bound service instance for each resource. Routes call application
  services only and never open repositories or transition run state.
- Safe HTTP error translation: malformed request/key and Git/path validation
  are 422, missing resources are 404, stale state/idempotency conflicts are
  409, and persistence/service failures are bounded 503 responses. Request
  validation output is generic so submitted secret/path values cannot be
  echoed in HTTP errors.

### TDD evidence

- RED before implementation:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/api/test_projects.py apps/orchestrator/tests/api/test_tasks.py apps/orchestrator/tests/api/test_runs.py -q`
  failed during fixture setup with the expected missing `create_app` service
  injection (`unexpected keyword argument 'project_service'`).
- GREEN after schemas, routes, dependency, and app wiring:
  the focused route suite passed 11 tests; after adding the real PostgreSQL
  operator-client flow it passed 12 tests.
- API regression GREEN:
  `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/api -q`
  passed 41 tests before the final route security additions; the focused
  Task 10 route suite remains the authoritative Slice 4 count and is rerun in
  final verification.

### Decisions and concerns

- HTTP responses expose policy/run/task identity and safe digests/snapshots;
  command acknowledgements expose only ID/type/status/version and never echo
  feedback or payloads.
- Request validation errors use a bounded generic detail instead of Pydantic's
  input echo. Service-boundary exception text is never interpolated into a
  response.
- `GET /api/tasks` requires a `project_id` query parameter because the
  application task repository is intentionally project-scoped; run listing
  accepts optional project/task filters.
- The pre-existing untracked API fixture is task-scoped route test setup and
  is preserved for the candidate commit.

### Candidate files

- Added `forge/api/errors.py`, `forge/api/schemas/{projects,tasks,runs}.py`,
  and `forge/api/routes/{projects,tasks,runs}.py`.
- Modified `forge/api/app.py` and `forge/api/dependencies.py`.
- Added Task 10 API boundary tests in `tests/api/test_{projects,tasks,runs}.py`
  and retained the shared route fixture.

### Final verification

- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/api/test_projects.py apps/orchestrator/tests/api/test_tasks.py apps/orchestrator/tests/api/test_runs.py -q` — **14 passed**.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/api -q` — **44 passed**.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence -q` — **93 passed** in 338.32s.
- `.venv\\Scripts\\python.exe -m pytest -q` — **864 passed, 2 skipped** in 112.78s.
- `.venv\\Scripts\\python.exe -m pytest apps/orchestrator/tests/persistence/test_schema.py -q` — **17 passed**; this covers upgrade from `0001`, head inventory, downgrade/re-upgrade, and `alembic check`.
- `.venv\\Scripts\\ruff.exe check apps/orchestrator/src/forge apps/orchestrator/tests/api` — clean.
- `.venv\\Scripts\\ruff.exe format --check apps/orchestrator/src/forge/api apps/orchestrator/tests/api` — **25 files already formatted**.
- `.venv\\Scripts\\python.exe -m mypy apps/orchestrator/src/forge` — success, **92 files**.
- `git diff --check` — clean.

The full source-tree format check also reports one pre-existing unformatted
`apps/orchestrator/src/forge/application/adapters/__init__.py` from an earlier
slice; it was left untouched to preserve unrelated work. The in-scope API
source and tests pass format checking. The containing candidate commit is
reported by the handoff below.
