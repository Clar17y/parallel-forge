# Forge v0.1 Design

Status: Approved design, awaiting written-spec review  
Date: 2026-08-21  
Product: Forge  
Scope: Smallest useful local-first vertical slice

## 1. Summary

Forge is an agentic software-engineering control plane. It accepts a real
engineering task, coordinates specialist AI agents, enforces explicit quality
and security gates, and gives a human operator control over implementation,
publishing, and merging.

Forge v0.1 is a local-first, repository-agnostic modular monolith:

- a Python FastAPI control API and orchestration worker;
- a Next.js and TypeScript operator dashboard;
- PostgreSQL as the durable system of record from the first release;
- Google Agent Development Kit behind an internal agent adapter;
- controlled repository, Git, build, and GitHub interfaces;
- isolated Git worktrees and per-worktree databases;
- a deterministic Release Controller for remote GitHub actions;
- OpenTelemetry-compatible tracing and persisted usage data.

The first target repository is Parallel, but no Parallel-specific behavior may
be embedded in Forge's domain or application layers.

## 2. Product outcome

Given a registered local Git repository and a plain-text task or GitHub issue,
an operator can use the dashboard to:

1. inspect the task and generated implementation plan;
2. approve or request revision of the plan;
3. let Forge prepare an isolated worktree and database;
4. let a Developer agent implement the approved plan;
5. run configured checks and an independent Reviewer agent;
6. let Forge remediate validation or review failures within a bounded policy;
7. inspect the complete diff, checks, findings, activity, and usage;
8. approve publishing a pull request;
9. let Forge create and update that pull request while attempting to make CI
   green;
10. approve a merge only for an exact, verified remote commit;
11. pause, cancel, resume after restart, or explicitly tear down retained
    resources.

Forge is not a chatbot that happens to run commands. It is a stateful workflow
system whose model-driven components operate inside deterministic boundaries.

## 3. Goals

- Deliver a working end-to-end development and GitHub pull-request lifecycle.
- Keep humans in control at the plan, publish, and merge boundaries.
- Make the local validation and approved PR-remediation loop meaningfully
  autonomous.
- Persist state, commands, artifacts, approvals, actions, and usage from day
  one.
- Apply least privilege independently to Planner, Developer, and Reviewer
  agents.
- Make every consequential action attributable and auditable.
- Recover safely from process restarts and duplicate command delivery.
- Support repository-specific checks without embedding repository-specific
  logic in Forge.
- Establish security boundaries that can later support private repositories
  and hosted deployment.

## 4. Non-goals for v0.1

- GitLab or Bitbucket support.
- Multi-tenant SaaS hosting or organization administration.
- Distributed worker fleets.
- Kubernetes, Kafka, Terraform, or GCP deployment.
- A knowledge graph or general-purpose retrieval platform.
- Parallel developer agents or speculative competing implementations.
- A plugin marketplace or custom MCP services.
- Automatic merging without an immediate human approval.
- Arbitrary shell access from the dashboard or unrestricted shell tools for
  agents.
- Guaranteed containment of hostile code when the operator explicitly selects
  the unsandboxed host runner.

The architecture leaves seams for these capabilities, but v0.1 does not create
empty infrastructure or abstraction layers for them.

## 5. Key decisions

| Area | Decision |
| --- | --- |
| Shape | Local-first modular monolith |
| Backend | Python 3.14, FastAPI, one API process and one worker process |
| Agent framework | Google ADK behind a Forge-owned adapter |
| Dashboard | Next.js and TypeScript |
| Persistence | PostgreSQL from the first slice |
| Live updates | Server-Sent Events |
| Work queue | Durable PostgreSQL commands and leases |
| Git isolation | Managed worktree and branch per run |
| Build isolation | Docker sandbox by default; explicit trusted host runner fallback |
| Remote actions | Deterministic Release Controller, never an LLM agent |
| GitHub monitoring | Polling in v0.1; webhook seam retained |
| Artifact storage | Local content-addressed storage plus PostgreSQL metadata |
| Tool protocol | Internal typed interfaces first; MCP extraction only when useful |
| Merge policy | Exact-head, green-check, immediate human approval |

## 6. Trust model and terminology

### 6.1 Human operator

