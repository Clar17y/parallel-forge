# Forge Architecture

Status: Approved for v0.1  
Date: 2026-08-21

## Purpose

Forge is a local-first control plane for agent-assisted software delivery. It
coordinates model-driven roles, controlled development tools, durable workflow
state, human approval gates, and a constrained GitHub release lifecycle.

The complete product design is recorded in
docs/superpowers/specs/2026-08-21-forge-v0-1-design.md. This document is the
short architectural map for contributors.

## System context

The human operator uses a Next.js dashboard. The dashboard submits typed
commands and reads state through a FastAPI service. PostgreSQL stores all
workflow state and a durable command queue. An orchestrator worker dispatches
specialist agents and controlled tools. A deterministic Release Controller is
the only component allowed to perform remote GitHub writes.

~~~text
Operator
   |
   v
Next.js web
   |
   | REST + SSE
   v
FastAPI control API
   |
   v
PostgreSQL <----> local content-addressed artifact store
   |
   v
Orchestrator worker
   |
   +---- AgentGateway ---- Planner / Developer / Reviewer
   |
   +---- Controlled tools ---- Repository / Git / Build sandbox
   |
   +---- Release Controller ---- GitHub and managed branch writes
~~~

## Runtime units

### Web control plane

Responsibilities:

- present runs, approvals, projects, policies, usage, and audit evidence;
- submit state-aware, typed operator commands;
- display live events through Server-Sent Events;
- make the exact artifacts and commit being approved visible.

It does not contain orchestration logic or direct access to models, GitHub,
Git, the filesystem, or PostgreSQL.

### Control API

Responsibilities:

- authenticate the local operator boundary;
- validate command syntax, expected run version, and request origin;
- write durable commands;
- serve projections and artifacts;
- stream ordered run events.

It does not perform long-running workflow steps in request handlers.

### Orchestrator worker

Responsibilities:

- lease one run at a time;
- validate state transitions and policy;
- dispatch agents and tools;
- store artifacts and evidence;
- enforce retries, budgets, and human gates;
- reconcile incomplete local and remote operations after restart.

### AgentGateway

Responsibilities:

- translate Forge's typed agent request into provider/framework calls;
- use Google ADK where it helps with invocation and agent workflow;
- validate structured responses;
- record model/provider usage and correlation metadata.

Forge's domain never imports ADK types. State, approvals, authorization,
persistence, and retries are Forge-owned.

### Controlled tool layer

Responsibilities:

- expose typed repository reads and worktree-scoped writes;
- create and inspect managed Git worktrees and local commits;
- run only named, operator-approved checks;
- enforce canonical path containment and role permissions;
- capture redacted audit evidence.

Internal typed interfaces are sufficient for v0.1. MCP is reserved for a later
process or interoperability boundary that provides concrete value.

### Release Controller

Responsibilities:

- push only the branch recorded for an approved run;
- create or reconcile one managed pull request;
- observe remote heads, checks, and review state;
- merge only when an exact-head human approval still matches.

It is deterministic application code, not an LLM agent. Agents never receive
GitHub write credentials or its operations as callable tools.

### PostgreSQL

Responsibilities:

- current workflow state and optimistic version;
- durable commands and worker leases;
- approvals and invalidations;
- append-only events and audit metadata;
- agent, tool, validation, review, PR, and usage records;
- artifact metadata and lineage.

The system uses current-state tables plus an append-only audit log. It is not
full event sourcing.

### Artifact store

Large immutable plans, diffs, logs, and outputs are stored locally by content
hash. PostgreSQL stores the digest, media type, producer, lineage, and storage
pointer. Writes are atomic and verified.

## Code boundaries

The Python application follows a ports-and-adapters dependency direction:

~~~text
domain
  ^
  |
application
  ^
  |
adapters: api, persistence, agents, tools, observability, cli
~~~

- Domain defines states, transitions, approvals, policies, and value objects.
- Application defines use cases and ports.
- Adapters integrate FastAPI, PostgreSQL, ADK/providers, Git, Docker, GitHub,
  OpenTelemetry, and the local filesystem.
- Dependencies point inward. Framework types do not enter Domain.

Each module must have one clear responsibility and a typed public interface.
Large modules are split by capability, not by arbitrary technical layers.

## Contracts

### Command contract

Every mutating request carries:

- a unique command ID and idempotency key;
- actor identity;
- run ID and expected run version;
- expected evidence digest for approval commands;
- a typed payload.

### Agent contract

Every agent request carries:

- role and instruction version;
- selected immutable context artifacts;
- allowed typed tools;
- output schema;
- token, cost, duration, and tool budgets.

Agent output is evidence, never a workflow transition by itself.

### Tool contract

Every tool request carries:

- run, step, and actor identity;
- tool operation and structured arguments;
- canonical project/worktree resource identity;
- current authorization context.

Authorization is evaluated in deterministic code before adapter invocation.

### Approval contract

An approval identifies the actor, gate, run version, policy version, and digest
of all evidence being authorized. A material evidence change makes it stale.

## Concurrency and consistency

- A PostgreSQL lease permits at most one active worker for a run.
- Optimistic run versions reject stale dashboard commands.
- State change and audit event are committed atomically where possible.
- Commands and remote operations are idempotent and reconcilable.
- External GitHub state is re-read before a consequential transition.
- Server-Sent Event sequence IDs let dashboards resume without missing events.

## Security boundaries

The principal boundaries are:

1. browser to loopback control API;
2. Forge control plane to model providers;
3. Forge worker to untrusted repository/generated code;
4. Forge Release Controller to GitHub;
5. one agent role to another;
6. one run's worktree/database/artifacts to another run.

See docs/threat-model.md for threats, controls, and residual risks.

## Deployment

v0.1 runs locally with:

- the web application;
- the API/worker;
- PostgreSQL;
- optional Docker build sandboxes.

The API binds to loopback by default. No GCP, Cloud Run, Terraform, Kubernetes,
or distributed queue is included. The API/worker command boundary and
OpenTelemetry instrumentation preserve a clean later deployment seam.
