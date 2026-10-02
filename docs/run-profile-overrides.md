# Per-run subscription profile overrides

Run creation uses the project's selected subscription profile by default. An
operator may instead freeze any existing immutable profile version on one run;
this does not change the project's selection:

```powershell
forge run create --task-id 00000000-0000-0000-0000-000000000000 --idempotency-key local-run-1
forge run create --task-id 00000000-0000-0000-0000-000000000000 --idempotency-key local-run-2 --profile-id 00000000-0000-0000-0000-000000000000 --profile-version 2
forge run show --run-id 00000000-0000-0000-0000-000000000000
```

The run creation transaction freezes the profile routes and safety-policy
version together with the run, event, planning command, and idempotency receipt.
Retries with the same key and selection return that original run. Reusing a key
with another profile selection conflicts. The CLI uses its stable local
operator identity and does not create a browser session.

The run API and cockpit expose the frozen profile ID, version, and selection
source (`project_default` or `run_override`). Older runs that have a frozen
profile but predate provenance report an unknown source. A run without an
envelope has no frozen profile.