The operator configures projects and policies, approves plans, authorizes PR
publishing, authorizes merges, and may pause, cancel, or tear down runs.

### 6.2 Model-driven agents

Planner, Developer, and Reviewer are probabilistic components. Their output is
untrusted until validated. They receive only role-appropriate tools and never
receive GitHub write credentials.

### 6.3 Orchestrator

The orchestrator is Forge-owned application code. It validates state
transitions, dispatches agents and controlled tools, enforces policies and
budgets, persists evidence, and decides when human input is required.

### 6.4 Release Controller

The Release Controller is deterministic application code inside the
orchestrator boundary. It alone can invoke constrained remote-write operations:

- push a managed branch;
- create or update the managed pull request;
- merge the pull request after exact approval.

It is not an agent and does not interpret repository prose. Every operation
requires a valid workflow state, matching approval evidence, and an allowed
target repository and branch.

### 6.5 Repository content and tasks

Issue descriptions, repository files, generated source, test output, and review
comments are untrusted inputs. They may inform agent work but cannot change
Forge policy, tool permissions, credentials, approval requirements, or system
instructions.

## 7. Architecture

~~~text
Browser
  |
  v
Next.js dashboard
  |
  | REST commands and queries
  | Server-Sent Events
  v
FastAPI control API --------------------+
  |                                    |
  v                                    v
PostgreSQL                       Artifact metadata
  |                              and local CAS
  | durable commands, events,
  | leases, state and approvals
  v
Orchestrator worker
  |
  +--> Agent adapter --> Planner / Developer / Reviewer
  |
  +--> Controlled tool layer
  |      +--> repository reader/writer
  |      +--> worktree and local Git
  |      +--> sandboxed named checks
  |
  +--> Release Controller
         +--> constrained Git push
         +--> GitHub PR API
         +--> GitHub checks/reviews polling
         +--> exact-head merge
~~~

The dashboard contains presentation and interaction logic only. It cannot call
GitHub, model providers, the filesystem, or Git directly. Workflow decisions
remain in the Python application layer.

The API and worker may run in one process during early development, but their
interfaces and database-backed command boundary remain separate so they can be
split without changing the domain model.

## 8. Repository structure

~~~text
forge/
├── apps/
│   ├── orchestrator/
│   │   ├── src/forge/
│   │   │   ├── domain/
│   │   │   ├── application/
│   │   │   ├── persistence/
│   │   │   ├── agents/
│   │   │   ├── tools/
│   │   │   ├── observability/
│   │   │   └── cli/
│   │   └── tests/
│   └── web/
├── agents/
│   ├── planner/instructions.md
│   ├── developer/instructions.md
│   └── reviewer/instructions.md
├── scripts/
│   ├── setup-worktree.ps1
│   ├── setup-worktree.sh
│   ├── teardown-worktree.ps1
│   └── teardown-worktree.sh
├── tests/
│   ├── integration/
│   └── evaluations/
├── docs/
│   ├── architecture.md
│   ├── threat-model.md
│   ├── adr/
│   └── superpowers/specs/
├── docker-compose.yml
└── pyproject.toml
~~~

No mcp or infrastructure directories are created until they contain a real
boundary or deployable resource.

### 8.1 Module dependency rule

- Domain contains state, policy, and value types and depends on no framework.
- Application contains use cases and ports and depends only on Domain.
- Persistence, agents, tools, observability, and API/CLI code are adapters.
- Adapters may depend inward; Domain and Application never depend outward.
- Agent prompts are versioned inputs, not embedded orchestration logic.

FastAPI publishes the control-plane OpenAPI contract. The web application
generates or derives TypeScript API types from that contract rather than
maintaining a second handwritten schema.

## 9. Workflow and state machine

### 9.1 Primary states

