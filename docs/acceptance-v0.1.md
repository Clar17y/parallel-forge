# v0.1 acceptance evidence map

Status on 9 September 2026: **not yet accepted**. Implementation and required
independent review repairs are complete; final full CI acceptance is pending
explicit user authorization for a specific run. No full dispatch or rerun is
authorized, including after a failure. Focused checks and remote checkpoints
remain authorized; they do not waive the final full gate.
Candidate: `fc3d09ae8dc885f184a5c709f895eaa5045e2514` on `forge/v0-1`.
The [progress ledger](v0.1-progress.md) retains task checkpoints, reproduced
failures, repair dispositions and exact verification evidence.

The browser scenarios use separate local API/worker processes, PostgreSQL and
deterministic fake agents/GitHub on the hosted runner. Component tests supplement
that flow with adverse authorization, budget, concurrency and recovery cases.
Live model and live GitHub writes are credential-gated opt-ins, not claimed here.

## Design section 23 mapping

| Item | Requirement | Executable evidence |
| --- | --- | --- |
| 1 | Register repository and versioned policy | Project API/service suites; hosted browser registration at5a50018. |
| 2 | Create plain-text task | Task API/service suites; hosted browser task creation. Configured issue import is optional. |
| 3 | Structured plan and human approval | `test_worker_planning_e2e.py`; real API/worker/browser plan approval. |
| 4 | Managed worktree and optional database | `test_worker_delivery_e2e.py`;13-step process acceptance provisions a real worktree/database. |
| 5 | Developer confined to managed worktree | Delivery process flow, controlled-tool authorization, repository escape and Windows/POSIX path suites. |
| 6 | Commit and inspectable diff | Controlled Git and delivery suites; browser candidate/Changes navigation with bound approval evidence. |
| 7 | Exact named required checks | Delivery validation suites; process acceptance; independent Linux Docker/staged-worktree smoke. |
| 8 | Independent Reviewer and findings | Review decision/projection suites; real process review and browser Review tab/approval evidence. |
| 9 | Bounded local remediation |13-step process failed-validation/remediation flow; delivery decision, remediation and review budget suites. |
| 10 | Live state and evidence views | REST/SSE and dashboard projection suites; browser cockpit tabs, usage, keyboard and accessibility checks. |
| 11 | Approval before PR writes | PR approval/publication security suites; browser exact PR approval. |
| 12 | Controlled push and PR reconciliation | Process delivery and complete publication crash/restart matrix. |
| 13 | CI/review monitoring and bounded remote remediation |13-step process failed-CI/remediation/green recovery; remote-development and monitoring budget suites. |
| 14 | Exact green head/base merge gate | Merge approval/protection suites; browser bound merge evidence/dialog; scoped release correctness gates. |
| 15 | Fresh authorization, expected head and protected base | `test_release_race_flow.py` actual head/base races; direct/queue mode integration and stale/forged approval suites. |
| 16 | Restart, pause and cancellation | Run-control/resume suites, complete actual-process crash matrix, browser restart/cancellation. |
| 17 | Retention and explicit teardown |13-step process retention/teardown, browser confirmation and retained resources, complete teardown crash matrix. |
| 18 | Required deterministic test suites | Current full backend CI below, both-platform static/unit/web checks, migrations/evaluations/security, Docker and Chromium evidence. |

These map the approved requirements to composed evidence; a browser pass alone
is not a substitute for security/recovery tests. Historical failed full runs are
not counted as complete passes. The current full backend result remains required.

## Verification record

