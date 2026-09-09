# Evaluation check evidence

`forge eval run --suite deterministic` uses the explicit fake gateway and synthetic
fixture observations. These are deterministic metric/regression fixtures, not
proof that a live model executed repository commands. Live suites require a
provider reference and use production controlled tools. Docker remains the default;
trusted-host fixture execution must be explicitly selected by an operator/integrator.

A developer fixture may declare `check_commands`, using the same exact named
`CommandSpec` schema as project policy. Its names must exactly match
`required_checks`; agents still request names, never argv. Fixtures without this
field retain the pytest command convention. The basic-change fixture runs its
standard-library assertion script, so it needs no pytest package inside the runner.

A check that supplies individual test/assertion evidence prints one line beginning
`FORGE_EVAL_REPORT_V1:` followed by this JSON shape:

```json
{"report_version":1,"fixture_version":"eval-fixture-v1","case_key":"developer/basic-change","command_name":"pytest","tests":{"test_app.py":true},"assertions":{"greet_returns_hello":true}}
```

The fixture-owned check harness must be outside the developer case's
`allowed_paths`; the basic-change fixture uses `run_checks.py`. Forge snapshots
that harness from the fixture template and verifies the actual managed worktree
before and after execution. Bound tool calls are serialized, so a concurrent
controlled write cannot race this observation. A missing pre-check or either
integrity failure rejects the report, including a self-restoring forged harness. A
harness must not import or execute arbitrary candidate code while producing a
report. The basic-change harness permits only a pure `greet` return expression
made from string literals, its argument, f-strings, and `+`; it separately
validates and executes submitted no-argument test assertions that may call only
that validated function. Literal string defaults and simple `str` annotations are
accepted; executable defaults, assertion messages, extra parameters and duplicate
test names are rejected. Only rebuilt, validated syntax is executed. The evaluator reads only bounded, content-addressed stdout
linked through the successful controlled tool receipt. It checks the tool-call
identity, command name, fixture/case version and declared result names. Model
output cannot supply these scores. Failed, cancelled, truncated, malformed,
duplicate or missing reports earn no test/assertion credit. Missing individual
results remain failures. A subsequent failed check invalidates its prior report;
all required check reports must be present before combined credit is awarded.

The receipt and output artifacts remain available through the evaluation's run
and tool-call records. Inspect them when a command passes but required-test or
assertion scores fail. Promoted baselines compare only equal fixture/metric
versions; changing a fixture's checks or reporting semantics requires a new
fixture version once that fixture has been published.

Each admitted fixture has its own durable Project, Run and Step.  A live
fixture settles those records only after its managed worktree teardown succeeds.
If teardown cannot be reconciled, Forge records an operator intervention and
retains the temporary fixture path for recovery; it does not delete that path or
invent a delivery approval.  Replaying an already settled case does not prepare
or tear down any fixture resources.
