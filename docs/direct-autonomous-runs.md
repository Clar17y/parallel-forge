# Direct autonomous runs — design

Status: owner-approved direction, 2026-10-22. Not yet implemented.
Supersedes the step order in [issue #128](https://github.com/Clar17y/parallel-forge/issues/128)
for everyday local development. Motivated by
[the overhaul assessment](local-development-overhaul-assessment.md): a small
change took 6h08m, 561 tool calls and 58.3M input tokens because Forge
mediates every agent action through its own tool protocol with a cold
provider invocation per attempt.

## Principle

Forge is a thin, durable wrapper around the provider CLIs the owner already
uses (Antigravity, Codex, Muse, Muse). Agents get full autonomy inside a
session: they read, search, edit and run tests with their own native tools,
in a managed worktree, exactly as they would in a terminal. Forge never
mediates individual actions. Forge owns the run lifecycle around the
sessions: worktrees, spawning and streaming, watchdog and cancellation,
Forge-run checks, diff inspection, transcripts, and publication.

The target user is the owner, locally, with demo-ability as the bar:
type a task, say go, get a finished pull request back. Locked-down
per-tool-call approval workflows are a possible later layer, not the
foundation, and are out of scope here.

## Run shapes

Three shapes, all autonomous after "go":

- **Quick implementation** — no plan step. One implement session, then the
  standard loop below.
- **Planned work** — one plan session first: the agent investigates freely
  and returns a plan artifact. Implementation then follows automatically.
  The existing per-run workflow preference ("review plan before
  implementation", default off) remains the only way a human plan
  approval enters the flow.
- **Epic** — one decompose session returns a structured task list. Each
  task then runs the quick or planned pipeline **sequentially** on the
  epic's branch line. Parallel child worktrees are out of scope for v1.

## The autonomous loop

Every session is one coarse command with one outcome: a prompt in,
streamed output while it works, a structured final result out. Sessions
are the only unit Forge schedules; there are typically 2–5 per task,
not hundreds of tool calls.

```
[plan session] -> implement session -> Forge-run checks --fail--> repair session --+
                                          |pass                                     |
                                          v                                         |
                                  review session (different provider) --blocking----+
                                          |clean
                                          v
                                  open PR -> watch review comments -> repair session
                                          -> push -> reply + resolve threads --+
                                          |quiet (checks green, no open threads)
                                          v
                                        DONE
```

- **Checks** are Forge-run and authoritative: the project's existing
  named checks from its versioned policy. The agent's self-reported test
  results are advisory. An identical-tree short-circuit applies: a
  candidate whose tree digest already has recorded check results is
  never revalidated.
- **Review** is a fresh session from a **different provider** than the
  implementer, returning structured findings. Blocking findings feed a
  repair session with the findings as its brief.
- **Repair** sessions receive exactly what failed: check evidence
  (the #136 payload), review findings, or PR comments verbatim.
- **Round caps:** at most 3 check/review repair rounds and 3 PR-comment
  rounds (owner-editable settings). Exhaustion returns the run to the
  owner with the current state, never a silent loop.
- **Human involvement** happens only when: a session returns a genuine
  decision request (structured, with options), a round cap is hit, a
  session/process dies unrecoverably, or the PR is ready to merge.
  Owner steering can be sent at any time and is delivered to the next
  session; it is never required.
- **"Done" means the PR is open, green and quiet**: checks passing, no
  unresolved review threads, no outstanding blocking findings. Merge
  remains the owner's immediate explicit action, always.

## Sessions and providers

One spawn interface for every CLI: working directory = managed
worktree, prompt in, streamed stdout/stderr persisted as the transcript
artifact, exit code out, whole process tree killable. A watchdog tracks
last-output time: silence beyond a threshold warns visibly, longer
silence kills the session and settles it as failed. Cancellation uses
the existing proved-stop machinery where available.

Each role (plan, implement, review, repair, decompose) has an ordered,
owner-editable route list. Defaults at time of writing:

| Role | Route order |
| --- | --- |
| Implement / repair | Antigravity (Gemini 3.8 Flash medium) → Codex → Muse |
| Review | Codex, or the first route whose provider ≠ implementer's |
| Plan / decompose | Codex → Muse → Antigravity |

Quota is a routine event, not a failure of the run: if a session exits
quota-exhausted (or a route is known-exhausted before launch), Forge
launches the next route in the same worktree with a short handoff note
(task, plan artifact if any, git status/diff summary, what the previous
session reported). The work product is the files and git state, so
switching providers mid-task is survivable by construction. Muse is
deliberately last for implementation because its allowance burns
fastest in practice.

Context passed between sessions is artifacts, not transcripts: the
task text, the plan (when planned), the previous sessions' final
summaries, failure evidence, and findings. Each session rebuilds its
own working context from the repository, warm, with its native tools.

## Trust posture

This design **replaces** the per-action mediation posture for direct
runs. The agent runs as the owner, with the owner's access, inside the
managed worktree — the same trust as running the CLI by hand, which the
owner has explicitly accepted for local personal use (the existing
`operator_trusted` admission already anticipates this). The mitigations
that remain, because they are cheap and do not slow the loop:

- Worktree isolation: the canonical checkout is never the session cwd;
  only the worktree's diff can become a candidate.
- Post-hoc verification: Forge inspects the resulting diff (scope
  against owned paths where a contract names them), runs its own
  checks, and requires review before the PR is called done. Nothing is
  believed because a session said so.
- Bounds: per-session timeout and silence watchdog, per-run round caps,
  provider step caps where the CLI supports them (e.g. Muse
  `max_model_steps`), and cancellation at any time.
- Persistence hygiene: transcripts and summaries are persisted
  redacted and bounded, as today. Credentials never enter prompts,
  artifacts, or run records.

`AGENTS.md`'s controlled-command-runner section must be amended when
this is implemented to state this posture explicitly for direct runs,
rather than the current blanket named-tools-only rule. The named-check
runner itself (Docker, exact policy names, no agent-supplied shell) is
**unchanged**: it constrains what Forge executes for checks, which is
a different thing from what an agent session may do in its worktree.

## What is reused, what goes dormant

Reused: projects and runs in PostgreSQL, managed worktrees, the
API/worker process split, subscription installations and route
registration, launch/stop-proof records, quota state, named checks and
the Docker runner, candidate inspection, PR publication and its
approval evidence, the web cockpit shell, workflow preferences, and
#136/#137/#138's evidence, steering and checkpoint payloads (as
session briefs).

Dormant for direct runs (unchanged, still used by existing run types):
the Forge tool protocol and per-attempt invocation loop, decision
application machinery, budget debits per tool call, and per-action
evidence manifests. Direct runs are a new run type beside the existing
paths; nothing is deleted in v1, and existing runs keep their frozen
behavior.

## v1 scope and sequencing

1. **Vertical slice:** quick implementation end-to-end — spawn/stream/
   watchdog, implement session, Forge checks + identical-tree
   short-circuit, cross-provider review, repair loop, PR open +
   PR-comment loop, done state. New-run form offers it as "Direct".
2. Planned shape (plan session + preference wiring).
3. Epic shape (decompose session, sequential child tasks reusing the
   same pipeline).
4. Route-list settings UI and quota-fallback polish.

Non-goals for v1: parallel epic children, per-action approvals or
allowlists, Docker-sandboxed sessions, token-level budget enforcement
(usage is reported where the CLI reports it), and any change to merge
authorization.

## Measuring success

Re-run the trial task class (small real change, ordinary path) and
compare against the assessment's numbers: 6h08m elapsed, 561 tool
calls, 58.3M input tokens, 4 full validations, 0 useful owner actions
available mid-run. The owner sets the acceptance bar before the
measuring run (elapsed time, input tokens, session count, owner
actions required). If the slice does not improve the economics by
roughly an order of magnitude, stop and re-scope rather than layering
more machinery on top.

