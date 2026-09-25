"""Run the pinned v0.1 package in its own interpreter against a disposable DB."""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import UUID, uuid4

from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessSupervisor,
    terminal_launch_proof,
)
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.services.auth import AuthenticatedSession
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.policy import CommandSpec
from forge.domain.tool import ToolAuthorizationContext, ToolRequest, ToolResult
from forge.evaluations.credentials import assert_credential_free
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_counter_service_fixture,
    get_acceptance_command_specs,
)
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from legacy_upgrade_case import LegacyPlanGateway
from legacy_upgrade_manifest import blobs_for, snapshot_rows, upgrade_existing_legacy_case
from pydantic import TypeAdapter
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[4]
REVISION = "781567f7365e76dfc37cf8e7cf98765cb00800e4"
ARCHIVE_PATHS = (
    "apps/orchestrator/src/forge",
    "apps/orchestrator/migrations",
    "agents",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
)


def _assert_matching_environment(historical_root, current_root):
    metadata = []
    locks = []
    for root in (historical_root, current_root):
        document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
        project = document["project"]
        assert project["name"] == "parallel-forge"
        local = [package for package in lock["package"] if package["name"] == project["name"]]
        assert len(local) == 1 and local[0]["source"] == {"editable": "."}
        # Historical source is imported through its own PYTHONPATH. Only its
        # first-party release label may differ, bound to that project's lock entry.
        assert local[0].pop("version") == project.pop("version")
        # Test marker registration does not alter the interpreter environment.
        document.get("tool", {}).get("pytest", {}).get("ini_options", {}).pop("markers", None)
        metadata.append(document)
        locks.append(lock)
    assert locks[0] == locks[1], "Historical execution needs its exact locked dependencies"
    assert metadata[0] == metadata[1], "Historical execution needs matching environment metadata"


def historical_source():
    # Local Git objects only. CI must fetch the retained history; tests never
    # fetch a moving branch or download source from an unpinned external URL.
    archive = subprocess.check_output(
        ["git", "archive", "--format=zip", REVISION, *ARCHIVE_PATHS], cwd=ROOT
    )
    assert len(archive) < 16 * 1024 * 1024
    target = ROOT / ".llm-output/historical-v01-source" / str(uuid4())
    target.mkdir(parents=True)
    files = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert sum(item.file_size for item in bundle.infolist()) < 32 * 1024 * 1024
        for item in bundle.infolist():
            name = PurePosixPath(item.filename)
            assert not name.is_absolute() and ".." not in name.parts and "\\" not in item.filename
            assert not stat.S_ISLNK(item.external_attr >> 16)
            path = target.joinpath(*name.parts)
            assert path.resolve().is_relative_to(target.resolve())
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            value = bundle.read(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(value)
            files[item.filename] = hashlib.sha256(value).hexdigest()
    _assert_matching_environment(target, ROOT)
    return SimpleNamespace(
        root=target,
        files=files,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
    )


async def historical_process(source, database_url, payload, *, auth_state=None):
    assert re.fullmatch(r"forge_test_[0-9a-f]{32}", make_url(database_url).database or "")
    environment = {
        key: os.environ[key] for key in ("PATH", "TEMP", "TMP", "PATHEXT") if key in os.environ
    }
    environment.update(
        PYTHONPATH=str(source.root / "apps/orchestrator/src"),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        FORGE_HISTORICAL_DATABASE_SECRET_URL=database_url,
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, str(Path(__file__).with_name("historical_v01_peer.py"))),
        cwd=source.root,
        environment=environment,
        allowed_environment=frozenset(environment),
        duration_seconds=45,
    )
    session = await ClientProcessSupervisor().start(spec)
    response = None
    operator = None
    try:
        await session.send({**payload, "source_root": str(source.root)})
        response = await session.receive()
        if response is not None:
            # Test authentication crosses only this private in-memory pipe.
            # Remove it before assertions or retaining any process provenance.
            operator = response.pop("operator_session", None)
    finally:
        stopped = await session.close(completed=response is not None)
    assert stopped.stop_confirmed
    assert response is not None and response.get("completed") is True, stopped.stderr
    if operator is not None:
        assert auth_state is not None
        try:
            auth_state["operator_session"] = TypeAdapter(AuthenticatedSession).validate_python(
                operator
            )
        except TypeError, ValueError:
            raise AssertionError("invalid historical test-session handoff") from None
    loaded = response["loaded_forge_files"]
    assert loaded and all(source.files.get(name) == digest for name, digest in loaded.items())
    assert all(
        hashlib.sha256((source.root / name).read_bytes()).hexdigest() == digest
        for name, digest in loaded.items()
    )
    # Only safe data and process proof leave the helper. The disposable database
    # credential stays in the child's explicit environment, never in a record.
    response["terminal"] = terminal_launch_proof(stopped).model_dump(mode="json")
    response["environment_key_names"] = sorted(spec.environment)
    assert_credential_free(json.dumps(response))
    return response