| State | Meaning |
| --- | --- |
| CREATED | Task is recorded but planning has not started |
| PLANNING | Planner is inspecting context and producing a plan |
| AWAITING_PLAN_APPROVAL | A plan artifact is frozen for human decision |
| PREPARING_WORKTREE | Forge is creating isolated local resources |
| IMPLEMENTING | Developer is changing the managed worktree |
| VALIDATING | Configured checks are running |
| REVIEWING | Independent Reviewer is assessing task, plan, diff, and results |
| REMEDIATING | Developer is addressing local or remote findings |
| AWAITING_PR_APPROVAL | Candidate commit and evidence are frozen for publication |
| PUBLISHING_PR | Release Controller is pushing and creating/reconciling the PR |
| MONITORING_PR | Forge is monitoring checks and review feedback |
| AWAITING_HUMAN_INTERVENTION | Policy, safety, scope, or retry boundary needs a decision |
| AWAITING_MERGE_APPROVAL | Remote head and green evidence are frozen for merge decision |
| MERGING | Release Controller is rechecking and performing the approved merge |
| PAUSED | Operator has paused dispatch; the previous active state is retained |
| COMPLETED | Approved merge has completed, or a configured local-only run ended |
| FAILED | An unrecoverable system failure has been recorded |
| CANCELLED | Operator cancelled the run |

Worktree/database teardown is a separate resource-lifecycle command. Completion
or cancellation never destroys evidence or local resources automatically.

### 9.2 Required gates

#### Plan gate

Plan approval is bound to:

- normalized plan document and digest;
- task version;
- base repository and base commit;
- project policy version;
- permitted checks and dependency changes;
- autonomy, usage, cost, and remediation budgets.

Approval authorizes local implementation only. Any material plan, base, policy,
or budget change invalidates it.

#### PR publication gate

PR approval is bound to:

- candidate local commit;
- normalized diff digest;
- validation results;
- independent review decision;
- target repository, base branch, and proposed title/body;
- remote-remediation policy, defaulting to at most three cycles.

Approval authorizes the Release Controller to publish that candidate and push
bounded remediation commits for the same approved task. It does not authorize a
merge. Scope expansion, an unapproved dependency, a security concern, a policy
change, or budget exhaustion immediately returns control to the operator.

#### Merge gate

Merge approval is bound to:

- GitHub repository and PR number;
- exact remote head commit;
- base branch;
- required checks and their successful conclusions;
- absence of unresolved blocking review findings;
- merge method;
- current project policy version.

Any remote-head, check-set, review, base, method, or policy change invalidates
the approval. Immediately before merging, the Release Controller retrieves the
remote state again and compares it with the approval evidence.

### 9.3 Normal flow

~~~text
CREATED
  -> PLANNING
  -> AWAITING_PLAN_APPROVAL
  -> PREPARING_WORKTREE
  -> IMPLEMENTING
  -> VALIDATING
  -> REVIEWING
  -> [REMEDIATING -> VALIDATING -> REVIEWING]*
  -> AWAITING_PR_APPROVAL
  -> PUBLISHING_PR
  -> MONITORING_PR
  -> [REMEDIATING -> VALIDATING -> REVIEWING -> MONITORING_PR]*
  -> AWAITING_MERGE_APPROVAL
  -> MERGING
  -> COMPLETED
~~~

The local loop ends when checks pass and no blocking or major review findings
remain. The remote loop defaults to three remediation cycles. The project
policy may lower or raise this limit, but the value is part of PR approval.

Minor findings and suggestions are displayed and persisted. Project policy
determines whether they block publication or merge.

### 9.4 Pause, cancel, rejection, and intervention

- Pause stops new dispatch and lets an in-flight atomic tool operation finish or
  time out. Resume returns to the recorded state after reconciliation.
- Cancel prevents further agent and remote actions. It preserves the worktree,
  database, artifacts, and audit history.
- Plan revision returns to PLANNING with operator feedback.
- A rejected PR candidate returns to REMEDIATING or CANCELLED according to the
  operator command.
- A rejected merge remains in AWAITING_HUMAN_INTERVENTION or is cancelled.
- Safety, permission, scope, cost, time, and retry violations never consume
  implicit authority; they enter AWAITING_HUMAN_INTERVENTION.

## 10. Commands, concurrency, and recovery

The dashboard submits typed commands such as:

- approve_plan and request_plan_revision;
- pause_run, resume_run, and cancel_run;
- approve_pr and request_candidate_changes;
- approve_merge and reject_merge;
- teardown_run_resources.

Every command contains:

- command ID and idempotency key;
- actor identity;
- run ID and expected run version;
- expected artifact or approval digest where relevant;
- structured payload;
- submission timestamp.

