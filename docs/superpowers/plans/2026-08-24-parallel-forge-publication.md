# Parallel Forge Public Repository Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the current Parallel Forge milestone and complete reachable history as `Clar17y/parallel-forge`, then add the approved project entry to the `Clar17y/Clar17y` profile README.

**Architecture:** Public metadata is prepared and committed locally before any remote mutation. A pinned, canary-checked secret scanner and targeted Git inventory gate publication; only then does the controller authenticate interactively, create the empty public repository, push the existing `main` and `forge/v0-1` refs without force, update the profile repository through its own exact clone, and independently read back all public state.

**Tech Stack:** Git, GitHub CLI, GitHub HTTPS, Markdown, Apache License 2.0, Docker, Gitleaks v8.29.0 pinned by image digest, PowerShell 7.

## Global Constraints

- Public repository identity is exactly `Clar17y/parallel-forge`; visibility is public.
- Display name is **Parallel Forge** and the product is described as designed for Parallel but independently usable.
- Preserve all commits reachable from `main` and `forge/v0-1`; never squash, rewrite, or force push.
- Publish the canonical unmodified Apache License 2.0 text.
- Keep `main` at `80860628b19c0e919dd89c8596d7d5789ad38162` and use `forge/v0-1` as the temporary default branch.
- Do not create a pull request, merge, release, tag, branch deletion, or unrelated profile change.
- Stop before any push on a possible live-secret finding, wrong owner, name collision, unexpected remote, ref drift, or non-fast-forward condition.
- Authenticate only through the interactive GitHub CLI flow; never place credentials in argv, files, logs, or chat.
- README capability claims must distinguish implemented v0.1 foundations from roadmap items.
- The profile README edit is exact and immediately follows the current Parallel section.

---

### Task 1: Prepare public repository metadata

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Reference: `docs/architecture.md`
- Reference: `docs/threat-model.md`
- Reference: `docs/superpowers/specs/2026-08-21-forge-v0-1-design.md`
- Reference: `docs/superpowers/plans/2026-08-21-forge-v0-1.md`
- Reference: `pyproject.toml`
- Reference: `docker-compose.yml`

**Interfaces:**
- Consumes: the implemented capability inventory and runtime versions already committed to `forge/v0-1`.
- Produces: an accurate public landing page and GitHub-detectable Apache-2.0 license for later publication.

- [ ] **Step 1: Verify the exact starting refs and clean worktree**

Run:

```powershell
git status --short --branch
git rev-parse main
git rev-parse forge/v0-1
git merge-base main forge/v0-1
git remote -v
```

Expected: clean `forge/v0-1`; `main` and merge-base both equal `80860628b19c0e919dd89c8596d7d5789ad38162`; no remote exists.

- [ ] **Step 2: Add the root README**

Create `README.md` with these exact sections and claim boundaries:

```markdown
# Parallel Forge

> **Status: active v0.1 development.** The durable backend and local execution
> foundations are under active construction; the agent workflow, dashboard,
> GitHub publication controller, and final operator experience remain roadmap work.

Parallel Forge is a local-first control plane for durable, reviewable
agent-assisted software delivery. It began as the engineering system for
building Parallel, but is designed to manage other repositories independently.

## What exists today

- PostgreSQL-backed workflow state, durable commands, leases, operation intents,
  causal events, telemetry, redaction, and usage accounting
- content-addressed artifact storage and lineage
- local operator authentication and evidence-bound approval primitives
- confined repository reading and controlled Git/worktree/commit operations
- protected local secrets, isolated PostgreSQL resources, and environment staging
- Docker-first and explicit trusted-host command execution bound to exact managed
  worktrees

## Safety model

Model-driven agents never receive push, pull-request write, merge, approval,
credential, policy-write, or Forge-database authority. Remote writes are reserved
for a deterministic Release Controller and require exact human-approved evidence.

## Architecture

The FastAPI control API and separate orchestrator worker communicate through
PostgreSQL-backed commands, leases, state, events, and operation intents. A local
content-addressed store retains bounded evidence, while Forge-owned adapters bind
repository, Git, worktree, database, secret, and runner effects to controlled
interfaces. The planned Next.js dashboard and deterministic Release Controller
are architectural targets, not completed user-facing features.

## Development status and roadmap

Tasks 1-12 of the v0.1 plan are complete and independently reviewed. Task 13 has
delivered controlled Git, isolated worktrees and databases, protected secrets,
durable resource preparation, environment staging, and worktree-bound runners;
durable ordered setup orchestration and lifecycle completion remain in progress.

Later roadmap stages add controlled agent tools and contracts, planning and
delivery workflows, REST/SSE projections, the dashboard, GitHub inspection,
human-approved PR publication and merge control, evaluation, restart recovery,
cross-platform CI, and final acceptance testing.

## Prerequisites and verification

- Python 3.14
- Node.js 24
- PostgreSQL 17
- Docker
- `uv sync --frozen --extra dev`
- `docker compose up -d postgres`
- `.venv/Scripts/python.exe -m pytest -q` on Windows or
  `.venv/bin/python -m pytest -q` on POSIX
- `.venv/Scripts/python.exe -m ruff check apps/orchestrator` and
  `.venv/Scripts/python.exe -m mypy apps/orchestrator/src` on Windows, with the
  equivalent `.venv/bin/python` commands on POSIX

The final one-command development environment and dashboard are not available
yet.

## Documentation

- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Full v0.1 design](docs/superpowers/specs/2026-08-21-forge-v0-1-design.md)
- [Implementation roadmap](docs/superpowers/plans/2026-08-21-forge-v0-1.md)

## License

Copyright 2026 Clar17y. Licensed under the Apache License, Version 2.0.
```

