"""Stopped disposable upgrade, coordinated backup and pre-subscription rollback."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import historical_v01
import pytest
import test_historical_v01_recovery as recovery_cases
import test_historical_v01_upgrade as plan_case
from alembic import command
from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessSupervisor,
    ProcessIdentityStatus,
)
from forge.domain.operation import canonical_digest
from forge.evaluations.credentials import assert_credential_free
from legacy_upgrade_manifest import LEGACY_REVISION
from sqlalchemy import text
from sqlalchemy.engine import make_url


async def _tables(session_factory):
    async with session_factory() as session:
        names = await session.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {}
        for name in names:
            assert re.fullmatch(r"[a-z_]+", name)
            if name == "alembic_version":
                continue
            rows = (await session.scalars(text(f'SELECT to_jsonb(t) FROM "{name}" AS t'))).all()
            row_count = len(rows)
            assert row_count < 10000, name
            tables[name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return tables


def _assert_original_rows(before, after):
    for name, rows in before.items():
        actual_count, expected_count = len(after[name]), len(rows)
        assert actual_count == expected_count, name
        columns = set(rows[0]) if rows else set()
        projected = [{key: row[key] for key in columns} for row in after[name]]
        projected.sort(key=lambda row: json.dumps(row, sort_keys=True))
        # Compare digests so authentication/session rows never enter failure output.
        actual_digest, expected_digest = canonical_digest(projected), canonical_digest(rows)
        assert actual_digest == expected_digest, name


def _backup(case, database_url, backup_root):
    url = make_url(database_url)
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert re.fullmatch(r"forge_test_[0-9a-f]{32}", url.database or "")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Disposable backup rehearsal requires the PostgreSQL Docker service")
    candidates = subprocess.check_output(
        [docker, "ps", "--filter", f"publish={url.port}", "--format", "{{.ID}}"],
        text=True,
        timeout=15,
    ).splitlines()
    assert len(candidates) == 1 and re.fullmatch(r"[0-9a-f]+", candidates[0])
    container = candidates[0]
    dumped = subprocess.run(
        [docker, "exec", container, "pg_dump", "-U", url.username, "-Fc", url.database],
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert dumped.returncode == 0, "Disposable pg_dump failed"
    assert dumped.stdout.startswith(b"PGDMP") and len(dumped.stdout) < 16 * 1024 * 1024
    backup_root.mkdir()
    dump_path = backup_root / "control.dump"
    dump_path.write_bytes(dumped.stdout)
    listing = subprocess.run(
        [docker, "exec", "-i", container, "pg_restore", "--list"],
        input=dump_path.read_bytes(),
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert listing.returncode == 0 and b"TABLE DATA public runs " in listing.stdout
    entries = {}
    filesystem = backup_root / "filesystem.zip"
    with zipfile.ZipFile(filesystem, "x", zipfile.ZIP_DEFLATED) as archive:
        for label, root in (("data", case.settings.data_root), ("repository", case.fixture.path)):
            root = Path(root).resolve(strict=True)
            assert root.is_relative_to(backup_root.parent.resolve())
            for path in sorted(root.rglob("*")):
                assert not path.is_symlink()
                if path.is_file():
                    name = f"{label}/{path.relative_to(root).as_posix()}"
                    content = path.read_bytes()
                    entries[name] = hashlib.sha256(content).hexdigest()
                    archive.writestr(name, content)
    with zipfile.ZipFile(filesystem) as archive:
        assert archive.testzip() is None and set(archive.namelist()) == set(entries)
        assert all(
            hashlib.sha256(archive.read(name)).hexdigest() == h for name, h in entries.items()
        )
    return {
        "database_archive_sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
        "database_archive_bytes": dump_path.stat().st_size,
        "database_archive_catalog_verified": True,
        "filesystem_archive_sha256": hashlib.sha256(filesystem.read_bytes()).hexdigest(),
        "filesystem_files_verified": len(entries),
        "filesystem_entry_digests": entries,
        "backup_restored": False,
    }


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.parametrize("state", ["plan", "unfinished", "review-pause", "review-cancel"])
async def test_stopped_upgrade_backup_rollback_and_recovery(
    test_database_url, alembic_config_factory, tmp_path, monkeypatch, state
):
    processes = []
    checkpoints = []
    original_process = historical_v01.historical_process
    original_upgrade = historical_v01.upgrade_existing_legacy_case

    async def stopped_process(*args, **kwargs):
        result = await original_process(*args, **kwargs)
        proof = result["terminal"]
        assert proof["stop_confirmed"]
        receipt = ClientProcessReceipt(
            proof["launch_id"], proof["pid"], proof["process_identity"], 0
        )
        assert ClientProcessSupervisor.identity_status(receipt) is ProcessIdentityStatus.GONE
        processes.append(proof)
        return result

    async def stopped_upgrade(case, factory, database_url, config):
        assert len(processes) == 1, "The old writer must stop before backup or migration"
        frozen = await _tables(factory)
        backup = await asyncio.to_thread(_backup, case, database_url, tmp_path / "backup")
        after_backup = await _tables(factory)
        assert set(after_backup) == set(frozen)
        _assert_original_rows(frozen, after_backup)
        await original_upgrade(case, factory, database_url, config)
        upgraded = await _tables(factory)
        _assert_original_rows(frozen, upgraded)
        # No new-version effects have been admitted: exercise only that rollback boundary.
        assert all(not rows for name, rows in upgraded.items() if name not in frozen)
        await asyncio.to_thread(command.downgrade, config, LEGACY_REVISION)
        _assert_original_rows(frozen, await _tables(factory))
        await asyncio.to_thread(command.upgrade, config, "head")
        _assert_original_rows(frozen, await _tables(factory))
        async with factory() as session:
            head = await session.scalar(text("SELECT version_num FROM alembic_version"))
        checkpoints.append(
            {
                **backup,
                "state": state,
                "old_revision": historical_v01.REVISION,
                "old_migration": LEGACY_REVISION,
                "new_migration": head,
                "old_writer_stopped_before_backup": True,
                "pre_subscription_rollback": "passed",
                "original_table_digests": {
                    name: canonical_digest(rows) for name, rows in frozen.items()
                },
                "original_table_row_counts": {name: len(rows) for name, rows in frozen.items()},
            }
        )

    monkeypatch.setattr(historical_v01, "historical_process", stopped_process)
    monkeypatch.setattr(historical_v01, "upgrade_existing_legacy_case", stopped_upgrade)
    if state == "plan":
        await plan_case.test_historical_v01_plan_survives_upgrade_and_current_operator_controls(
            test_database_url, alembic_config_factory, tmp_path
        )
    elif state == "unfinished":
        await recovery_cases.test_historical_unfinished_write_recovers_once_after_upgrade(
            test_database_url, alembic_config_factory, tmp_path
        )
    else:
        await recovery_cases.test_historical_review_and_pending_control_survive_upgrade(
            test_database_url, alembic_config_factory, tmp_path, state.removeprefix("review-")
        )
    assert len(checkpoints) == 1 and len(processes) == 2
    checkpoint = {**checkpoints[0], "historical_processes_stopped": len(processes)}
    assert_credential_free(json.dumps(checkpoint))
    output = Path(os.environ.get("FORGE_ACCEPTANCE_OUTPUT_ROOT", tmp_path / "acceptance"))
    if "FORGE_ACCEPTANCE_OUTPUT_ROOT" in os.environ:
        scratch = Path(__file__).resolve().parents[4] / ".llm-output"
        assert (
            output.resolve().is_relative_to(scratch.resolve())
            and output.resolve() != scratch.resolve()
        )
    output.mkdir(parents=True, exist_ok=True)
    (output / f"stopped-upgrade-{state}.json").write_text(
        json.dumps({"digest": canonical_digest(checkpoint), "evidence": checkpoint}, indent=2)
        + "\n",
        encoding="utf-8",
    )
