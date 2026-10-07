# Epic execution lifecycle and shared budget

An execution freezes one saved brief revision and matching graph revision. The
ordinary start action selects the accepted pair. An authenticated owner may
select another saved matching pair or start while another epoch is active; the
operation records the source, actor and warnings. Replaying the same mutation
key returns its original source receipt. Legacy implicit epochs remain readable
with no invented control sidecar.

`epic_execution_controls` holds the mutable version and state. Pause, resume and
cancel record durable intents for children that need commands or effect
settlement. The separate worker checks the current execution version and
enqueues each normal run command in one transaction retaining the epic then run
locks. It observes command acknowledgement separately from the child's latest
state and rechecks every bound child before settling the execution aggregate.
Settled completed, failed and cancelled children remain historical participants;
controls preserve their outcomes and enqueue no new commands for them. Terminal
children with pending effects remain observed until quiescence is proved. An
already-paused child needs no second pause command. A paused child retains
its prior approval gate in
the run suspension context; the execution projection exposes both the current
`pending_gate` and `retained_gate`. A refused command leaves the execution
`BLOCKED` with its actual child state and refusal visible. Cancellation cannot
settle while any child process, command, or effect remains unresolved. An owner
may manually admit a sibling while a control is pending; its new work prevents
the old control from reporting a falsely settled aggregate state.

The shared epic ledger is versioned and spans authoring jobs, epochs and child
provider invocations, including ordinary API model usage and subscription attempts.
Ordinary usage is bound to its persisted agent execution; subscription
consumption retains its existing reservation and account lineage. Each invocation
is counted once. The owner edits limits through `PUT /api/epics/{id}/budget`
with an expected version and idempotency key. `disabled_dimensions` explicitly
unsets any of the six aggregate caps while retaining its prior numeric setting
for later re-enable: `duration_ms`, `tool_call_count`, `input_tokens`,
`output_tokens`, `estimated_api_cost_minor`, and `provider_attempts`. The
per-run and per-attempt `TaskBudget` remains authoritative separately. Unknown
or incompatible historical usage is never rewritten to zero. The read
projection shows known, held and unknown capacity, currency lineage, and owner
actions. A one-use owner permit can admit the next exact child run attempt past
epic budget warnings; normal run, provider quota and stop fences still apply.
Positive cost enters the known total only with matching persisted telemetry,
route, quota-admission account and currency. Missing lineage retains the raw
observation as unknown exposure. Conflicting currencies have no comparable
aggregate minor-unit total; their original usage records remain intact and
default admission refuses the unknown capacity.
An authoring job refused before its first provider attempt has no process to
settle and remains eligible for a direct owner retry or an edited ceiling. Its
usage is still unknown until measured; an owner retry authorizes one next
claim without rewriting that fact or changing the frozen invocation budget.
Authoring telemetry uncertainty follows its configured allowance. A positive
cost with missing currency or quota lineage requires a direct owner retry,
retains the raw observation as unknown exposure, and consumes that authority
only when the next claim is admitted.

Scheduler admission first advances a durable round-robin cursor over at most
128 queued or expired run IDs, locks their bound epics then ledger rows in
stable order, and only then takes its advisory
lock and candidate/cleanup run locks. It claims and cleans only that prepared
snapshot; newly queued or expired work waits for a later poll. A bridge child
hold is a floor on unresolved exposure, not a second charge for the same
internal attempt. Persisted ordinary model usage and exact subscription reservation/consumption
rows replace that floor as they become authoritative. The worker lifts a terminal child
floor only after `prove_quiescent` observes command, process and effect
settlement. Legacy children without provable telemetry retain unknown
exposure. A new bridge child also waits for authoritative quiescence of every
earlier bound child, including terminal children with fully known usage; the
owner may explicitly proceed with the unsettled-effect warning recorded.
Execution controls preserve source and lifecycle authority. The separate
eligibility producer verifies settled child merge evidence against the actual
project branch and successor baseline; a completed run or published PR alone
cannot satisfy a prerequisite. Readiness and completion evidence appear in the
ordinary execution projection. Verification uses the project's local canonical
default-branch baseline. A remote merge alone leaves the successor blocked until
the owner synchronizes that baseline; dispatch does not fetch or rewrite the
canonical repository.

Sequential coordination is disabled by default. The authenticated owner enables
or disables it with `PUT /api/epics/{epic_id}/executions/{execution_id}/dispatch`,
an idempotency key and `expected_dispatch_version` (zero before configuration).
The setting binds to the execution's frozen source and may select an existing
profile/version pair. The separate worker admits ready required items through
the ordinary run bridge. Each child retains its own plan, PR-publication and
immediate merge approvals. Accepting a brief or graph does not grant those
approvals. The execution projection retains dispatch state, blockers, claims,
child links and verified handoff evidence. Before claiming a ready item, the
worker waits for active or unsettled bound children across the epic, including
manual children and earlier epochs. Expected sequencing waits preserve enabled
dispatch; the bridge retains its final admission fence. A genuine admission failure disables
automatic dispatch with its actual blocker. After resolving the warning or
editing a limit, the owner can directly re-enable the current dispatch version;
the failed admission has no automatic retry allowance. Earlier effects and usage
remain recorded. See the
[release evidence](v0.3-release.md) for the cases actually validated.