Replace each bracketed drafting instruction with concise final prose; no bracketed instructions remain in the file.

- [ ] **Step 3: Add the canonical license**

Create `LICENSE` using the unmodified text from `https://www.apache.org/licenses/LICENSE-2.0.txt`, beginning with `Apache License` / `Version 2.0, January 2004` and ending with the standard limitations under the License. Do not append a copyright statement or modify the canonical text.

- [ ] **Step 4: Verify public metadata**

Run:

```powershell
rg -n "active v0.1 development|What exists today|Safety model|Development status and roadmap|Copyright 2026 Clar17y" README.md
rg -n "Apache License|Version 2.0, January 2004|END OF TERMS AND CONDITIONS" LICENSE
rg -n "\[Use|\[List|\[State|\[Link|TBD|TODO" README.md
git diff --check
```

Expected: required headings/copy exist; the placeholder scan exits 1 with no output; diff check is clean.

- [ ] **Step 5: Run candidate-appropriate verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/orchestrator/tests/domain/test_policy.py apps/orchestrator/tests/tools/test_worktree.py -q
.venv\Scripts\python.exe -m ruff check apps/orchestrator
.venv\Scripts\python.exe -m mypy apps/orchestrator/src
```

Expected: all selected tests and static checks pass. Documentation-only changes do not require a full suite before the independent final gate.

- [ ] **Step 6: Review and commit the metadata**

Run:

```powershell
git diff -- README.md LICENSE
git add README.md LICENSE
git diff --cached --check
git commit -m "docs: introduce Parallel Forge publicly"
```

Expected: the commit contains only `README.md` and `LICENSE`.

---

### Task 2: Gate publication with a complete-history safety scan

**Files:**
- Read: all objects reachable from `main` and `forge/v0-1`
- Temporary only: `.tmp/gitleaks-canary/`
- Temporary only: `.tmp/gitleaks-report/`

**Interfaces:**
- Consumes: exact local refs after Task 1 and pinned official scanner image `ghcr.io/gitleaks/gitleaks:v8.29.0@sha256:29c8a0572eae6d8ce620db1a4599663d43b06b0a8f90e42ded2ad5f63ac57f71`.
- Produces: a redacted PASS or an explicit publication blocker; it never creates an allowlist merely to obtain a green result.

- [ ] **Step 1: Record the exact outgoing object set**

Run:

```powershell
git rev-list --count main
git rev-list --count forge/v0-1
git rev-list --objects main forge/v0-1
git status --short --branch
```

Expected: refs resolve, the worktree is clean, and only reachable objects listed by these refs are in publication scope.

- [ ] **Step 2: Pull and identify the pinned scanner**

Run the exact digest-pinned image and print its version. Do not use `latest` or v8.30.1 because a public regression report exists for the latter's default-rule matching.

```powershell
docker pull ghcr.io/gitleaks/gitleaks:v8.29.0@sha256:29c8a0572eae6d8ce620db1a4599663d43b06b0a8f90e42ded2ad5f63ac57f71
docker run --rm ghcr.io/gitleaks/gitleaks:v8.29.0@sha256:29c8a0572eae6d8ce620db1a4599663d43b06b0a8f90e42ded2ad5f63ac57f71 version
```

Expected: version output identifies v8.29.0.

- [ ] **Step 3: Prove the scanner detects a synthetic canary**

Create an ignored temporary directory with a scanner-recognized synthetic fixture, run `gitleaks dir --redact=100 --no-banner`, and require its configured finding exit code. The canary must never be staged or committed. Remove the validated exact temporary directory after the expected finding.

Expected: the canary produces a redacted finding and nonzero finding exit; a false green blocks publication.

- [ ] **Step 4: Scan the complete reachable history**

Mount the repository root read-only and run:

```powershell
docker run --rm --mount type=bind,source="D:\Code\Parallel Forge",target=/repo,readonly ghcr.io/gitleaks/gitleaks:v8.29.0@sha256:29c8a0572eae6d8ce620db1a4599663d43b06b0a8f90e42ded2ad5f63ac57f71 git --redact=100 --no-banner --log-opts="--all" /repo
```

Expected: exit 0 and no findings. If findings occur, inspect only redacted metadata (rule, path, commit, fingerprint), distinguish synthetic fixtures from possible live material, and stop for owner review on any uncertainty.

- [ ] **Step 5: Run tracked-file and ref-boundary checks**

Run:

```powershell
git ls-tree -r --name-only main forge/v0-1
git status --short --branch
git remote -v
git tag --list
```

Assert from the file inventory that no tracked `.env` other than `.env.example`, private key, database, virtual environment, cache, `.forge`, or generated artifact exists. Confirm no remote and no tag will be published.

- [ ] **Step 6: Record the gate result**

Return a concise report containing scanner version/digest, canary result, history result, synthetic-fixture adjudications if any, exact branch SHAs, and the decision `SAFE TO PUBLISH` or `BLOCKED`. Never include matched secret material.

---

### Task 3: Create and publish the GitHub repository

**Files:**
- Modify local Git config: add exact `origin`
- External: create `https://github.com/Clar17y/parallel-forge`