The API validates syntax and authorization, then stores the command in
PostgreSQL. The worker claims commands through a lease. A run has at most one
active workflow lease, preventing two workers from advancing it concurrently.

Each successful state change, approval, tool call, and material observation is
committed with an append-only run event in the same transaction as the current
state update where possible. This is an auditable current-state model, not full
event sourcing.

On restart, the worker:

1. finds expired leases and incomplete operations;
2. queries actual local or GitHub state;
3. reconciles idempotent operation records;
4. either resumes safely or requests human intervention.

Remote operations record provider request IDs, repository IDs, branch names,
PR numbers, and observed head commits. Repeated delivery reconciles the intended
resource rather than creating a duplicate.

Transient provider, network, or database errors use bounded exponential retry.
Malformed model output may be repaired once through schema feedback, then
becomes an intervention if still invalid.

## 11. Persistence

PostgreSQL is the source of truth from the first release.

| Table | Purpose |
| --- | --- |
| projects | Repository identity, default branch, instructions, checks, and policy |
| tasks | Normalized task text and optional external issue identity |
| runs | Current workflow state, version, phase, budgets, and resource identity |
| run_commands | Durable operator/system commands, idempotency, status, and lease |
| run_events | Append-only audit timeline |
| steps | Attempts, transitions, timings, and outcomes |
| approvals | Actor, gate, evidence digest, policy version, and invalidation |
| agent_executions | Role, prompt version, provider/model, status, and timing |
| tool_calls | Tool, normalized arguments, authorization, result metadata, and timing |
| model_usage | Input/output tokens, estimated cost, latency, and provider |
| artifacts | Content digest, media type, storage pointer, producer, and lineage |
| validation_results | Named check, command version, exit state, and output artifact |
| reviews | Structured findings, severity, location, status, and decision |
| pull_requests | Repository, branch, PR number, head, checks, reviews, and merge state |

Large plans, diffs, logs, and model outputs use a local content-addressed store.
PostgreSQL holds their hashes, metadata, and lineage. The store uses atomic
writes and hash verification.

Forge does not persist:

- GitHub or model-provider secrets in run records;
- hidden model reasoning;
- unrestricted environment dumps;
- raw secret-bearing command arguments;
- full file contents unless required as a versioned artifact.

Sensitive values are referenced by server-side secret identifiers and redacted
before persistence or display.

## 12. Agent contracts

### 12.1 Common contract

Each agent execution receives:

- an immutable run/task identity;
- a role-specific system instruction version;
- explicitly selected context artifacts;
- a structured input schema;
- role-specific typed tools;
- token, cost, duration, and tool-call budgets.

Each execution returns a validated structured result. The orchestrator, not the
agent, decides the next workflow transition.

Google ADK may manage model invocation and role workflows, but Forge owns
schemas, state, permissions, persistence, approvals, retries, and audit.
Provider-specific code remains behind the AgentGateway port so alternative
providers can be added without changing the domain.

### 12.2 Planner

Reads the task, repository structure, relevant files, and engineering
instructions. It returns:

- summary and assumptions;
- affected components and proposed changes;
- ordered implementation steps;
- required checks and tests;
- risks, security considerations, and dependency changes;
- a machine-readable plan digest input.

It has no write, build, commit, remote GitHub, or secret access.

### 12.3 Developer

Receives only an approved plan and a managed worktree. It may:

- read and edit files inside the canonical worktree root;
- search the repository;
- run approved named checks;
- inspect status and diff;
- create local commits through a controlled Git tool.

It cannot access GitHub write credentials, merge, push, alter Forge policy,
change the approved target, inspect Forge's database, or read unapproved secret
files.

### 12.4 Reviewer

Receives the original task, approved plan, diff, check results, and relevant
repository instructions. It is a fresh execution independent of the Developer.
It cannot modify source or perform remote writes.

Findings are structured as blocker, major, minor, or suggestion and include a
location, explanation, evidence, and proposed resolution. The Reviewer returns
a decision separately from its findings.

## 13. Controlled tools

Initial internal operations include:

~~~text
repository.list_files
repository.read_file
repository.search
repository.read_instructions
repository.write_file

git.create_worktree
git.status
git.diff
git.commit

build.run_named_check

