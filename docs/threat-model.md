# Forge v0.1 Threat Model

Status: Approved initial threat model  
Date: 2026-08-21  
Review trigger: Any new remote-write capability, runner mode, credential class,
provider, tenancy model, or deployment boundary

## 1. Scope

This threat model covers the local-first Forge v0.1 workflow:

- task and optional GitHub issue ingestion;
- repository inspection;
- Planner, Developer, and Reviewer executions;
- managed worktree and per-worktree database lifecycle;
- generated-code validation;
- operator approvals through the dashboard;
- pull-request creation, monitoring, remediation, and approved merge;
- local PostgreSQL and artifact persistence;
- external model-provider and GitHub connections.

It does not claim that a local deployment is inherently safe. Forge processes
untrusted text and executes untrusted generated code against repositories that
may contain valuable source and secrets.

## 2. Security objectives

Forge must:

- prevent agents and untrusted content from granting themselves authority;
- prevent one role or run from accessing another's resources;
- prevent remote writes without the required human approval;
- prevent merge when approved evidence has changed;
- restrict generated-code execution and make unsandboxed execution explicit;
- minimize and redact secrets and proprietary source sent to external systems;
- make consequential actions attributable and reconstructable;
- fail closed when identity, state, path, approval, or policy is ambiguous;
- preserve evidence after cancellation or failure.

## 3. Assets

- source code and repository history;
- GitHub repositories, branches, pull requests, and merge rights;
- GitHub and model-provider credentials;
- environment files and application secrets;
- Forge policy and agent system instructions;
- PostgreSQL workflow and audit data;
- worktree-specific databases;
- plans, diffs, logs, reviews, and artifacts;
- operator identity and approvals;
- model usage and cost budgets;
- host filesystem, network, processes, and Docker daemon.

## 4. Trust zones

### Zone A: Operator browser

Trusted to express human intent, but browser requests still require origin,
CSRF, state-version, and approval-evidence validation.

### Zone B: Forge control plane

FastAPI, Domain/Application code, PostgreSQL, policy enforcement, artifact
metadata, and the Release Controller. This is the trusted computing base.

### Zone C: Agent/provider boundary

Model responses are untrusted. Provider systems are external data processors.
Only selected, policy-approved context may cross this boundary.

### Zone D: Target repository and generated-code runner

Repository files, issue text, dependency scripts, generated code, test code,
compiler plugins, and build output are untrusted.

### Zone E: GitHub

External authoritative state for remote branches, PRs, checks, reviews, and
merges. Remote state can change independently of Forge.

### Zone F: Local host and Docker

The host is trusted operational infrastructure. Docker is a containment layer,
not a perfect security boundary. The Docker daemon and host socket are
high-value assets and are never mounted into build sandboxes.

## 5. Assumptions

- v0.1 has one local operator and binds its API to loopback.
- The host account and Forge installation have not already been compromised.
- PostgreSQL and the artifact directory are accessible only to the Forge host
  account and configured local services.
- GitHub branch protections and required checks remain a valuable independent
  defense.
- Model providers may retain or process submitted data according to their own
  terms; Forge cannot erase that residual risk.
- Operators understand that selecting the trusted host runner allows generated
  code to act with the Forge process account's permissions.

## 6. Threats and controls

### T01: Prompt injection through repository content

Threat:

Repository instructions, source comments, test output, or generated artifacts
tell an agent to ignore its role, reveal secrets, widen scope, or invoke a
release action.

Controls:

- treat repository content as quoted, untrusted task data;
- keep system instructions and permission policy outside repository-controlled
  context;
- enforce role and tool authorization in deterministic code;
- never expose Release Controller operations as agent tools;
- validate agent output schemas and all tool arguments;
- show scope-changing proposals to the operator.

Residual risk:

An injection may still influence plan or code quality within allowed
permissions. Independent review, validation, and human evidence inspection
reduce but do not eliminate this risk.

### T02: Malicious issue or task description

Threat:

An issue requests credential theft, destructive changes, hidden scope, or
remote action while presenting itself as legitimate work.

Controls:

- store original and normalized task text;
- Planner explicitly reports scope, risks, dependency changes, and tests;
- plan approval is required before any write;
- task text cannot mutate project policy or system instructions;
- scope and security deviations enter human intervention.