**Interfaces:**
- Consumes: Task 2 `SAFE TO PUBLISH`, clean local refs, interactive GitHub CLI authentication for `Clar17y`.
- Produces: public GitHub repository with exact `main` and `forge/v0-1` refs and approved metadata.

- [ ] **Step 1: Revalidate immediately before external mutation**

Run:

```powershell
git status --short --branch
git rev-parse main
git rev-parse forge/v0-1
git remote -v
gh auth status
```

Expected: local refs match the safety report and no remote exists. Record the
expected expired-auth state but do not attempt a repository query with invalid
credentials.

- [ ] **Step 2: Re-authenticate interactively**

Run `gh auth login --hostname github.com --git-protocol https --web` in a PTY.
The operator completes the GitHub browser/device flow. Then rerun
`gh auth status`, require active account `Clar17y`, and run
`gh repo view Clar17y/parallel-forge`. Continue only when GitHub returns an
authenticated not-found response; an existing repository or another account
stops publication.

- [ ] **Step 3: Create the empty public repository**

Run:

```powershell
gh repo create Clar17y/parallel-forge --public --description "Local-first control plane for agent-assisted software delivery with durable workflow state, typed tools, human approval gates, and controlled GitHub releases."
gh repo view Clar17y/parallel-forge --json nameWithOwner,visibility,isEmpty,defaultBranchRef,url
```

Expected: exact owner/name, PUBLIC visibility, empty repository, and no unexpected initialized commit.

- [ ] **Step 4: Add the exact remote and push without force**

Run:

```powershell
git remote add origin https://github.com/Clar17y/parallel-forge.git
git push origin main:main
git push --set-upstream origin forge/v0-1
```

Expected: both ordinary pushes succeed; no force option, tag, or extra ref is sent.

- [ ] **Step 5: Configure the public landing branch and topics**

Run:

```powershell
gh repo edit Clar17y/parallel-forge --default-branch forge/v0-1
gh repo edit Clar17y/parallel-forge --add-topic ai-agents --add-topic agentic-workflows --add-topic developer-tools --add-topic human-in-the-loop --add-topic python --add-topic postgresql
```

