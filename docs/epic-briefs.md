# Saved epics and requirements briefs

An epic belongs to one registered project and keeps a title, an editable draft,
immutable brief revisions, and an explicitly accepted revision. This foundation
does not create delivery tasks or runs. Work-item graphs, brainstorming jobs,
execution, and the epic screens are separate v0.3 deliverables.

Every brief contains a problem, outcomes, scope, exclusions, requirements,
decisions, assumptions, and open questions. Requirements have caller-supplied
stable UUIDs, text, and ordered acceptance criteria. Keep an existing requirement
ID when editing its text.

## Save, revise, and accept

Create an epic with `POST /api/epics`, supplying `schema_version: 1`,
`project_id`, `title`, and an optional `draft`. List a project's epics with
`GET /api/epics?project_id=…`, and reopen one with `GET /api/epics/{epic_id}`.

Use `PATCH /api/epics/{epic_id}` to replace the title and draft. Supply
`schema_version: 1`, `expected_epic_version`, `title`, and `draft`.
The response contains the new version. Incomplete drafts can be saved.

To keep a historical snapshot, use
`POST /api/epics/{epic_id}/brief-revisions` with `schema_version: 1`,
`expected_epic_version`, and `content`. This appends an immutable revision,
updates the saved draft, and increments the epic version in one transaction.
The revision response includes its `brief_revision_id`, `revision_number`,
`epic_version` at creation, and computed `content_digest`.
History and individual revisions remain available through the corresponding
`GET .../brief-revisions` and `GET .../brief-revisions/{brief_revision_id}` routes.

Saving a draft or revision leaves the accepted selection in place. Explicitly
select a revision using `POST /api/epics/{epic_id}/brief-adoptions` with the
[frozen adoption body](v0.3-design.md): `schema_version`,
`expected_epic_version`, `brief_revision_id`, and `brief_digest`.
An accepted brief needs a nonblank problem, an outcome, and a requirement;
each requirement needs an acceptance criterion.
Read the selected immutable content with
`GET /api/epics/{epic_id}/accepted-brief`. Without a selection this returns 409.

Selecting a different brief ID or digest clears both accepted graph pointers
atomically. Selecting the same brief again preserves the graph selection.
Previously saved revisions and existing tasks, plans, runs, and approval
evidence remain unchanged.

## Versions, replays, and input limits

All reads require the existing operator session. Every mutation also requires
CSRF protection and an `Idempotency-Key`. Edits, revision saves, and adoptions
require the current `expected_epic_version`; a stale mutation returns 409.
Creation has no existing version to compare.

Retry a request with its original key and body to receive its original response,
even after later edits or adoptions. A replay does not increment the version,
append a revision, or add another audit event. Reusing a key with a different
body or target conflicts. Use the GET projection for the latest state.

Bodies reject unknown fields and schema versions. Titles are nonblank and at
most 256 UTF-8 bytes. Problems allow 10,000 characters; list entries, requirement
text, and criteria allow 5,000 characters each. Prose lists, requirements, and
criteria each allow at most 64 entries. Complete brief JSON is limited to
131,072 UTF-8 bytes. Raw credentials are rejected by Forge's durable-payload
boundary. These texts remain untrusted input for later authoring and planning.

Responses explicitly include nullable accepted brief and graph IDs/digests.
Revisions include a nullable `source_job_id`; operator requests cannot set it.
All records use schema version 1 and Forge's existing canonical digest.

## Storage and upgrade

PostgreSQL stores the drafts, immutable revisions, accepted pointers, mutation
receipts, and audit evidence. A same-epic, exact-digest foreign key protects brief
selection. Database triggers reject revision updates and deletion.

Migration `20261003_0031` adds empty `epics` and `epic_brief_revisions` tables.
It preserves existing v0.1/v0.2 records. Empty new tables can be downgraded;
a downgrade with saved epic data fails without discarding the records.
Follow the [stopped-upgrade and forward-repair policy](v0.2-upgrade.md).