github.get_issue
github.get_pull_request
github.get_checks
github.get_reviews

release.push_managed_branch
release.create_or_update_pull_request
release.merge_pull_request
~~~

Repository writes are available only to the Developer and only within the
managed worktree. Release operations are not agent tools; only the Release
Controller can call them.

Tool authorization is checked in deterministic code using role, run state,
project policy, canonical resource identity, and approved evidence. Arguments
and result metadata are recorded with secret redaction.

MCP is not introduced merely to wrap in-process calls. A tool family may move
behind MCP later when it creates a meaningful process, permission, deployment,
or interoperability boundary.

## 14. Project policy and repository configuration

Each project has an operator-approved, versioned policy stored in PostgreSQL.
It defines:

- canonical repository path and allowed GitHub repository;
- default and permitted base branches;
- engineering instruction discovery rules;
- named check commands, timeouts, and required status;
- build runner and network policy;
- allowed environment files and keys;
- worktree/database provisioning settings;
- allowed merge methods;
- finding severity rules;
- agent provider/model choices and budgets;
- local and remote remediation limits.

A repository may contain suggested Forge configuration, but it is untrusted
until imported and approved by the operator. Candidate changes cannot mutate
the active project policy for their own run.

Named checks map stable names such as test, lint, typecheck, and build to exact
operator-approved command vectors. Agents select names, not arbitrary command
strings.

Dependency installation is permitted only when the approved plan and project
policy allow it. An unexpected new dependency or install command requires human
intervention.

## 15. Worktree and database lifecycle

Forge development itself ships matching setup and teardown scripts for
PowerShell and Bash, modelled on the proven Parallel workflow.

Runtime provisioning and the scripts share these invariants:

- deterministic, collision-resistant resource names derived from project and
  run IDs;
- a canonical managed-worktree root;
- path containment checks before create, move, or removal;
- rejection of symlink, junction, and traversal escapes;
- a branch uniquely owned by the run;
- explicit allowlists for copied environment files and rewritten keys;
- secret values never printed in logs;
- a dedicated PostgreSQL database per worktree when the project requests one;
- recorded resource identity before implementation starts;
- cleanup verification and idempotent teardown.

The setup sequence is:

1. validate repository, branch, target path, and collision-free resource name;
2. create the managed branch and worktree;
3. copy only configured environment files without logging contents;
4. rewrite configured database identifiers and other worktree-local values;
5. create the isolated database;
6. run approved bootstrap, migration, and seed steps;
7. persist verified resource metadata.

If setup fails, Forge records the partial resource state and performs only
validated, bounded rollback. Otherwise it requests intervention.

Teardown is always explicit. It removes the worktree before dropping the
database, verifies exact recorded paths and identifiers, and keeps the branch
by default. Branch deletion is a separate confirmed option.

## 16. Validation, review, and remediation

Validation runs the project policy's required named checks in a deterministic
order. Every attempt records the exact policy version, command identity,
duration, exit status, and bounded output artifact.

By default, check execution occurs in a Docker container that:

- runs as a non-root user;
- mounts only the managed worktree;
- receives an allowlisted environment;
- has no Docker socket;
- has network access disabled unless project policy explicitly enables it;
- has CPU, memory, output, and time limits.

An operator may configure a trusted host runner for repositories that cannot
run in Docker. It is disabled by default and displayed as unsandboxed in every
approval and run view. Selecting it acknowledges that generated code can act
with the Forge host account's permissions.

The local remediation loop requires revalidation and a fresh independent
review after every Developer change. The remote loop follows the same sequence
before the Release Controller pushes a remediation commit.

The default remote limit is three cycles. A cycle is counted when a remediation
attempt begins, regardless of whether it produces a commit. After the limit,
Forge preserves all evidence and enters AWAITING_HUMAN_INTERVENTION.

## 17. GitHub and pull-request flow

1. The Developer produces a local commit in the managed worktree.
2. Forge freezes the candidate diff and evidence in AWAITING_PR_APPROVAL.
3. The operator approves publication for that exact candidate and bounded
   remediation policy.
4. The Release Controller verifies the approval, pushes the managed branch,
   and creates or reconciles one PR.
5. Forge polls GitHub for the PR head, required checks, review state, and
   blocking feedback.
