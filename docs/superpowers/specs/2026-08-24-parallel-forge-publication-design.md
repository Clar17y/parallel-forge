# Parallel Forge public repository publication design

## Purpose

Publish the current Parallel Forge milestone as a transparent public development
repository without presenting the unfinished v0.1 workflow as a completed
release. Add a concise project entry to the `Clar17y/Clar17y` profile README so
the repository is discoverable from the maintainer profile.

## Public identity

- GitHub repository: `Clar17y/parallel-forge`
- Display name: **Parallel Forge**
- Visibility: public
- License: Apache License 2.0
- Description: "Local-first control plane for agent-assisted software delivery
  with durable workflow state, typed tools, human approval gates, and controlled
  GitHub releases."
- Suggested topics: `ai-agents`, `agentic-workflows`, `developer-tools`,
  `human-in-the-loop`, `python`, and `postgresql`

Parallel Forge began as the engineering control plane intended for building
Parallel, but its repository and documentation describe it as an independent
tool that can manage other software projects.

## Publication and branch model

Preserve the complete reachable development history. Do not squash, rewrite, or
remove the security-review and implementation records already committed to the
branch.

Push both existing branches:

- `main` remains at the original shared base commit. It is retained as the clean
  future pull-request target.
- `forge/v0-1` contains the current implementation and new public metadata. It
  becomes the temporary default branch so visitors land on the current README.

No pull request, merge, release, or tag is created as part of publication.
Moving the default branch to `main` and opening a pull request remain separate,
explicitly authorized operations.

## Repository README

Add a root `README.md` to `forge/v0-1` with:

1. the Parallel Forge name and a short local-first, human-governed agentic
   engineering description;
2. a prominent active-v0.1 status warning;
3. the relationship to Parallel and the standalone use case;
4. only capabilities that are currently implemented, including durable
   PostgreSQL workflow state, causal events, content-addressed artifacts,
   protected local secrets and environment staging, controlled Git operations,
   isolated worktree/database provisioning, and Docker-first execution;
5. the safety model: model-driven agents do not receive push, PR-write, merge,
   approval, credential, or Forge-database authority;
6. a high-level architecture and links to the architecture, threat model,
   design, and implementation roadmap;
7. exact prerequisites already established by the repository: Python 3.14,
   Node.js 24, PostgreSQL 17, and Docker;
8. verified development and test commands only, without claiming that the final
   one-command development environment or dashboard already exists;
9. an explicit roadmap summary for structured agents, dashboard controls,
   bounded CI remediation, and separately approved PR and merge actions; and
10. an Apache-2.0 license reference.

Add the canonical, unmodified Apache License 2.0 text as root `LICENSE` so
GitHub can detect it reliably. Put the 2026 Clar17y copyright statement in the
README license section rather than modifying the canonical license text. Do not
invent a company or legal entity, and do not add a `NOTICE` file unless the
repository later acquires notices that Apache-2.0 requires it to preserve.

## GitHub profile README

Preserve the current `Clar17y/Clar17y` README and insert this section immediately
after the existing Parallel section:

> # 🔨 Parallel Forge
>
> **A local-first control plane for durable, reviewable agent-assisted software
> delivery.**
>
> Parallel Forge began as the engineering system I wanted for building Parallel,
> but is designed to work independently with other repositories. It combines
> autonomous agent workflows with explicit human approval gates, isolated
> worktrees and databases, constrained command execution, durable state, audit
> evidence and recovery after interruption.
>
> The current v0.1 foundation includes PostgreSQL-backed workflow state, causal
> events, content-addressed artifacts, protected secrets and environment
> staging, controlled Git operations, isolated worktree/database provisioning,
> and Docker-first execution.
>
> The roadmap adds structured planning, implementation and independent review
> agents; a live control dashboard; bounded CI remediation; and human-approved
> PR publication and merging.
>
> **Status:** Active v0.1 development — [view the public
> repository](https://github.com/Clar17y/parallel-forge).

The profile update is committed directly to the profile repository because it
is a standalone metadata change explicitly requested by the owner. It does not
modify any other profile content.

## Safety and publication checks

Before creating or pushing the public repository:

1. verify the local worktree and index are clean before public-metadata edits;
2. run a dedicated secret scanner across the complete reachable Git history;
3. inspect tracked environment, credential-shaped, private-key-shaped, and local
   path fixtures, distinguishing synthetic test data from live credentials;
4. stop before publication if any possible live secret is found;
5. verify no `.env`, private key, local database, virtual environment, or
   generated artifact is tracked;
6. review the exact outgoing branch refs and commit range; and
7. authenticate GitHub through the interactive GitHub CLI flow without exposing
   a token in argv, files, logs, or chat.

The current history intentionally retains internal design/review records and
synthetic local path or repository fixtures. Their presence is disclosed and
accepted; unreachable local commits are not pushed.

## External operation order

1. Add and verify `README.md`, `LICENSE`, and this approved design on
   `forge/v0-1`.
2. Run candidate-scoped documentation checks and the complete history safety
   scan.
3. Commit the public metadata locally.
4. Re-authenticate the configured `Clar17y` GitHub CLI account interactively.
5. Create the empty public `Clar17y/parallel-forge` repository.
6. Add its exact GitHub URL as `origin`.
7. Push the existing `main` ref and then `forge/v0-1`, without force.
8. Set `forge/v0-1` as the temporary default branch and configure the approved
   description and topics.
9. Fetch the `Clar17y/Clar17y` repository, make only the approved README change,
   verify its diff, commit, and push it without force.
10. Read back both public repositories and verify links, branches, README
    rendering, and license detection.

## Failure handling

- Repository-name collision, wrong owner, unexpected existing remote, non-fast-
  forward push, or changed profile README stops the operation for review.
- Authentication failure triggers a fresh interactive login; credentials are
  never requested in chat.
- A failed profile update does not roll back or delete the newly published Forge
  repository. The partial result is reported truthfully and can be retried.
- No force push, branch deletion, history rewrite, pull request, or merge is an
  allowed recovery action.

## Acceptance criteria

- `Clar17y/parallel-forge` is public and has the approved description/topics.
- Its reachable history is preserved and contains no detected live secret.
- `main` and `forge/v0-1` exist remotely at the expected commits.
- `forge/v0-1` is the temporary default branch.
- The root README renders and clearly states the active-v0.1 status.
- GitHub recognizes the repository as Apache-2.0 licensed.
- The Clar17y profile displays the approved Parallel Forge section and working
  repository link.
- No pull request, merge, release, tag, force push, or unrelated profile change
  occurs.
