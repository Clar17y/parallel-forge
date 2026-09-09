# v0.1 acceptance evidence map

Status on 9 September 2026: **deterministic v0.1 acceptance passed**.
Implementation through Task 29 and required independent review repairs are
complete. The user-authorized full run34410330735 passed on candidate
`4d42e0d89ec12a243fc88a8956dacc89f77e13df` on `forge/v0-1`.
This record's subsequent changes are documentation only. No PR merge, live
provider/GitHub acceptance, deployment, or further full CI run is authorized.
The [progress ledger](v0.1-progress.md) retains task checkpoints, reproduced
failures, repair dispositions and exact verification evidence.

The browser scenarios use separate local API/worker processes, PostgreSQL and
deterministic fake agents/GitHub on the hosted runner. Component tests supplement
that flow with adverse authorization, budget, concurrency and recovery cases.
Live model and live GitHub writes are credential-gated opt-ins, not claimed here.

## Design section 23 mapping

| Item | Requirement | Executable evidence |
| --- | --- | --- |
| 1 | Register repository and versioned policy | `apps/orchestrator/tests/application/test_project_task_services.py`;  hosted browser registration at5a50018. |
| 2 | Create plain-text task | `apps/orchestrator/tests/application/test_project_task_services.py`;  hosted browser task creation. Configured issue import is optional. |
| 3 | Structured plan and human approval | `test_worker_planning_e2e.py`; real API/worker/browser plan approval. |
| 4 | Managed worktree and optional database | `test_worker_delivery_e2e.py`;13-step process acceptance provisions a real worktree/database. |
| 5 | Developer confined to managed worktree | `tests/integration/test_worker_delivery_e2e.py`, `apps/orchestrator/tests/security/test_repository_escape.py` and `apps/orchestrator/tests/tools/test_paths.py`. |
| 6 | Commit and inspectable diff | `apps/orchestrator/tests/tools/test_git.py` and `tests/integration/test_worker_delivery_e2e.py`; browser candidate/Changes navigation with bound approval evidence. |
| 7 | Exact named required checks | `tests/integration/test_delivery_validation.py`; process acceptance; independent Linux Docker/staged-worktree smoke. |
| 8 | Independent Reviewer and findings | `tests/integration/test_review_decision.py` and `tests/integration/test_dashboard_projection.py`; real process review and browser Review tab/approval evidence. |
| 9 | Bounded local remediation |`tests/integration/test_vertical_slice.py` and `tests/integration/test_delivery_remediation.py`. |
| 10 | Live state and evidence views | `apps/orchestrator/tests/api/test_sse.py` and `tests/integration/test_dashboard_projection.py`; browser cockpit tabs, usage, keyboard and accessibility checks. |
| 11 | Approval before PR writes | `tests/integration/test_pr_approval.py` and `tests/integration/test_pr_publication.py`; browser exact PR approval. |
| 12 | Controlled push and PR reconciliation | `tests/integration/test_vertical_slice.py` and `tests/integration/test_worker_restart.py`. |
| 13 | CI/review monitoring and bounded remote remediation |`tests/integration/test_vertical_slice.py` exercises failed-CI/remediation/green recovery. |
| 14 | Exact green head/base merge gate | `tests/integration/test_merge_approval.py`; browser bound merge evidence/dialog; scoped release correctness gates. |
| 15 | Fresh authorization, expected head and protected base | `test_release_race_flow.py` actual head/base races; direct/queue mode integration and stale/forged approval suites. |
| 16 | Restart, pause and cancellation | `tests/integration/test_run_controls.py`, `tests/integration/test_resume_run.py` and `tests/integration/test_worker_restart.py`; browser restart/cancellation. |
| 17 | Retention and explicit teardown |`tests/integration/test_vertical_slice.py` and `tests/integration/test_worker_restart.py`; browser confirmation and retained resources. |
| 18 | Required deterministic test suites | Current full backend CI below, both-platform static/unit/web checks, migrations/evaluations/security, Docker and Chromium evidence. |

These map the approved requirements to composed evidence; a browser pass alone
is not a substitute for security/recovery tests. Historical failed full runs are
not counted as complete passes. The final backend and Docker results are recorded below.

## Verification record

| Verification | Candidate and result |
| --- | --- |
| Full backend, migrations and evaluations | [Run34410330735](https://github.com/Clar17y/parallel-forge/actions/runs/34410330735), job102663009109 at4d42e0d: **3806passed,79skipped,4deselected** in1601.79s. |
| Docker runner contract | Same job: **3passed** in29.13s; actual Docker step succeeded, with no skips. |
| Ubuntu contract/static checks | Same run, job102663009486:395passed7skipped10.50s; Ruff and strict mypy290sources passed. |
| Windows contract/static checks | Same run, job102663009482:400passed2skipped22.12s; Ruff and strict mypy290sources passed. |
| Hosted secret scan | Same run, job102663009470 passed. Exact4d42e0d git-archive scan also passed with verified Gitleaks8.30.1. |
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
- The POSIX same-UID metadata-rename test is explicitly Windows-only. The
  linked-registration-quarantine symlink fixture also skipped on this hosted
  run; no execution of that individual case is claimed. Related repository
  escape, path/link and actual Docker boundary tests provide separate evidence.
- Hosted Ubuntu lacks `rg`; the actual-ripgrep search contract passed locally.
  The privileged host bind-mount test requires mount capability unavailable to
  the ordinary hosted process; actual Docker mount guards and related path
  boundary tests have separate passing evidence. No privileged run is claimed.
- Web/API schema and Docker inputs are unchanged from their cited passing
  candidates. Evaluation-only worker changes do not affect browser delivery
  scenarios. The recovery harness deadline changed atfc3d09a, so its focused
  Linux result and final full backend pass supplement the prior22-case proof.
- The new baseline migration is covered by focused existing-data round trips
  and the final full backend pass at4d42e0d. Documentation-only checkpoints do not invalidate
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
