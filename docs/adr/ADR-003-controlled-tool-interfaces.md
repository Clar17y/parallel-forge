# ADR-003: Controlled tool interfaces and Release Controller

Status: Accepted  
Date: 2026-08-21

## Context

Agents need to inspect repositories, edit isolated code, run validation, and
participate in a pull-request workflow. Giving every model unrestricted shell,
filesystem, Git, and GitHub access would let prompt injection or model error
cross role, repository, secret, and approval boundaries.

At the same time, Forge must be autonomous enough to publish approved work,
remediate CI failures, and merge after human authorization.

## Decision

Forge exposes narrow typed tool operations and evaluates authorization in
deterministic code using role, run state, project policy, canonical resource
identity, and approved evidence.

Role capabilities are:

| Role | Capabilities |
| --- | --- |
| Planner | Repository reads and searches only |
| Developer | Managed-worktree reads/writes, named checks, local status/diff/commit |
| Reviewer | Repository reads, diff, and validation evidence only |
| Release Controller | Constrained managed-branch push, PR create/update, exact-head merge |
| Human operator | Policy and approval commands through the dashboard |

The Release Controller is trusted application code, not an LLM agent. GitHub
write credentials exist only in its adapter. Agents cannot call release
operations or receive those credentials.

Checks are selected by stable names mapped to operator-approved command vectors.
Filesystem operations canonicalize paths and enforce containment. Build code
runs in Docker by default; any host runner is explicit and labelled
unsandboxed.

Internal ports and adapters implement the v0.1 boundary. MCP is introduced only
when a later process, permission, deployment, or interoperability boundary
benefits from it.

## Consequences

Positive:

- prompt injection cannot directly turn an agent request into push or merge;
- least privilege is enforceable and testable outside prompts;
- tool calls have consistent validation, redaction, audit, and idempotency;
- repository-specific commands remain configurable without generic agent shell;
- Forge retains autonomous PR remediation through trusted code.

Negative:

- each needed operation requires a deliberate typed interface;
- some repositories may need new adapters or named checks;
- Docker-based validation adds local setup and performance cost;
- the trusted tool and Release Controller code require especially strong tests
  and review.

## Alternatives considered

### Unrestricted shell for all agents

Rejected because prompts are not a security boundary and arbitrary commands
would defeat role separation, path containment, secret minimization, and audit.

### Give the Developer GitHub write access

Rejected because repository or issue injection could then cause remote writes
outside the approved release state.

### Require a human to execute every GitHub command manually

Rejected because it would prevent Forge from providing the approved autonomous
PR creation, monitoring, remediation, and merge workflow.

### Build separate MCP servers immediately

Rejected as premature. In-process typed ports establish the contract now and
can be extracted later without adding empty services to the first slice.
