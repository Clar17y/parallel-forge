"""Immutable, credential-free row and blob evidence from a disposable v0.1 DB."""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

from alembic import command
from forge.domain.operation import canonical_digest
from forge.evaluations.credentials import assert_credential_free
from sqlalchemy import text
from sqlalchemy.engine import make_url

LEGACY_REVISION = "20260909_0006"
# A closed set of fixture evidence tables. Authentication/session/challenge and
# credential-store tables are deliberately outside the snapshot contract.
TABLES = (
    "projects",
    "project_policy_versions",
    "tasks",
    "runs",
    "run_commands",
    "run_events",
    "steps",
    "approvals",
    "agent_executions",
    "tool_calls",
    "model_usage",
    "artifacts",
    "artifact_lineages",
    "artifact_lineage_parents",
    "validation_results",
    "evidence_sets",
    "agent_execution_evidence_inputs",
    "reviews",
    "operation_intents",
)


def _ordered(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


async def snapshot_rows(session_factory):
    async with session_factory() as session:
        result = {}
        for table in TABLES:
            rows = (await session.scalars(text(f'SELECT to_jsonb(t) FROM "{table}" AS t'))).all()
            assert len(rows) < 10000
            result[table] = _ordered(rows)
        assert len(result["runs"]) == 1, "snapshot is restricted to this isolated fixture DB"
        return result


async def blobs_for(case, rows):
    blobs = {}
    for descriptor in rows["artifacts"]:
        digest = descriptor["digest"]
        value = await case.store.open_bytes(digest, max_bytes=8 * 1024 * 1024)
        assert len(value) == descriptor["size_bytes"]
        assert hashlib.sha256(value).hexdigest() == digest
        assert_credential_free(value.decode("utf-8"))
        blobs[digest] = value
    return blobs


async def upgrade_legacy_case(case, session_factory, database_url, config):
    assert re.fullmatch(r"forge_test_[0-9a-f]{32}", make_url(database_url).database or "")
    await case.handlers.aclose()
    # This exercises the retained legacy service under the final v0.1 schema;
    # it does not claim execution of the historical v0.1 binary.
    await asyncio.to_thread(command.downgrade, config, LEGACY_REVISION)
    await upgrade_existing_legacy_case(case, session_factory, database_url, config)


async def upgrade_existing_legacy_case(case, session_factory, database_url, config):
    """Compare a real v0.1 schema snapshot through two additive head upgrades."""
    assert re.fullmatch(r"forge_test_[0-9a-f]{32}", make_url(database_url).database or "")
    async with session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version")) == LEGACY_REVISION
        )
    frozen = await snapshot_rows(session_factory)
    original_blobs = await blobs_for(case, frozen)
    for _ in range(2):
        await asyncio.to_thread(command.upgrade, config, "head")
        current = await snapshot_rows(session_factory)
        for table, old_rows in frozen.items():
            assert len(current[table]) == len(old_rows)
            if old_rows:
                columns = set(old_rows[0])
                assert all(columns <= set(row) for row in current[table])
                projected = [{key: row[key] for key in columns} for row in current[table]]
                assert _ordered(projected) == old_rows, table
        assert await blobs_for(case, current) == original_blobs
    case.legacy_rows, case.legacy_blobs = frozen, original_blobs


async def retain_legacy_manifest(
    case, session_factory, tmp_path, *, scenario, controls=(), source_provenance=None
):
    current = await snapshot_rows(session_factory)
    blobs = await blobs_for(case, current)
    assert all(blobs[digest] == value for digest, value in case.legacy_blobs.items())
    value = {
        "schema_version": 1,
        "scenario": scenario,
        "legacy_schema": LEGACY_REVISION,
        "proof_limits": [
            (
                "pinned historical v0.1 source in a separate interpreter; current test driver"
                if source_provenance is not None
                else "current retained legacy service, not the historical v0.1 binary"
            ),
            "scripted provider; trusted-host local Git and controlled tools",
            "guarded disposable PostgreSQL database; no production upgrade or rollback claim",
        ],
        "before_upgrade": case.legacy_rows,
        "after_controls": current,
        "before_row_digest": canonical_digest(case.legacy_rows),
        "after_row_digest": canonical_digest(current),
        "controls": list(controls),
        "provider_requests": [
            {
                "execution_id": str(request.execution_id),
                "role": request.role.value,
                "provider": request.provider,
                "model": request.model,
                "instruction_version": request.instruction_version,
                "instruction_digest": request.instruction_digest,
            }
            for request in case.gateway.requests
        ],
        "artifact_bytes": {digest: len(blob) for digest, blob in blobs.items()},
        "repeated_head_upgrade": True,
        **({"source_provenance": source_provenance} if source_provenance is not None else {}),
    }
    assert_credential_free(json.dumps(value, ensure_ascii=False))
    configured = os.environ.get("FORGE_ACCEPTANCE_OUTPUT_ROOT")
    output = Path(configured).resolve() if configured else tmp_path / "legacy-acceptance"
    if configured:
        scratch = (Path(__file__).resolve().parents[4] / ".llm-output").resolve()
        assert output.is_relative_to(scratch) and output != scratch
    output = output / str(case.run.id)
    output.mkdir(parents=True, exist_ok=False)
    for digest, blob in blobs.items():
        with (output / digest).open("xb") as stream:
            stream.write(blob)
    manifest = {"manifest_digest": canonical_digest(value), "evidence": value}
    path = output / "manifest.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    assert (
        canonical_digest(json.loads(path.read_text(encoding="utf-8"))["evidence"])
        == manifest["manifest_digest"]
    )
    return path