Residual risk:

The operator may approve a deceptive task. Forge makes evidence visible but
does not replace human judgment.

### T03: Arbitrary command execution

Threat:

An agent constructs a shell command, build script, or argument that escapes the
intended operation.

Controls:

- expose named checks rather than agent-supplied command strings;
- store approved commands as argument vectors in versioned project policy;
- avoid shell interpolation in tool adapters;
- run checks in a constrained Docker sandbox by default;
- enforce time, CPU, memory, output, environment, mount, and network limits;
- do not mount the Docker socket;
- label and separately configure the unsandboxed host runner.

Residual risk:

Approved repository commands and dependency scripts are code execution. Docker
reduces impact but is not a formally verified sandbox. The host runner provides
substantially weaker isolation.

### T04: Secret exposure

Threat:

Agents, build output, artifacts, logs, traces, or UI responses reveal GitHub,
database, cloud, model-provider, or application secrets.

Controls:

- agents receive no environment by default;
- project policy allowlists environment files and keys per operation;
- GitHub write credentials exist only in the Release Controller adapter;
- redact known sensitive values before persistence, tracing, and display;
- never persist unrestricted environment dumps;
- never log copied environment-file contents;
- mount or inject credentials only for the narrow operation requiring them.

Residual risk:

Unknown secrets embedded in repository files may be read or sent to a provider.
Future secret scanning can reduce this risk but cannot guarantee detection.

### T05: Unsafe dependency installation

Threat:

A proposed or transitive dependency runs hostile install hooks, compromises the
runner, leaks data, or expands supply-chain risk.

Controls:

- dependency changes must be declared in the approved plan;
- project policy controls install/bootstrap commands and network access;
- unexpected manifests, lockfile changes, or install operations escalate;
- installation runs inside the build sandbox by default;
- diff and independent review include dependency changes.

Residual risk:

An approved dependency or existing lockfile may still be compromised.

### T06: Destructive Git operation

Threat:

An agent or faulty adapter force-pushes, deletes a valuable branch, writes to
the wrong repository, or removes the wrong worktree.

Controls:

- agents have no push or merge capability;
- the Release Controller targets only the recorded repository and managed
  branch;
- v0.1 never force-pushes;
- canonical worktree paths and resource identities are persisted and rechecked;
- teardown is explicit and keeps branches by default;
- branch deletion is separately confirmed;
- destructive operations fail closed on ambiguity.

Residual risk:

A bug in the trusted Release Controller or Git adapter can still perform an
incorrect authorized operation. Integration tests and GitHub branch protection
provide independent defense.

### T07: Privilege escalation between roles

Threat:

Planner writes files, Reviewer alters the candidate, Developer performs remote
writes, or an agent invokes another role's tools.

Controls:

- issue a fresh role-specific execution and capability set;
- authorize every tool call using role, state, run, policy, and resource;
- Reviewer is a separate execution with read-only inputs;
- Release Controller functions are not present in agent tool registries;
- record authorization decisions and denied attempts.

Residual risk:

Misconfigured role policy in trusted code could widen permissions. Permission
matrix unit tests are mandatory.

### T08: Untrusted generated code escapes isolation

Threat:

Generated source or tests exploit a runtime, compiler, package manager,
container, kernel, or host runner.

Controls:

- Docker sandbox is the default;
- run non-root with only the worktree mounted;
- disable network by default;
- do not mount Forge data, host secrets, GitHub credentials, or Docker socket;
- apply resource and duration limits;
- treat sandbox output as untrusted and bounded;
- display a prominent warning and approval evidence when using host mode.

Residual risk:

Containers share a kernel and cannot guarantee containment. Host mode is
explicitly unsafe for hostile code.

### T09: Approval-gate bypass or replay

Threat:

An agent, stale browser, duplicated command, or attacker reuses an approval for
different evidence or advances state without approval.

Controls:

- agents cannot create approval records;
- approval commands require actor, expected run version, idempotency key, and
  exact evidence digest;
- state transitions enforce required gates;
- plan, PR, and merge approvals bind different evidence sets;
- material change invalidates approval;
- duplicate commands reconcile by ID and intended effect;
- merge state is re-read from GitHub immediately before action.

Residual risk:

The local single-operator boundary does not protect against a compromised host
or browser session.

