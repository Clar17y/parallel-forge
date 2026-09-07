# Task17 planning evidence map

Status: implemented and tested at checkpoint `451e39b121b7dcf8fb81a9243767ceff12034abd`;
acceptance remains pending the separate Sol-high correctness disposition. This
map supports Task17 only, not Tasks18-29 or final cross-platform acceptance.

The approved requirement source is Task17 in
[the v0.1 plan](superpowers/plans/2026-08-21-forge-v0-1.md). Source paths below are
relative to `apps/orchestrator/src/forge`; test paths are repository-relative.

| Requirement | Implementation | Discriminating evidence |
| --- | --- | --- |
| Planning freezes a structured plan and waits for human approval without preparing a worktree | `application/services/planning.py`, `application/handlers/planning.py` | `tests/integration/test_worker_planning_e2e.py::test_http_plan_requires_exact_approval_before_preparation[approve]` verifies actual API bootstrap/project/task/run creation, worker planning, pending gate, usage, no premature preparation, then exactly one preparation command after authorization. |
| Deterministic context binds the immutable original task, policy, base, instructions and bounded repository content | `PlanningService` context construction; `domain/agent.py` contracts | `apps/orchestrator/tests/application/test_planning_workflow.py::test_planning_initial_execution_binds_immutable_task_and_readonly_tools`; security context tests in `test_planner_permissions.py` check environment/custom secret exclusion and path normalization. |
| Planner receives only controlled read tools and exact durable execution authority | `worker/composition.py::BoundPlanningGateway`; controlled tools | `tests/integration/test_worker_planning_e2e.py::test_composed_planner_tools_use_durable_admission` actually invokes the bound read tool with PostgreSQL admission; README succeeds and `.env` is rejected. `test_planner_permissions.py` exercises Developer/release escalation and traversal rejection. |
| Invalid output, budget failure and omitted required checks preserve evidence and require intervention | Planning result/failure settlement and accepted agent gateway contracts | `tests/integration/test_planning_failed_usage.py` checks invalid attempts, budget failure, prompt drift, usage retention and final-commit rollback; `test_planning_workflow.py::test_planning_fails_closed_when_plan_omits_policy_required_checks` asserts intervention. |
| API authorization binds current task/plan/base/policy/runner/budgets/version and spends no stale challenge | `application/services/plan_evidence.py`; default `api/app.py` validator wiring; authorization transaction | `test_worker_planning_e2e.py` authorization mutation matrix exercises actual HTTP/PostgreSQL default composition. `apps/orchestrator/tests/api/test_approvals.py::test_postgres_approval_rejects_incomplete_plan_gate_without_spending_challenge` verifies fabricated/incomplete evidence has no approval, queue, state or challenge-consumption effect. Route-only tests explicitly inject their authorization boundary. |
| Worker consumes the exact existing approval and settles owned post-authorization drift atomically | `application/handlers/approvals.py::ApprovePlanHandler` | Real workflow `drift_before_worker` refreshes base, invalidates the owned approval and produces a new plan; `wrong_actor` and `wrong_approval_policy` reject forged bindings without mutating the legitimate gate. `test_plan_approval.py` covers other exact-binding failures and no second challenge/approval creation. |
| Revision records bounded, command-bound operator feedback as untrusted input and creates a fresh semantic attempt/gate | `application/services/plan_revision.py`, `plan_restart.py`, planning feedback selection | Real workflow `revision` and `revision_twice` preserve original task, isolate feedback from system instructions, expire old challenges without spending them, retain identical-content plan provenance and produce distinct approval evidence. Unit context tests assert `UntrustedSourceKind.TASK`. |
| Queue retries cannot bypass ambiguous execution or fabricate restart authority | Semantic admission, exact restart-event/source/queued-command binding, revision crash replay | `test_plan_approval_workflow.py::test_semantic_payload_cannot_bypass_ambiguous_running_execution`; real workflow crash and completed-source mutation cases remove/null feedback or substitute queued identity. RED demonstrated an unauthorized second Planner call; repaired cases reject it while legitimate crash replay still succeeds. |
| Concurrent restart and failed event persistence do not leave partial authoritative state | `persistence/repositories/runs.py::restart_planning`, caller transaction | `test_plan_approval_workflow.py` verifies one winner under concurrent restart and rollback after event failure, including refreshed returned bindings. |
| Plan evidence identifies the result actually produced by its successful execution | `plan_evidence.py`; `persistence/repositories/executions.py::get_outcome` | `tests/integration/test_plan_evidence_provenance.py` separately tests an alternate plan with genuine evidence producer, corrupt producer identity, and corrupt parent lineage. Genuine output FK is retained in the metadata cases; immutable database triggers remain enabled. |
| Production worker actually composes the Planner, approval and revision handlers | `worker/composition.py`, `worker/main.py`, explicit pricing settings | `apps/orchestrator/tests/application/test_worker_composition.py` covers real construction, invalid configuration, exact request/tool bindings and narrow test seams; `test_worker_entrypoint.py` covers default composition/recovery behavior. The HTTP/PostgreSQL workflow exercises that composition with only the external agent gateway replaced. |

## Verification identity and limits

The final combined command selected API, approval model, settings, agents,
application, security and persistence suites plus the four planning PostgreSQL
files. It passed **1616 tests**, with two host symlink/file-link skips and six
dependency warnings. Full source/test Ruff, formatting and source mypy passed.
All pytest runs excluded `live_provider` and `live_github`.

Checks ran at HEAD `f6dd2df38fb184e6186e53a16057d4dba6d169f0` with complete
uncommitted identity recorded before and after execution; those identities were
equal. The tested staged tree `0f9cad50c9717beee6c125e5a992bca917fb5088` is exactly
the tree of implementation commit `451e39b121b7dcf8fb81a9243767ceff12034abd`.
Raw commands/results/manifests are retained locally under
`.llm-output/runs/task17-integration/frozen-final-candidate/`. This is an explicit
candidate-to-commit mapping, not a claim of execution on a later documentation HEAD.

One integrated independent review found A17-C01 and A17-C02; both were repaired
and closed by one finding-specific recheck. Review output is not test evidence.
The separate Sol-high gate is pending. Preparation command execution and the
complete local delivery/review/remediation workflow belong to Task18. Live
provider checks and final cross-platform/whole-product acceptance remain open.
