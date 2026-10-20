# Ordinary development

New local runs using a subscription profile prepare internally and let the
selected primary investigate, implement, run named checks, and repair failures.
A tested local diff is a useful result before a pull request exists. The run
keeps the selected primary route and the existing managed-worktree, command,
evidence, budget, and recovery boundaries.
Changed owned files are recorded in a local commit before the candidate closes,
so the existing finalizer can retain a complete tested diff. An unchanged task
does not require an empty commit. A local commit grants no publication authority.

The new-run form and run cockpit expose these owner preferences:

| Preference | Default | Owner choice |
| --- | --- | --- |
| Review plan before implementation | Off | Turn on to wait for the existing evidence-bound plan approval. |
| Delegation | Auto | Disable child delegation or request it within the frozen profile and policy. |
| Optional review | Auto | Skip optional review or require review using the configured routes. |

Preferences are stored on each run. Updating them does not replace the frozen
subscription profile or approve publication. A stale edit must be refreshed;
retrying an unchanged request retains its original operation identity.
Edits apply to future decisions. Completed preparation and review retain their
historical evidence; changing a preference does not rerun those completed steps.

An unanswered material question stops preparation. Answer it through the
existing plan-revision flow. A preference change or plan approval does not
invent the missing answer. Pause and cancel continue to fence execution, and
resuming retains existing work and accepted owner preferences.

Internal preparation records the originating owner action and exact preparation
evidence separately from human plan approval. Existing runs without workflow
preferences, legacy controller runs, and epic children retain their existing
frozen workflow. They are not silently converted to the ordinary path.

When implementation and the required checks finish, inspect the changed files,
diff, and actual check receipts in the cockpit. Publication still requires the
owner's explicit action on the concrete candidate. Merge requires its own
immediate human authorization.

This change implements [issue #130](https://github.com/Clar17y/parallel-forge/issues/130)
from [roadmap #128](https://github.com/Clar17y/parallel-forge/issues/128). Quick
Questions, dashboard redesign, runner performance, provider-adapter rewrites,
and Forge-owned publication acceptance remain separate work.

See [the validation record](ordinary-development-validation.md) for focused
coverage, real UI measurements and retained limitations.