6. Failures within policy dispatch a local Developer remediation, then named
   checks and an independent review.
7. The Release Controller pushes the resulting commit and returns to
   monitoring.
8. When all required checks are green and blocking findings are resolved,
   Forge freezes the remote evidence in AWAITING_MERGE_APPROVAL.
9. The operator gives immediate merge approval for the exact remote head and
   merge method.
10. The Release Controller refetches the PR and merges only if every bound
    condition still matches.

External commits to the managed branch invalidate candidate or merge evidence.
Forge never force-pushes in v0.1. Base-branch drift is reported; policy may
require remediation or a new approval, but Forge does not silently rebase after
merge approval.

## 18. Dashboard

The dashboard is the complete operator control plane, not a decorative status
page.

### 18.1 Primary navigation

- Runs
- Approvals
- Projects
- Policies
- Agents and models
- Tool permissions
- Evaluations
- Audit log
- Usage

### 18.2 Run cockpit

The run page displays:

- task, project, branch, worktree, database, PR, base, and exact head;
- current state, phase timeline, active autonomy, and next required gate;
- Pause and Cancel controls;
- Planner, Developer, Reviewer, and Release Controller status;
- required checks and attempt history;
- remediation count and remaining budget;
- cost, tokens, duration, and tool-call counts;
- live activity and persisted audit events.

Tabs are Overview, Plan, Changes, Checks, Review, Activity, Usage, and Security.

Controls are state-aware. For example, Approve merge appears only when the run
is awaiting merge approval and shows the exact commit and checks being
approved. The dashboard has no generic terminal or arbitrary shell form.

REST handles commands and historical queries. Server-Sent Events stream
ordered run events with resumable event IDs. Reconnecting clients query the
current state and continue after their last received event.

v0.1 is a single-operator local application. The API binds to loopback by
default and enforces origin and CSRF protections for state-changing requests.
Remote or multi-user exposure requires a later authentication design.

## 19. Observability and audit

Every run, step, agent execution, tool call, validation, approval, and remote
operation receives correlated identifiers. Forge emits OpenTelemetry-compatible
traces and structured logs while PostgreSQL retains the durable audit record.

Persisted usage includes:

- provider and model;
- prompt/instruction version;
- input and output tokens;
- estimated cost and estimation source;
- duration and retries;
- tool-call count;
- validation and review outcomes;
- remediation cycles;
- human decisions.

Logs and traces use the same redaction policy as persisted records. Trace export
is optional in v0.1; instrumentation is present without requiring an external
observability service.

## 20. Security design

The detailed threat model is in docs/threat-model.md. Core controls are:

- repository and issue text are untrusted data, never authority;
- role permissions are enforced outside model prompts;
- filesystem operations resolve canonical paths and enforce containment;
- remote credentials exist only inside server-side Release Controller adapters;
- agents cannot push, create PRs, merge, change policy, or approve themselves;
- approvals are content-addressed and invalidated on evidence changes;
- build execution is sandboxed by default and explicitly labelled otherwise;
- secrets are allowlisted, scoped, redacted, and never included in agent context
  by default;
- named commands replace arbitrary shell strings;
- dependency changes require approved plan scope;
- all provider use is recorded, and project policy controls which providers may
  receive repository data.

These controls reduce risk but do not make hostile generated code safe on an
unsandboxed host, prevent all model-provider retention, or prove that a review
found every defect. Those residual risks remain visible to the operator.

## 21. Error handling

| Failure class | Behavior |
| --- | --- |
| Transient network/provider failure | Bounded retry, then intervention |
| Invalid structured agent output | One schema-repair attempt, then intervention |
| Validation or blocking review failure | Remediation while budget remains |
| Scope, dependency, security, permission, or budget violation | Immediate intervention |
| Worker/API restart | Lease expiry, state reconciliation, safe resume |
| Duplicate command or remote request | Idempotent reconciliation |
| Artifact hash/storage corruption | FAILED with preserved metadata |
| Cancellation | Stop dispatch; preserve resources and evidence |
| Teardown failure | Preserve exact remaining resource state; do not guess |

FAILED is reserved for unrecoverable integrity or internal errors. A task that
needs a human decision is not incorrectly labelled failed.

## 22. Testing and evaluations

