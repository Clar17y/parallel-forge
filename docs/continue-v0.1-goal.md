# Continue Forge v0.1 with a persistent goal

Use the following as a user prompt in a new task for `C:\dev\parallel-forge`.
This uses Codex's built-in persistent goal capability; no separately installed
`goal` skill is required. Do not start executing it merely because this document
is encountered during repository inspection.

---

Create a persistent goal to complete the existing Forge v0.1 implementation plan
from its actual verified state through the complete deterministic acceptance
gate. Do not set a token budget unless I explicitly supply one. Keep my selected
primary model. Work autonomously within the boundaries below until the goal is
complete or a concrete external dependency requires my action.

## Establish the current state

Start in `C:\dev\parallel-forge`, fetch and inspect `origin/forge/v0-1`, preserve
unrelated work, and read:

- `AGENTS.md` and `C:/Users/sdyer/.codex/AGENTS.md`;
- `C:/Users/sdyer/.codex/agent-framework/README.md` and `routing.json`;
- `docs/superpowers/plans/2026-08-21-forge-v0-1.md`;
- `docs/superpowers/specs/2026-08-21-forge-v0-1-design.md`;
- the Task 13 lifecycle plans/spec, architecture, threat model, and ADRs;
- `docs/recovery-environment.md` and any newer progress ledger/evidence.

Create a fresh `codex/` worktree from the updated `forge/v0-1` using the repository
setup script and registered project policy. Verify the resulting manifest and
resources. Do not write implementation files in the canonical checkout. The
recovery worktrees were temporary and may have been removed; bootstrap the CLI
with Python 3.14 and the locked dependencies if needed. Keep its control-database
connection distinct from any isolated worktree database. Use Node 24.x only.

Reconstruct a tracked, evidence-backed task ledger rather than trusting the
stale README or unchecked plan boxes. At recovery time, Tasks 1-13 had substantial
implementation; Task 14 still had unavailable commit/check/evidence tools;
Tasks 15-16 had code but lacked their committed agent tests; Task 17 had a durable
planning service but missing approval/revision handlers and production worker
wiring. Tasks 18-29 remained largely unfinished. Verify all of this against the
current checkout before choosing work.

The recovery baseline was 1,530 passing tests, 57 skips and 92 failures (88
state-engine, 3 run-service, 1 schema). First reproduce and resolve the failures
against the approved contracts. Do not weaken tests or safety rules just to make
them green, and do not assume failures mean the tests are stale. Recover missing
verification coverage and review evidence. Mark completed work only when its
implementation, tests and required reviews support that claim.

## Use the installed multi-provider framework

Use this framework instead of the plan's legacy superpowers execution routing;
retain the plan's functionality, task dependencies, TDD and safety requirements.
The primary owns architecture, decomposition, integration and final synthesis.

- Routine implementation goes through `ask-gemini` FIRST using `agy` and exactly
  the configured Gemini 3.8 Flash medium model. Only adapter exit 20 with
  `fallback_required` permits a fresh Luna-medium implementer. Preserve partial
  work and confirm the prior writer/session stopped before handing off. Follow
  framework pending-session recovery; authentication, permissions, missing
  models, timeouts and transient network failures are not quota exhaustion.
- Route complex bounded implementation to Terra-low `complex_implementer`;
  planning/adversarial tests to Sol-low `planner`/`test_engineer`; exploration,
  documentation, refactor audit and verification to Luna medium.
- Independent review uses `ask-claude`, fresh context, exactly Claude Opus 5,
  with read/search only: no shell, edits, MCP or subagents. If unavailable, use
  a fresh Astra-low reviewer and disclose the fallback. Never Sonnet or Fable.
- Run a separate Sol-high correctness gate after repairs for concurrency,
  cancellation, timeouts, security, migrations, data integrity and release work.
  Use Sol-high security review for explicit security analysis.
- Use fresh verifier evidence tied to candidate HEAD and uncommitted diff
  identity. Supply reviewers the base and saved integrated diff. Distinguish
  task review from whole-branch review and historical results from fresh checks.
- Delegate independent bounded contracts with non-overlapping owned files,
  acceptance criteria and validation. Subagents may not delegate unless you
  explicitly authorize it. Respect the host's concurrency limit. Keep contracts,
  logs and partial results in the active worktree's `.llm-output/`.
- Follow installed framework updates when its routing changes. Provider CLI
  bypass/yolo authorization does not bypass Codex sandbox or approval review.

## Execute and preserve progress

Close earlier incomplete tasks before advancing their dependants, then complete
the remaining tasks through Task 29. Use focused red-green TDD, the affected
suite, independent reviews and required gates for each bounded slice. Keep
PostgreSQL authoritative, API and worker separate, agents behind Forge-owned
interfaces, named controlled tools, confined worktrees, Docker by default and
evidence-bound human approval gates. Implement the actual end-to-end workflow,
REST/SSE, dashboard, deterministic GitHub controller, evaluations, recovery,
cross-platform CI and operator documentation specified by the plan.

Make small checkpoint commits after verified slices. You may push implementation
branches and create/update reviewable PRs targeting `forge/v0-1` to preserve work
remotely. This prompt does not authorize merging future PRs: request immediate,
explicit human authorization for each merge after presenting exact evidence.
Continue independent authorized work while waiting where possible. Never delete
unmerged work or rewrite another agent's changes. Archive local evidence before
any explicitly authorized teardown; keep branches by default.

Keep the tracked ledger current with task status, commits, review verdicts,
verification commands/results, skip reasons, remaining gaps and the exact next
action so another session can resume after a crash. Preserve state across
compaction. Report concrete blockers and provider failures; do not silently
switch routes, weaken requirements, spin on unchanged blockers, or claim partial
progress as completion. Do not purchase credits, consume usage resets, run paid
provider/live-GitHub acceptance tests, or deploy externally without specific
authorization beyond the development-provider routing already granted.

## Completion criteria

All 29 plan tasks and design acceptance criteria are implemented and mapped to
current automated evidence. Deterministic unit, PostgreSQL integration, security,
evaluation, REST/SSE, UI and end-to-end acceptance suites pass; lint, type checks,
builds, migration checks and required cross-platform CI pass. Required independent
task and final branch reviews have no unresolved blockers/major findings.
Documentation and the progress ledger describe the actual final state. Report
remaining opt-in live checks separately. Mark the persistent goal complete only
when implementation and all required verification are truly complete; keep any
pending human merge authorization explicit.