Expected: `forge/v0-1` becomes default and only approved topics are added.

- [ ] **Step 6: Verify exact remote refs and absence of prohibited objects**

Run:

```powershell
git ls-remote --heads origin main forge/v0-1
git ls-remote --tags origin
gh pr list --repo Clar17y/parallel-forge --state all
gh release list --repo Clar17y/parallel-forge
```

Expected: remote branch SHAs exactly equal local refs; no tags, PRs, or releases exist.

---

### Task 4: Add Parallel Forge to the GitHub profile README

**Files:**
- External clone: `Clar17y/Clar17y:README.md`
- Temporary local clone: `D:\Code\Parallel Forge\.tmp\clar17y-profile-publication`

**Interfaces:**
- Consumes: published Forge URL and current authoritative profile README.
- Produces: one profile-repository commit changing only `README.md` with the approved Parallel Forge section.

- [ ] **Step 1: Clone the exact current profile repository**

Verify the explicit temporary target does not exist, create its parent if necessary, then run:

```powershell
git clone --branch main --single-branch https://github.com/Clar17y/Clar17y.git "D:\Code\Parallel Forge\.tmp\clar17y-profile-publication"
git status --short --branch
git remote -v
```

Expected: clean `main` and exact `Clar17y/Clar17y` origin. A pre-existing target or unexpected default branch stops the task.

- [ ] **Step 2: Insert the approved section only**

Use `apply_patch` to insert the exact section from `docs/superpowers/specs/2026-08-24-parallel-forge-publication-design.md` immediately after the existing Parallel section and its terminating horizontal rule. Do not reformat or change any existing text.

- [ ] **Step 3: Verify the isolated profile diff**

Run in the profile clone:

```powershell
git status --short
git diff --check
git diff -- README.md
rg -n "Parallel Forge|https://github.com/Clar17y/parallel-forge|Active v0.1 development" README.md
```

Expected: only `README.md` changed; exact approved link/status exists once; surrounding Parallel and Savvy Hampers sections are unchanged.

- [ ] **Step 4: Commit and push without force**

Run:

```powershell
git add README.md
git diff --cached --check
git commit -m "docs: add Parallel Forge to project profile"
git push origin main
```

Expected: one ordinary fast-forward push. A remote update or non-fast-forward condition stops the task; never pull/rebase/force automatically.

- [ ] **Step 5: Verify the public profile and retain truthful cleanup state**

Read back `https://raw.githubusercontent.com/Clar17y/Clar17y/main/README.md` and confirm the exact section/link. Only after verification, resolve the explicit temporary clone path, verify it is beneath `D:\Code\Parallel Forge\.tmp`, and remove that exact directory. Report whether cleanup succeeded.

---

### Task 5: Independently verify the complete publication

**Files:**
- Read-only local and remote state

**Interfaces:**
- Consumes: exact candidate commits from Tasks 1-4.
- Produces: independent evidence that publication is correct and no prohibited GitHub action occurred.

- [ ] **Step 1: Verify the local candidate**

Run:

```powershell
git status --short --branch
git log -3 --oneline
git remote -v
git rev-parse main
git rev-parse forge/v0-1
git diff --check main..forge/v0-1
```

Expected: clean tracked worktree, correct origin, unchanged `main`, and diff hygiene clean.

- [ ] **Step 2: Verify public repository metadata and refs**

Use `gh repo view`, `gh api repos/Clar17y/parallel-forge/topics`, and `git ls-remote` to assert exact owner, PUBLIC visibility, approved description/topics, default branch `forge/v0-1`, and exact remote SHAs for only `main` and `forge/v0-1`.

- [ ] **Step 3: Verify rendered public content**

Read back README and LICENSE from the default branch. Assert the active-v0.1 warning, implemented-versus-roadmap distinction, safety model, documentation links, copyright statement, and canonical Apache text. Confirm GitHub reports Apache-2.0 license metadata.

- [ ] **Step 4: Verify profile publication and prohibited-state absence**

Read back the profile README and confirm the approved section appears exactly once. Assert the Forge repository has zero pull requests, releases, and tags and that `main` was not advanced.

- [ ] **Step 5: Report the milestone**

Return the public repository link, pushed branch names/SHAs, metadata commit, profile commit, safety scan summary, verification results, and explicit statements that no PR or merge occurred. If any assertion fails, report the exact partial state without attempting destructive recovery.
