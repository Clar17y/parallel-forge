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
An already-paused child needs no second pause command. A paused child retains
its prior approval gate in
the run suspension context; the execution projection exposes both the current
`pending_gate` and `retained_gate`. A refused command leaves the execution
`BLOCKED` with its actual child state and refusal visible. Cancellation cannot
settle while any child process, command, or effect remains unresolved. An owner
may manually admit a sibling while a control is pending; its new work prevents
the old control from reporting a falsely settled aggregate state.

The shared epic ledger is versioned and spans authoring jobs, epochs and child
subscription attempts. The owner edits limits through `PUT /api/epics/{id}/budget`
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
internal attempt. Exact subscription reservation and consumption rows replace
that floor as they become authoritative. The worker lifts a terminal child
floor only after `prove_quiescent` observes command, process and effect
settlement. Legacy children without provable telemetry retain unknown
exposure. A new bridge child also waits for authoritative quiescence of every
earlier bound child, including terminal children with fully known usage; the
owner may explicitly proceed with the unsettled-effect warning recorded.
Source freezing and these controls do not perform automatic item
eligibility, dispatch or Git integration proof.
