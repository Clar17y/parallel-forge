# v0.1 acceptance evidence map

Status on 9 September 2026: **not accepted**. This maps design section 23 to
existing automated entry points and remaining proof. Test names identify coverage
to inspect and execute; their presence is not a passing result. Candidate-specific
results and reviews are recorded in [the progress ledger](v0.1-progress.md).

The design requires all items to work locally through the dashboard. Existing
ASGI/composed-worker tests are component integration evidence; they do not replace
separate API/worker processes, browser interaction, accessibility or restart tests.

| Item | Requirement | Existing automated entry point | Remaining acceptance proof |
| --- | --- | --- | --- |
| 1 | Register a repository and versioned policy | `apps/orchestrator/tests/api/test_projects.py::test_projects_list_and_registration_use_injected_service`; project/task service tests | Browser registration against real persistence and local repository validation |
| 2 | Create a plain-text task | `apps/orchestrator/tests/api/test_tasks.py::test_tasks_list_create_and_get_use_injected_service` | Browser task creation through the running API; optional configured issue import |
| 3 | Plan, then wait for human approval | `tests/integration/test_worker_planning_e2e.py::test_http_plan_requires_exact_approval_before_preparation` | The same gate in a browser with separate worker/API processes |
| 4 | Isolated worktree and optional database | `tests/integration/test_worker_delivery_e2e.py::test_http_approved_delivery_reaches_pr_gate_with_real_worktree_and_check` | Full dashboard flow and both host lifecycle smoke results |
| 5 | Developer confined to managed worktree | Same delivery flow; `apps/orchestrator/tests/security` and controlled-tool suites | Current cross-platform confinement results and process-level denied-tool case |
| 6 | Local commit and inspectable diff | Same delivery flow and controlled Git tests | Browser-visible exact commit/diff from the accepted candidate |
| 7 | Execute named required checks | `tests/integration/test_delivery_validation.py::test_required_checks_have_committed_intents_and_publish_in_policy_order` | Browser check evidence; focused hosted Docker smoke passed at `a978454` (164 passed, 46 platform skips); Linux cancellation correctness gate closed |
| 8 | Independent Reviewer and structured findings | `tests/integration/test_dashboard_projection.py::test_cockpit_contains_actual_checks_independent_review_and_pull_request` | Visible independent execution and findings in browser acceptance |
| 9 | Bounded local remediation | `tests/integration/test_delivery_remediation.py::test_failed_validation_runs_fresh_developer_remediation_and_revalidates` | Complete default-budget cycle and exhaustion through dashboard |
| 10 | Live state, artifacts, checks, findings and usage | Dashboard projection tests, API SSE tests and web component tests | Running REST/SSE reconnect and browser artifact/usage navigation |
| 11 | Approval before PR writes | `tests/integration/test_pr_approval.py::test_stale_pr_approval_never_publishes` | Browser gate with zero observed pre-approval remote writes |
| 12 | Controlled push and PR reconciliation | `tests/integration/test_pr_publication.py::test_crash_after_pr_creation_reconciles_once_before_monitoring` | Complete fake-GitHub process acceptance and release repair confirmation |
| 13 | CI/review monitoring and bounded remote remediation | `tests/integration/test_remote_development.py::test_remote_failure_reaches_developer_as_untrusted_evidence_then_validation` | Failed CI through recovery and budget exhaustion in full flow |
| 14 | Merge approval for exact green head/base | `tests/integration/test_merge_approval.py::test_merge_approval_consumes_exact_gate_or_invalidates_without_merge` | Browser rendering of the frozen evidence and scoped correctness gate |
| 15 | Fresh human approval, expected head and protected base | Merge approval/controller/security suites; `tests/integration/test_merge_queue_mode.py::test_consumed_merge_mode_comes_from_approved_observation` | Direct and queued merge process acceptance and stale-head/base cases; scoped release repair gates closed at `1505679`, final candidate acceptance remains open |
| 16 | Restart, pause and cancellation | `tests/integration/test_run_controls.py::test_pause_retains_exact_approval_suspension_context_and_replays_once`; release resume and resource recovery suites | Actual process restarts at required crash points and final security/recovery gate |
| 17 | Retain resources; explicit teardown | `tests/integration/test_run_controls.py::test_cancel_retains_managed_resource_identity_without_successor_dispatch`; `apps/orchestrator/tests/application/test_teardown_handler.py::test_teardown_persists_admission_before_effect_and_completion_afterward`; real process vertical teardown at `d4282f3` | Browser teardown and final filesystem gate; quiescent intervention admission repaired at `8ae3a1a`, actual intervention-resource process proof remains open |
| 18 | All required test suites | Python/runtime and web contract workflows; evaluation metrics/fixtures | Current full deterministic suite, evaluations CLI, Playwright, accessibility, secret/generated-file checks and complete review evidence |

## Completed focused hosted acceptance

Chromium run34371063844/job102532101737 at `5a50018` passed all three real
API/worker browser scenarios: registration/task creation and the three exact
approvals; keyboard navigation and accessibility across dialogs; persisted cancel
across restart, retained resources and explicit teardown. Three browser-free
bridge contracts passed in the same job. Supervisor startup/recovery/shutdown
passed at `140fb43` (run34365654124/job102513567026); Linux Docker smoke and its
cancellation gate passed at `a978454`. These close the corresponding focused
proofs above; full integrated backend/web CI, recovery matrix and final independent
review still determine acceptance.

## Final verification record

No final accepted candidate is recorded yet. The final record must include the
exact commit, commands, configuration, platform results, skips with their coverage
elsewhere, and required independent review dispositions. It must cover:

- PostgreSQL migration upgrade/downgrade and the complete deterministic backend;
- Ruff and strict mypy on Windows and Linux;
- web tests, lint, types, build and OpenAPI regeneration on both platforms;
- Docker runner and Bash lifecycle smoke on Linux, PowerShell lifecycle on Windows;
- deterministic evaluation execution and version-bound regression thresholds;
- Playwright Chromium, keyboard navigation and accessibility checks;
- the complete dashboard workflow through three approvals and fake GitHub effects;
- restart/pause/cancel/teardown and the security cases in design section 22;
- secret-pattern and generated-file cleanliness checks.

Live model/GitHub checks require explicit credential-gated opt-in and are not
claimed by deterministic test results. No development PR merge is authorized by
this document or by a green CI run.

Full Python/runtime and web CI run on pull requests and explicit manual dispatch.
Checkpoint pushes preserve work without rerunning those suites. The separate
Linux Docker smoke workflow has no full-backend prerequisite and supports manual
dispatch and narrowly filtered Docker-related pushes. Run full required CI on
the integrated candidate; repeat it only for relevant changes or failures.