### T10: Remote branch or PR changes after approval

Threat:

A new commit, changed base, changed check set, unresolved review, or altered
merge method makes a prior approval unsafe.

Controls:

- merge approval binds repository, PR, exact head, base, required successful
  checks, review resolution, merge method, and policy version;
- Release Controller retrieves and compares authoritative GitHub state just
  before merge;
- any mismatch invalidates approval and returns to monitoring/intervention;
- GitHub branch protection remains enabled.

Residual risk:

A race inside GitHub's own merge semantics is governed by GitHub protections
and API preconditions. Forge records the observed and resulting merge commits.

### T11: Data leakage to model providers

Threat:

Private source, secrets, customer data, or unnecessary repository content is
sent to an external model provider.

Controls:

- project policy allowlists providers and models;
- select only task-relevant files and bounded excerpts;
- exclude configured sensitive paths and secret files;
- record provider, model, prompt version, and artifact lineage;
- make provider choice visible to the operator;
- support a provider adapter so local or differently governed models can be
  added later.

Residual risk:

Provider handling and retention remain governed externally. Forge cannot
guarantee deletion after transmission.

### T12: Cross-run worktree, database, or artifact access

Threat:

One run reads, overwrites, or deletes another run's filesystem, database, or
artifacts through collision, traversal, symlink, or identifier confusion.

Controls:

- derive collision-resistant names from project and run IDs;
- resolve and verify canonical paths beneath an exact managed root;
- reject traversal, symlink, junction, and reparse-point escapes;
- persist resource identity before use;
- scope database credentials and names per worktree where practical;
- verify exact recorded resources during teardown;
- test name collisions and alternate path forms on Windows and Linux.

Residual risk:

All local resources still share the host account and PostgreSQL server in v0.1.

### T13: Audit tampering or incomplete evidence

Threat:

An action cannot be reconstructed, logs omit a denied operation, or mutable
records hide what was approved.

Controls:

- append run events alongside current-state changes;
- content-address artifacts and verify hashes;
- record approval evidence and invalidation;
- correlate agents, tools, checks, remote requests, and usage;
- bound and mark truncated output rather than silently dropping it;
- preserve resources and evidence on cancellation.

Residual risk:

The local operator or compromised database administrator can alter storage.
Tamper-evident external audit export is future work.

### T14: Cost or resource exhaustion

Threat:

Agents loop, retry indefinitely, create excessive artifacts, consume provider
budget, or hold worker leases forever.

Controls:

- per-run token, cost, duration, tool-call, and remediation budgets;
- bounded retries and lease expiry;
- explicit count semantics for remediation cycles;
- output and artifact size limits;
- immediate intervention when a budget boundary is reached;
- usage displayed live and persisted.

Residual risk:

Provider pricing and token reporting may lag or differ from estimates.

### T15: Local dashboard request forgery

Threat:

A malicious page causes the operator's browser to issue an approval or other
state-changing request to the loopback API.

Controls:

- bind to loopback by default;
- validate Origin and Host;
- use CSRF tokens and same-site cookies where cookies are used;
- require typed POST commands with expected run versions and evidence digests;
- avoid state-changing GET endpoints;
- display exact approval evidence before submission.

Residual risk:

A compromised browser extension or host session remains inside the local trust
boundary.

## 7. Security verification

Required automated coverage includes:

- role-to-tool permission matrix tests;
- illegal state-transition and approval-replay tests;
- stale remote-head merge tests;
- canonical path, traversal, symlink, junction, and collision tests;
- redaction tests across events, logs, traces, artifacts, and API responses;
- sandbox configuration assertions;
- denied dependency/network escalation tests;
- idempotent and wrong-repository Release Controller tests;
- restart and expired-lease reconciliation tests.

Before a live GitHub merge path is enabled, a fresh independent review must
check the Release Controller, approval binding, credential scope, and remote
state reconciliation.

## 8. Incident behavior

On a suspected security violation, Forge:

1. stops new dispatch for the affected run;
2. avoids further remote writes;
3. enters AWAITING_HUMAN_INTERVENTION or FAILED according to integrity;
4. preserves worktree, database, artifacts, events, and remote identifiers;
5. records the triggering observation without copying secrets;
6. requires an explicit operator decision before resuming or teardown.

Forge does not silently clean up evidence after a security event.
