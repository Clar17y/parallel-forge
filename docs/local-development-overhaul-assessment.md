# Local development overhaul assessment — 2026-10-21

## Verdict

Forge is not currently a competitive way to do everyday local development.
Issue #128's own acceptance test is: "would the owner choose Forge over
opening a coding agent directly for tomorrow's small fix?" On the measured
evidence below, the answer is no, by roughly an order of magnitude in both
time and tokens. The three fixes landed alongside this assessment (#136,
#137, #138) repair specific failure modes found in the live trial; they do
not change the underlying economics. Continuing the incremental roadmap
(#132, #133) before addressing those economics would polish an experience
that is not yet viable.

This assessment records the evidence, the architectural root causes, and
what an overhaul would have to change. It does not propose a rewrite from
scratch (#128 explicitly rejects that), and it keeps the parts that work:
PostgreSQL durability, managed worktrees, cancellation/recovery, and the
no-mandatory-approval ordinary path.

## Evidence

Live ordinary-path trial, 2026-10-21, run
`b74370a9-ebd6-4f21-a549-f56d90e91a6e` on merged `main` `e24ea87`.
Task: surface concrete Antigravity protocol-failure detail — a small
change. The run was cancelled by the owner after repairs exhausted.
Full record: comment on #129 (`6323605663`). Usage figures below were
recomputed from `subscription_attempts` telemetry in `forge_v02`.

| Attempt | Role | Input tokens | Calls | Duration |
|---|---|---|---|---|
| 1 | Planning | 14,792,768 | 128 | 1113s |
| 2 | Implementation | 26,903,578 | 176 | 2943s |
| 3 | Acceptance | 139,567 | 4 | 68s |
| 4 | Repair 1 | 3,800,353 | 32 | 830s |
| 5 | Acceptance | 103,129 | 4 | 71s |
| 6 | Repair 2 | 8,343,554 | 131 | 1131s |
| 7 | Acceptance | 105,064 | 4 | 59s |
| 8 | Repair 3 | 4,074,065 | 81 | 851s |
| 9 | Acceptance | 43,481 | 1 | 41s |
| **All** | | **58,305,559** (55,514,152 cached) | **561** | **6h08m elapsed** |

- Implementation resent on average ~153k input tokens per tool call
  (26.9M / 176). 95% was prompt-cache hits, which reduces price but not
  latency, quota burn, or fragility.
- Four controller validations ran at ~55 minutes each (web suite ~35 min,
  backend pytest 638s; see #121). Roughly 3.7 of the 6.1 hours were spent
  re-validating a candidate that the repair attempts had not changed.
- Tool execution is not the bottleneck: search/read execute in ~0.05s.
  The cost of every call is a model round trip.
- Pre-#135 baseline from the same database (28 tasks, 81 attempts): 32%
  of investigation calls in attempts ≥2 (308/962) were exact duplicates
  of a call an earlier attempt had already made. In this trial,
  implementation still duplicated 14% (20/144) of planning's investigation
  calls because the checkpoint did not cross the plan→implementation
  contract revision (fixed by #138, not yet live-measured).
- All three repair attempts re-ran only their path-scoped in-run check,
  saw green, made zero writes, and resubmitted the identical candidate.
  The one failing test (1 failed / 6251 passed) reproduced locally in
  2.5s once a human looked at it.
- The owner could not intervene usefully mid-run: worker feedback
  excluded the primary, and revision commands exist only at gates
  (fixed by #137, not yet live-proved).

For contrast, a direct coding-agent session doing the same task class
reads the same files in a handful of batched calls, keeps one warm
context, and runs the one failing test file in seconds. That is the
competition #128 names, and the gap is structural, not a matter of
prompt tuning.

## Root causes

### 1. Stateless per-attempt provider invocations

Every attempt is a fresh, cold provider invocation. This is deliberate:
`apps/orchestrator/src/forge/agents/claude_gateway.py` passes
`--no-session-persistence`, and `muse_runtime.py` passes
`--no-session-log`. The durability model (PostgreSQL as system of
record, attempts as the unit of recovery) is sound, but the consequence
is that context is rebuilt and resent constantly: within an attempt the
CLI resends history per turn (~153k tokens/call above), and across
attempts everything starts over except what the checkpoint carries.
Nine attempts for one small change means nine cold starts.

### 2. A chatty, one-file-at-a-time tool protocol

Repository investigation is one model round trip per operation:
`read_file` takes a single path
(`apps/orchestrator/src/forge/tools/repository.py`,
`RepositoryReader.read_file`), search is one query per call, and
results return without enough surrounding content to avoid follow-up
reads. Planning alone made 128 calls, 104 of them searches. Each call
costs seconds of model latency to do 0.05s of work. Direct agents batch
reads and return content-bearing search results; Forge's protocol
cannot express either.

### 3. Phase and attempt fragmentation

Planning, implementation, acceptance, and each repair are separate
attempts with separate contracts (`task_digest` changes at the
plan→implementation revision). Until #138 the checkpoint stopped at
that boundary, so the most expensive transition in every run restarted
investigation. Acceptance sub-attempts add three more cold starts
(~350k tokens here) to perform checks a controller could run without a
model at all.

### 4. Validation economics

Controller validation runs the project's full named checks in Docker on
every candidate, unchanged or not: ~55 minutes per pass, four passes,
zero candidate changes between three of them, and no per-check progress
events during the silence (#121 covers raw runner speed). Meanwhile the
in-run check the primary can see is path-scoped, so the primary's view
("green") and the controller's view ("1 failed") diverged — and until
#136 the failure detail never reached the repair attempt. There is no
diff-scoped test selection at the controller level and no
identical-tree short-circuit.

### 5. Broken human-in-the-loop paths (now fixed, not yet live-proved)

Repairs were blind (#136: the primary role could not call
`validation-results.read`, and no failure detail was pushed into the
handoff) and the owner had no steering channel to a sole primary
(#137). These turned one trivial test failure into three wasted repair
attempts and a cancelled run. The fixes are correctness repairs; they
prevent this exact loop, but they do not make the loop cheap when it
legitimately happens.

### 6. Operability traps

- A process-level `FORGE_SUBSCRIPTION_INSTALLATIONS_PATH` silently
  shadowed `.env`, so the worker registered 2 of 7 routes and the run
  queued against a missing route. The queue diagnostic named the exact
  problem (a genuine strength), but the trap itself should not exist
  silently; `scripts/dev.py` now warns on shadowed dotenv values.
- The first bootstrap sign-in link printed at stack start had expired
  by first use.
- The UI flickered on "Forge is preparing a plan" during long attempts
  and offered no per-check validation progress; the usage report
  omitted a provider the owner expected to see.
- The managed-worktree base is the canonical repo's local default
  branch. In this repository local `main` (`f2c98c3`) has diverged from
  `origin/main` (18 local-only vs 4 origin-only commits), so a
  self-development candidate cannot be published as a clean PR to
  GitHub `main` without a separate reconciliation the workflow does not
  surface until publication time.

## What the three fixes do and do not change

Landed with this assessment, each verified by focused tests but **not**
by a live run (the owner cancelled the planned re-trial on 2026-10-21 in
favour of this assessment; that is an explicit unproved case):

- **#136** pushes bounded failing-test evidence into the repair handoff.
  Prevents blind repairs. Does not reduce the cost of a repair attempt
  that still has to happen.
- **#138** carries the prior-attempt checkpoint across exactly one
  boundary — the approved-plan contract revision. Should reduce the 14%
  plan→implementation duplication. Does not help within-attempt resend
  or arbitrary contract changes.
- **#137** adds an owner steering channel to a sole primary (new
  `subscription_primary_feedback` table, exactly-once delivery at the
  next attempt boundary, inspector UI). Restores human agency mid-run.
  Does not make runs shorter.

None of them touch root causes 1, 2, or 4, which dominate the measured
cost. Forge-owned publication (#129) also remains unproved.

## What an overhaul would have to change

Ordered by expected impact on the measured numbers. Each item is a
design decision for the owner, not a commitment; several trade against
the current durability/recovery model and need explicit semantics.

1. **Warm sessions per task.** Resume or keep alive a provider session
   across the attempts of one task (or run one long-lived agent process
   per run), with PostgreSQL retaining the durable record and a defined
   rebuild path when a session is lost. Targets the ~153k-token/turn
   resend and the nine cold starts. This is the single largest lever
   and the deepest change: session identity, expiry, and recovery must
   be designed against the existing attempt/lease machinery, and some
   providers' CLIs may not support resumption Forge can rely on.
2. **Batched, content-bearing repository tools.** Multi-path reads,
   glob+read in one call, search results that include the matching
   content windows, and a one-call project map. Targets the 561-call
   count directly; a 5–10x reduction in round trips is plausible
   without any model change. Bounded/redacted output rules still apply.
3. **Incremental validation.** Diff-scoped test selection at the
   controller (run the full suite at publication, not per repair), an
   identical-tree short-circuit so an unchanged candidate is never
   revalidated, parallel named checks and runner speed work (#121),
   and streamed per-check progress. Targets the ~3.7 hours of
   validation in this trial.
4. **Collapse phases for small work.** One attempt that plans and
   implements for ordinary tasks; acceptance as controller-run checks,
   not model sub-attempts. The ordinary path (#134) removed the
   approval click but kept the separate planning invocation.
5. **Context discipline.** Cap what is resent: concise project context
   injected once, checkpoint summaries instead of transcripts (partly
   done in #135/#138), cheaper routes for investigation phases, and
   visibility of per-turn resend in the usage report so regressions are
   obvious.
6. **Operability.** Fail fast on shadowed environment configuration
   instead of warning, non-expiring-until-used bootstrap, honest
   progress events during validation, and surfacing base-branch
   divergence at run creation rather than publication.

## Recommendation

- Pause roadmap steps 4–5 (#132 interface work, #133 internal
  simplification) until the economics above are addressed; a clearer UI
  on a 6-hour run does not make the run acceptable.
- If Forge continues, prototype items 1+2+3 behind the existing
  ordinary path and re-run this same task class as the measuring stick.
  The owner should set the acceptance bar explicitly (elapsed time,
  input tokens, tool calls for a small fix) before the prototype starts;
  #128 forbids inventing targets after the fact.
- If the prototype cannot get within striking distance of a direct
  coding-agent session on that measuring stick, the honest conclusion
  is that Forge's value is in its durable orchestration/evidence
  machinery for larger, delegated work — not in everyday local
  development — and the roadmap should be re-scoped to say so.

## Unproved and retained

- #136/#137/#138 have no live-run evidence; their tests are cited in
  the accompanying PR. Before/after duplicate-investigation rates are
  unmeasured live.
- Forge-owned PR publication remains unproved (#129 stays open).
- The full backend suite was not run locally for these fixes. Focused
  and neighbouring suites passed (71 passed in the feedback/builder/
  checkpoint/repair group); two pre-existing
  `test_cancelled_run_releases_its_stopped_attempt_for_teardown`
  failures reproduce independently of these changes and are recorded,
  not waived.
- Trial artefacts are retained locally: run `b74370a9…` (CANCELLED),
  candidate `51f8f0c`, its managed worktree, and pre-migration database
  backups. Nothing from the trial was published.