| Verification | Candidate and result |
| --- | --- |
| Full backend, migrations, evaluations and Docker | **Pending authorization**. Cancelled [run34389229107](https://github.com/Clar17y/parallel-forge/actions/runs/34389229107) atfc3d09a; its backend is not acceptance evidence. Completed jobs below remain preserved. |
| Ubuntu contract/static checks | Same run, job102593140549:395passed7skipped10.01s; Ruff and strict mypy290sources passed. |
| Windows contract/static checks | Same run, job102593140571:400passed2skipped21.36s; Ruff and strict mypy290sources passed. |
| Hosted secret scan | Same run, job102593140692 passed. Exactfc3d09a git-archive scan also passed with verified Gitleaks8.30.1. |
| Web on Windows and Ubuntu | [Run34374162224](https://github.com/Clar17y/parallel-forge/actions/runs/34374162224) at1f43755:102tests on each platform, lint, types, production build and generated contracts passed. |
| Chromium workflow/accessibility | [Run34371063844](https://github.com/Clar17y/parallel-forge/actions/runs/34371063844), job102532101737 at5a50018:3browser scenarios and3browser-free bridge contracts passed. |
| Development supervisor | [Run34365654124](https://github.com/Clar17y/parallel-forge/actions/runs/34365654124), job102513567026 at140fb43: install/migrate/bootstrap/API/web/worker readiness and owned-process shutdown passed. |
| Independent Linux Docker smoke | [Run34367367831](https://github.com/Clar17y/parallel-forge/actions/runs/34367367831), job102519460835 ata978454:164passed46platformskips38.12s; Windows counterpart206passed24skipped. |
| Complete actual-process recovery matrix | Independent Windows verification at5a50018 with test inputs checkpointed9d272d8:22passed669.59s, matching before/after input hashes. |
| Recovery deadline repair | [Focused run34389034564](https://github.com/Clar17y/parallel-forge/actions/runs/34389034564), job102592496941 atfc3d09a:5passed49.59s. Exact crash-exit and recovery assertions retained;90s bound accounts for30s owner expiry plus startup. |
| Baseline migration/CLI/logging repairs | Combined42-test Windows batch at4c8f716:42passed56.76s, including existing immutable snapshots across upgrade/downgrade/re-upgrade and logging order. |
| Additional Windows skip counterparts | Current environment/secret/manifest subset117passed24skipped68.36s; mocked failed-spawn job-handle regression1passed0.07s; real-rg search passed locally. |

The final full command is `python -m pytest -q -ra -m "not live_provider and
not live_github and not docker"`, followed by the Docker integration module.
Runtime versions are Python3.14.5, Node24.20.0 and PostgreSQL17; runner base images
remain pinned to the approved immutable Python3.14/Node24 digests. Local noisy
checks use `rtk proxy`; hosted checks use the frozen uv environment.

## Skip accounting and evidence reuse

- Ubuntu contract skips are Windows handle/PowerShell cases; Windows contract
  skips are Bash cases. The counterpart platform passed them.
- Full Linux platform skips for Windows path, environment, secrets, manifest,
  artifact and job-handle behavior have Windows contract/focused coverage.
  Windows POSIX and unavailable-link skips have Linux coverage; Windows junction
  and native path cases cover its supported reparse-point behavior.
- Hosted Ubuntu lacks `rg`; the actual-ripgrep search contract passed locally.
  The privileged host bind-mount test requires mount capability unavailable to
  the ordinary hosted process; actual Docker mount guards and related path
  boundary tests have separate passing evidence. No privileged run is claimed.
- Web/API schema and Docker inputs are unchanged from their cited passing
  candidates. Evaluation-only worker changes do not affect browser delivery
  scenarios. The recovery harness deadline changed atfc3d09a, so its focused
  Linux result supplements the prior22-case proof; full acceptance remains pending.
- The new baseline migration is covered by focused existing-data round trips
  and passing cases in run34385418384 (3802 passed, one recovery timing failure).
  That failed run is not full acceptance. Documentation-only checkpoints do not invalidate
  executable evidence; the final committed documentation is scanned separately.

## Independent dispositions and release boundary

Tasks1–23 were accepted at67e1a48. Release/queue findings and their scoped Sol gate
closed at1505679. Linux mount/cancellation findings and scoped correctness closed
ata978454. Final integrated Claude Opus5medium was attempted but unavailable due
to quota; the authorized fresh Astra-low fallback reviewed the integrated batch,
using prior accepted task reviews. FCR001–003 repairs closed at9797b7f.

The separate Sol-high gate closed repeated evaluation cancellation ECAN001 at
03c3ba3. Another bounded scoped disposition passed the baseline schema migration
at4c8f716. Test-only timing/formatting/isolation and minimal workflow adjustments
used focused checks and primary self-review. No unresolved material review
finding is waived. Review output is not substituted for test results.

Full Python and web CI run on PRs and manual dispatch, not checkpoint pushes.
The assistant must obtain explicit authorization for each specific full CI run
or rerun, first explaining its necessity and why focused verification is
insufficient. It must not trigger full CI indirectly through a PR without that
authorization. A passing focused check or repair does not authorize a full run.
The Docker workflow is independently runnable. Python manual recovery mode is
explicitly focused and does not count as a full acceptance run; PRs and default
manual dispatch retain every full gate. Superseded/failed runs and their useful
completed evidence are recorded in the ledger. No PR merge or live application
model/GitHub acceptance is authorized by a green run or this document.