async def _historical_case(
    database_url, alembic_config_factory, tmp_path, *, scenario, control=None
):
    source = historical_source()
    fixture = build_counter_service_fixture(tmp_path / "repo")
    _run_git_command(
        ["git", "remote", "add", "origin", "https://github.com/example/counter-service.git"],
        fixture.path,
    )
    data = tmp_path / "data"
    data.mkdir()
    auth_state = {}
    seeded = await historical_process(
        source,
        database_url,
        {
            "action": "seed-plan" if scenario == "plan" else "seed-state",
            "scenario": scenario,
            "control": control,
            "repository": str(fixture.path),
            "data_root": str(data),
            "task": fixture.case_contract.task,
            "commands": TypeAdapter(tuple[CommandSpec, ...]).dump_python(
                get_acceptance_command_specs(), mode="json"
            ),
        },
        auth_state=auth_state,
    )
    assert seeded["schema_revision"] == "20260909_0006"
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    settings = Settings(
        data_root=data, prompt_root=source.root / "agents", provider_secret_reference=""
    )
    store = FilesystemArtifactStore(settings.artifact_root)
    factory = lambda: PostgresUnitOfWork(session_factory)
    case = SimpleNamespace(
        engine=engine,
        session_factory=session_factory,
        settings=settings,
        factory=factory,
        store=store,
        fixture=fixture,
    )
    handlers = None
    try:
        await upgrade_existing_legacy_case(
            case, session_factory, database_url, alembic_config_factory(database_url)
        )
        before_reader = await snapshot_rows(session_factory)
        inspected = await historical_process(
            source,
            database_url,
            {
                "action": "read-plan" if scenario == "plan" else "read-state",
                "scenario": scenario,
                "data_root": str(data),
                "run_id": seeded["run_id"],
            },
        )
        assert inspected["run"] == seeded["run"] and inspected["provider_requests"] == []
        assert await snapshot_rows(session_factory) == before_reader
        assert await blobs_for(case, before_reader) == case.legacy_blobs
        validator = PlanEvidenceValidator(store, LocalGitRepositoryInspector(), data_root=str(data))
        async with factory() as work:
            case.run = await work.runs.get(UUID(seeded["run_id"]))
            if scenario == "plan":
                case.evidence = await validator.validate(work, case.run.id)
        requests = [
            SimpleNamespace(
                **{
                    **row,
                    "execution_id": UUID(row["execution_id"]),
                    "run_id": UUID(row["run_id"]),
                    "role": AgentRole(row["role"]),
                }
            )
            for row in seeded["provider_requests"]
        ]
        handlers = compose_worker_handlers(
            settings, session_factory, agent_gateway=LegacyPlanGateway()
        )
        case.handlers, case.validator = handlers, validator
        case.gateway = SimpleNamespace(requests=requests)
        if scenario == "unfinished":
            delivery = seeded["delivery"]
            case.tool_context = TypeAdapter(ToolAuthorizationContext).validate_python(
                delivery["tool_context"]
            )
            case.tool_request = TypeAdapter(ToolRequest).validate_python(delivery["tool_request"])
            case.tool_receipt = TypeAdapter(ToolResult).validate_python(delivery["tool_receipt"])
            case.partial = delivery["partial_text"].encode()
            case.original_writer = SimpleNamespace(calls=delivery["write_count"])
            case.tool_artifacts = {
                digest: await store.open_bytes(digest)
                for digest in case.tool_receipt.artifact_digests
            }
            async with factory() as work:
                case.admission = await work.executions.get_admission(
                    case.run.id, case.tool_context.agent_execution_id
                )
                case.call = await work.tool_calls.get(case.tool_context.invocation_id)
                case.operation = await work.operations.get(case.call.operation_intent_id)
        elif scenario == "review":
            case.pending_control = seeded["pending_control"]
            case.operator_session = auth_state["operator_session"]
        case.source_provenance = {
            "revision": REVISION,
            "scenario": scenario,
            "archive_paths": list(ARCHIVE_PATHS),
            "archive_sha256": source.archive_sha256,
            "lock_sha256": source.files["uv.lock"],
            "project_metadata_sha256": source.files["pyproject.toml"],
            "processes": [seeded, inspected],
            "old_reader_after_head_upgrade": True,
            "old_reader_preserved_rows_and_artifacts": True,
            "limits": [
                (
                    "Historical v0.1 Python source; shared environment with identical locked "
                    "dependencies and metadata apart from the bound first-party release "
                    "version and test markers"
                ),
                "Current test harness and explicitly scripted provider responses",
                "No historical deployment, rolling writer compatibility or rollback claim",
                "Current authenticated API components after upgrade; no browser or live provider call",
            ],
        }
        return case
    except BaseException:
        if handlers is not None:
            await handlers.aclose()
        await engine.dispose()
        raise


async def historical_plan_case(database_url, alembic_config_factory, tmp_path):
    return await _historical_case(database_url, alembic_config_factory, tmp_path, scenario="plan")


async def historical_execution_case(database_url, alembic_config_factory, tmp_path):
    return await _historical_case(
        database_url, alembic_config_factory, tmp_path, scenario="unfinished"
    )


async def historical_review_case(database_url, alembic_config_factory, tmp_path, *, control):
    return await _historical_case(
        database_url, alembic_config_factory, tmp_path, scenario="review", control=control
    )