### 22.1 Unit tests

- legal and illegal state transitions;
- approval digest creation and invalidation;
- optimistic run-version checks;
- policy and role authorization;
- remediation and usage budgets;
- canonical path and resource-name safety;
- structured agent-output validation;
- Release Controller preconditions.

### 22.2 Integration tests

- PostgreSQL repositories, migrations, commands, leases, and restart recovery;
- local content-addressed artifact storage;
- worktree creation, collision handling, and teardown;
- isolated database creation and cleanup;
- agent and tool adapter contracts;
- idempotent GitHub create/update/merge behavior using a fake service.

PowerShell and Bash worktree scripts have contract tests and smoke coverage on
Windows and Linux respectively.

### 22.3 End-to-end tests

Deterministic fake agents and a fake GitHub service exercise:

- planning through plan approval;
- implementation, failed validation, remediation, and review;
- PR publication approval and idempotent creation;
- failed CI, bounded remote remediation, and green recovery;
- stale merge approval after head change;
- exact-head successful merge;
- pause, cancellation, restart, and explicit teardown.

### 22.4 Security tests

- prompt-injected repository and task content cannot change permissions;
- traversal, symlink, junction, and alternate-path escapes are rejected;
- secret-bearing values are redacted from contexts, events, logs, and artifacts;
- agents cannot invoke Release Controller operations;
- stale or forged approvals are rejected;
- unexpected dependencies and unapproved network access escalate.

### 22.5 Evaluations

Versioned fixture repositories and task sets measure:

- plan completeness and relevance;
- implementation correctness;
- required-test selection;
- Reviewer defect detection and false positives;
- remediation success;
- tool-policy compliance;
- token, cost, and duration trends.

The default test suite uses deterministic fakes and needs no paid model or live
GitHub access. Real-provider and live-GitHub smoke evaluations are explicit,
credential-gated, and non-blocking for ordinary local development.

## 23. v0.1 acceptance criteria

Forge v0.1 is accepted when all of the following work locally through the
dashboard:

1. Register a local Git repository and versioned project policy.
2. Create a task from plain text; GitHub issue import may be used when
   configured.
3. Generate a structured Planner plan and wait for human approval.
4. Create an isolated worktree and, when configured, a per-worktree database.
5. Run a Developer against only that managed worktree.
6. Produce a local commit and inspectable diff.
7. Execute configured lint, test, typecheck, and other required named checks.
8. Run a separate Reviewer and display structured findings.
9. Perform bounded local remediation, validation, and re-review.
10. Display live state, artifacts, activity, checks, findings, and usage.
11. Require PR publication approval before any remote write.
12. Have the Release Controller push and create or reconcile the PR.
13. Monitor CI/reviews and perform up to three approved remediation cycles by
    default.
14. Request merge approval only for an exact green remote head.
15. Reject stale approval and merge only after immediate human authorization.
16. Recover safely after restart and support pause and cancellation.
17. Preserve resources on completion/cancellation and tear them down only on an
    explicit command.
18. Pass the unit, integration, end-to-end, and security test suites described
    above.

## 24. Staged implementation boundary

The implementation plan will deliver one vertical slice in dependency order:

1. domain model, PostgreSQL persistence, commands, and state engine;
2. safe local project/worktree/tool foundation;
3. agent contracts and local plan/implement/validate/review loop;
4. dashboard and live event stream over that real workflow;
5. Release Controller and GitHub PR/CI/merge lifecycle;
6. recovery, security hardening, evaluations, and acceptance verification.

Each stage must end in an executable, tested path. Later stages extend the same
domain model rather than creating parallel prototypes.

## 25. Design records

- docs/architecture.md describes module and runtime boundaries.
- docs/threat-model.md records threats, mitigations, and residual risks.
- docs/adr/ADR-001-human-approval-gates.md records the three approval gates.
- docs/adr/ADR-002-explicit-orchestration-state-machine.md records explicit
  persisted orchestration.
- docs/adr/ADR-003-controlled-tool-interfaces.md records least-privilege tools
  and the deterministic Release Controller.

This specification is the source for the staged implementation plan. If a
later implementation choice changes an approved boundary, the specification or
an ADR must be updated and reviewed before that boundary is changed.
