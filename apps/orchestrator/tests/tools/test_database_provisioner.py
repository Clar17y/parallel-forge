"""Focused contracts for the isolated PostgreSQL provisioner."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from forge.domain.operation import (
    OperationExecutionClaim,
    OperationIntent,
    OperationOutcome,
    OperationStatus,
)
from forge.domain.policy import DatabaseProvisioningPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.database import (
    DatabaseBinding,
    DatabaseIntegrityError,
    DatabaseProvisioner,
    DatabaseProvisionerError,
    DatabaseReconciliationRequired,
)
from forge.tools.secrets import LocalSecretStore, SecretAlreadyExistsError


@dataclass
class _SpyResolver:
    value: str = "postgresql://forge:forge@127.0.0.1:5435/postgres"
    calls: list[str] = field(default_factory=list)

    async def resolve(self, reference: str) -> str:
        self.calls.append(reference)
        return self.value


@dataclass
class _SpyPasswordSource:
    value: bytes = bytes(range(32))
    calls: list[int] = field(default_factory=list)

    def token_bytes(self, size: int) -> bytes:
        self.calls.append(size)
        return self.value


@dataclass
class _FakeConnection:
    events: list[str]
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    databases: dict[str, str] = field(default_factory=dict)
    database_settings: set[str] = field(default_factory=set)
    database_row_omissions: set[str] = field(default_factory=set)
    statements: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    closed: bool = False
    close_calls: int = 0
    failure_marker: str | None = None
    fetch_started: asyncio.Event | None = None
    fetch_release: asyncio.Event | None = None
    close_started: asyncio.Event | None = None
    close_release: asyncio.Event | None = None
    close_failure: Exception | None = None
    cancel_task_on_close: asyncio.Task[Any] | None = None
    close_cancel_sent: bool = False

    async def execute(self, statement: str, *parameters: object) -> str:
        self.statements.append((statement, parameters))
        self.events.append("sql")
        if self.failure_marker is not None and self.failure_marker in statement:
            raise RuntimeError("driver leaked detail")
        if statement.startswith('CREATE ROLE "'):
            role_name = statement.split('"', 2)[1]
            self.roles[role_name] = {
                "rolname": role_name,
                "rolsuper": False,
                "rolinherit": True,
                "rolcreaterole": False,
                "rolcreatedb": False,
                "rolcanlogin": True,
                "rolreplication": False,
                "rolbypassrls": False,
                "rolconnlimit": -1,
                "rolvaliduntil": None,
                "rolconfig": None,
                "has_memberships": False,
                "has_settings": False,
            }
        elif statement.startswith('CREATE DATABASE "'):
            quoted = re.findall(r'"([a-z_][a-z0-9_]*)"', statement)
            self.databases[quoted[0]] = quoted[1]
        elif statement.startswith('DROP DATABASE "'):
            self.databases.pop(statement.split('"', 2)[1], None)
        elif statement.startswith('DROP ROLE "'):
            self.roles.pop(statement.split('"', 2)[1], None)
        return "OK"

    async def fetchrow(self, statement: str, *parameters: object) -> dict[str, Any] | None:
        self.statements.append((statement, parameters))
        self.events.append("sql")
        if self.fetch_started is not None:
            self.fetch_started.set()
        if self.fetch_release is not None:
            await self.fetch_release.wait()
        lower = statement.casefold()
        if "pg_database" in lower:
            name = str(parameters[0]) if parameters else ""
            owner = self.databases.get(name)
            if owner is None:
                return None
            row: dict[str, Any] = {
                "datname": name,
                "owner": owner,
                "has_database_settings": name in self.database_settings,
            }
            for key in self.database_row_omissions:
                row.pop(key, None)
            return row
        if "pg_roles" in lower:
            name = str(parameters[0]) if parameters else ""
            return self.roles.get(name)
        return None

    async def fetch(self, statement: str, *parameters: object) -> list[dict[str, Any]]:
        self.statements.append((statement, parameters))
        self.events.append("sql")
        return []

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.cancel_task_on_close is not None and not self.close_cancel_sent:
            self.close_cancel_sent = True
            self.cancel_task_on_close.cancel()
        if self.close_release is not None:
            await self.close_release.wait()
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True
        self.events.append("connection-close")


@dataclass
class _SpyConnectionFactory:
    connection: _FakeConnection
    calls: list[str] = field(default_factory=list)

    async def __call__(self, url: str) -> _FakeConnection:
        self.calls.append(url)
        self.connection.events.append("connection")
        return self.connection


class _ImmediateExecutor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[Any] = []
        self.outcomes: list[OperationOutcome] = []

    async def execute(self, request: Any, adapter: Any) -> OperationOutcome:
        self.requests.append(request)
        self.events.append("intent")
        intent = _intent_for_request(request)
        outcome = cast(OperationOutcome, await adapter.invoke(intent))
        self.outcomes.append(outcome)
        return outcome


class _MemorySecretStore:
    def __init__(
        self, values: dict[str, bytes] | None = None, *, race_on_create: bool = False
    ) -> None:
        self.values = dict(values or {})
        self.race_on_create = race_on_create
        self.create_calls: list[str] = []
        self.read_calls: list[str] = []
        self.exists_calls: list[str] = []
        self.delete_calls: list[str] = []

    def create(self, secret_id: str, secret: bytes) -> None:
        self.create_calls.append(secret_id)
        if self.race_on_create:
            raise SecretAlreadyExistsError("secret already exists")
        if secret_id in self.values:
            raise SecretAlreadyExistsError("secret already exists")
        self.values[secret_id] = secret

    def read(self, secret_id: str) -> bytes:
        self.read_calls.append(secret_id)
        return self.values[secret_id]

    def exists(self, secret_id: str) -> bool:
        self.exists_calls.append(secret_id)
        return secret_id in self.values

    def delete(self, secret_id: str) -> None:
        self.delete_calls.append(secret_id)
        self.values.pop(secret_id, None)


class _SucceededExecutor:
    def __init__(self, outcome: OperationOutcome) -> None:
        self.outcome = outcome
        self.requests: list[Any] = []

    async def execute(self, request: Any, adapter: Any) -> OperationOutcome:
        del adapter
        self.requests.append(request)
        return self.outcome


class _MemoryOperationRepository:
    """Small durable-boundary double used with the real OperationExecutor."""

    def __init__(self) -> None:
        self.by_key: dict[str, OperationIntent] = {}
        self.by_id: dict[UUID, OperationIntent] = {}

    async def begin(
        self,
        *,
        run_id: UUID,
        operation_type: str,
        idempotency_key: str,
        request_digest: str,
        request_payload: Any,
        request_schema_version: int = 1,
        execution_owner: str | None = None,
        execution_lease_seconds: float | None = None,
    ) -> OperationIntent:
        existing = self.by_key.get(idempotency_key)
        if existing is not None:
            return replace(existing, is_new=False)
        now = datetime.now(UTC)
        candidate = OperationIntent(
            id=uuid4(),
            run_id=run_id,
            kind=operation_type,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            request_payload=request_payload,
            status=OperationStatus.PENDING,
            attempt=1 if execution_owner is not None else 0,
            created_at=now,
            updated_at=now,
            execution_owner=execution_owner,
            execution_lease_expires_at=(
                now + timedelta(seconds=execution_lease_seconds)
                if execution_owner is not None and execution_lease_seconds is not None
                else None
            ),
            is_new=True,
        )
        self.by_key[idempotency_key] = candidate
        self.by_id[candidate.id] = candidate
        return candidate

    async def get(self, intent_id: UUID) -> OperationIntent:
        return self.by_id[intent_id]

    async def claim_for_recovery(
        self, intent_id: UUID, *, owner_id: str, lease_seconds: float
    ) -> OperationExecutionClaim:
        current = self.by_id[intent_id]
        claimed = replace(
            current,
            execution_owner=owner_id,
            execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
            is_new=False,
        )
        self.by_id[intent_id] = claimed
        self.by_key[claimed.idempotency_key] = claimed
        return OperationExecutionClaim(intent=claimed, acquired=True)

    async def renew_execution(
        self, intent_id: UUID, *, owner_id: str, lease_seconds: float
    ) -> OperationIntent:
        current = self.by_id[intent_id]
        renewed = replace(
            current,
            execution_owner=owner_id,
            execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
            is_new=False,
        )
        self.by_id[intent_id] = renewed
        self.by_key[renewed.idempotency_key] = renewed
        return renewed

    async def complete(
        self, intent_id: UUID, outcome: OperationOutcome, *, owner_id: str | None = None
    ) -> OperationIntent:
        current = self.by_id[intent_id]
        now = datetime.now(UTC)
        completed = replace(
            current,
            status=OperationStatus.SUCCEEDED,
            remote_resource_id=outcome.remote_resource_id,
            outcome=outcome.payload,
            outcome_schema_version=outcome.outcome_schema_version,
            completed_at=now,
            updated_at=now,
            execution_owner=None,
            execution_lease_expires_at=None,
            is_new=False,
        )
        self.by_id[intent_id] = completed
        self.by_key[completed.idempotency_key] = completed
        return completed

    async def fail(
        self,
        intent_id: UUID,
        *,
        error: str,
        needs_reconciliation: bool = False,
        owner_id: str | None = None,
    ) -> OperationIntent:
        current = self.by_id[intent_id]
        failed = replace(
            current,
            status=(
                OperationStatus.NEEDS_RECONCILIATION
                if needs_reconciliation
                else OperationStatus.FAILED
            ),
            error=error,
            completed_at=None if needs_reconciliation else datetime.now(UTC),
            updated_at=datetime.now(UTC),
            execution_owner=None,
            execution_lease_expires_at=None,
            is_new=False,
        )
        self.by_id[intent_id] = failed
        self.by_key[failed.idempotency_key] = failed
        return failed

    async def list_unresolved(self) -> tuple[OperationIntent, ...]:
        return tuple(
            intent
            for intent in self.by_id.values()
            if intent.status in {OperationStatus.PENDING, OperationStatus.NEEDS_RECONCILIATION}
        )


def _intent_for_request(request: Any) -> OperationIntent:
    payload = (
        request.request_payload
        if hasattr(request, "request_payload")
        else request["request_payload"]
    )
    run_id = request.run_id if hasattr(request, "run_id") else request["run_id"]
    kind = request.kind if hasattr(request, "kind") else request["operation_type"]
    idempotency_key = (
        request.idempotency_key
        if hasattr(request, "idempotency_key")
        else request["idempotency_key"]
    )
    digest = (
        request.request_digest if hasattr(request, "request_digest") else request["request_digest"]
    )
    now = datetime.now(UTC)
    return OperationIntent(
        id=uuid4(),
        run_id=run_id,
        kind=kind,
        idempotency_key=idempotency_key,
        request_digest=digest,
        request_payload=payload,
        status=OperationStatus.PENDING,
        attempt=1,
        created_at=now,
        updated_at=now,
        is_new=True,
    )


def _enabled_policy() -> DatabaseProvisioningPolicy:
    return DatabaseProvisioningPolicy(
        enabled=True,
        admin_url_secret_reference="secret://forge/postgres-admin",
        injected_environment_key="FORGE_DATABASE_URL",
    )


def _disabled_policy() -> DatabaseProvisioningPolicy:
    return DatabaseProvisioningPolicy(enabled=False)


def _identity(*, enabled: bool = True) -> WorktreeIdentity:
    return WorktreeIdentity.for_run(uuid4(), uuid4(), "feature/database", enabled)


def _safe_role(name: str, **overrides: object) -> dict[str, Any]:
    role: dict[str, Any] = {
        "rolname": name,
        "rolsuper": False,
        "rolinherit": True,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolcanlogin": True,
        "rolreplication": False,
        "rolbypassrls": False,
        "rolconnlimit": -1,
        "rolvaliduntil": None,
        "rolconfig": None,
        "has_memberships": False,
        "has_settings": False,
    }
    role.update(overrides)
    return role


def _active_resource(identity: WorktreeIdentity) -> DatabaseBinding:
    assert identity.database_name is not None
    assert identity.database_role is not None
    return DatabaseBinding(
        state=ResourceState.ACTIVE,
        database_name=identity.database_name,
        database_role=identity.database_role,
        secret_id=_secret_id_for(identity),
    )


def _secret_id_for(identity: WorktreeIdentity) -> str:
    assert identity.run_id is not None
    return f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}"


def _provisioner(
    tmp_path: Path,
    *,
    identity: WorktreeIdentity,
    policy: DatabaseProvisioningPolicy,
    events: list[str],
    resolver: _SpyResolver | None = None,
    password_source: _SpyPasswordSource | None = None,
    connection: _FakeConnection | None = None,
    executor: _ImmediateExecutor | None = None,
    secret_store: Any | None = None,
) -> tuple[
    DatabaseProvisioner, _SpyResolver, _SpyPasswordSource, _FakeConnection, _ImmediateExecutor
]:
    resolved = resolver or _SpyResolver()
    source = password_source or _SpyPasswordSource()
    database = connection or _FakeConnection(events)
    factory = _SpyConnectionFactory(database)
    operation_executor = executor or _ImmediateExecutor(events)
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = secret_store or LocalSecretStore(data_root)
    return (
        DatabaseProvisioner(
            operation_executor=operation_executor,
            admin_secret_resolver=resolved,
            secret_store=store,
            password_source=source,
            connection_factory=factory,
        ),
        resolved,
        source,
        database,
        operation_executor,
    )


@pytest.mark.asyncio
async def test_disabled_provision_and_teardown_make_zero_dependency_calls(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity(enabled=False)
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path, identity=identity, policy=_disabled_policy(), events=events
    )

    binding = await provisioner.provision(identity, _disabled_policy(), policy_version=7)
    removed = await provisioner.teardown(
        identity,
        _disabled_policy(),
        binding,
        policy_version=7,
    )

    assert binding.state is ResourceState.DISABLED
    assert binding.database_name is None
    assert binding.database_role is None
    assert binding.secret_id is None
    assert not binding.environment
    assert removed == binding
    assert not events
    assert not resolver.calls
    assert not source.calls
    assert not connection.statements
    assert not executor.requests


@pytest.mark.asyncio
async def test_enabled_provision_persists_only_safe_binding_and_builds_encoded_url(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    identity = _identity()
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path, identity=identity, policy=_enabled_policy(), events=events
    )

    binding = await provisioner.provision(identity, _enabled_policy(), policy_version=3)

    assert binding.state is ResourceState.ACTIVE
    assert binding.database_name == identity.database_name
    assert binding.database_role == identity.database_role
    assert binding.secret_id == _secret_id_for(identity)
    assert tuple(binding.environment) == ("FORGE_DATABASE_URL",)
    scoped_url = binding.environment["FORGE_DATABASE_URL"]
    assert isinstance(scoped_url, str)
    assert "%" in scoped_url
    assert "postgres" in scoped_url
    assert source.calls == [32]
    assert resolver.calls == ["secret://forge/postgres-admin"]
    assert events.index("intent") < events.index("connection")
    assert "password" not in repr(binding).casefold()
    assert source.value.decode(errors="ignore") not in repr(binding)
    assert scoped_url not in repr(binding)
    assert "tmp_path" not in repr(binding)
    assert executor.requests
    request_payload = executor.requests[0].request_payload
    assert "password" not in repr(request_payload).casefold()
    assert resolver.value not in repr(request_payload)
    assert connection.closed is True


@pytest.mark.asyncio
async def test_forged_enabled_identity_and_noncanonical_reference_fail_before_effects(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    identity = _identity()
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path, identity=identity, policy=_enabled_policy(), events=events
    )
    forged = WorktreeIdentity(
        project_id=identity.project_id,
        run_id=identity.run_id,
        branch=identity.branch,
        worktree_name=identity.worktree_name,
        database_name="forge_forged",
        database_role=identity.database_role,
    )

    with pytest.raises(DatabaseProvisionerError):
        await provisioner.provision(forged, _enabled_policy(), policy_version=1)
    assert not events
    assert not resolver.calls
    assert not source.calls
    assert not connection.statements
    assert not executor.requests


@pytest.mark.asyncio
async def test_disabled_resource_mismatch_fails_before_every_dependency(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity(enabled=False)
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path, identity=identity, policy=_disabled_policy(), events=events
    )

    with pytest.raises(DatabaseIntegrityError):
        await provisioner.teardown(
            identity,
            _disabled_policy(),
            DatabaseBinding(
                state=ResourceState.PROVISIONING,
                database_name="forge_bad",
            ),
            policy_version=1,
        )
    assert not events
    assert not resolver.calls
    assert not source.calls
    assert not connection.statements
    assert not executor.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "secret://",
        "secret://forge//admin",
        "secret://forge/./admin",
        "secret://forge/../admin",
        "secret://forge/admin/",
        "secret://forge/admin?x=1",
        "secret://forge/admin#fragment",
        "secret://user@forge/admin",
        "secret://forge:5432/admin",
        "secret://forge/admin%2Fname",
        "secret://forge/admin\\name",
    ],
)
async def test_noncanonical_secret_reference_is_rejected_without_effects(
    tmp_path: Path, reference: str
) -> None:
    events: list[str] = []
    identity = _identity()
    policy = DatabaseProvisioningPolicy.model_construct(
        enabled=True,
        admin_url_secret_reference=reference,
        injected_environment_key="DATABASE_URL",
    )
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path, identity=identity, policy=policy, events=events
    )

    with pytest.raises(DatabaseIntegrityError, match="database administrator reference is invalid"):
        await provisioner.provision(identity, policy, policy_version=1)
    assert not events
    assert not resolver.calls
    assert not source.calls
    assert not connection.statements
    assert not executor.requests


def test_structured_admin_url_normalization_and_scoped_round_trip() -> None:
    from forge.tools.database import _normalize_admin_url, _scoped_url
    from sqlalchemy.engine import make_url

    identity = _identity()
    normalized = _normalize_admin_url(
        "postgresql+asyncpg://admin:p%40ss@db.example:5435/other?sslmode=require&target_session_attrs=read-write#ignored"
    )
    assert normalized.drivername == "postgresql"
    assert normalized.database == "postgres"
    assert normalized.host == "db.example"
    assert normalized.port == 5435
    assert normalized.query == {
        "sslmode": "require",
        "target_session_attrs": "read-write",
    }
    scoped = _scoped_url(normalized, identity, "abc+/==")
    parsed = make_url(scoped)
    assert parsed.username == identity.database_role
    assert parsed.password == "abc+/=="
    assert parsed.database == identity.database_name
    assert parsed.query == normalized.query


@pytest.mark.parametrize(
    "query",
    [
        "password=ADMIN_SENTINEL",
        "host=attacker.example",
        "sslmode=require&sslmode=prefer",
        "SSLMode=require",
        "sslmode=REQUIRE",
        "target_session_attrs=READ-WRITE",
    ],
)
def test_admin_url_rejects_unknown_duplicate_and_invalid_query_options(query: str) -> None:
    from forge.tools.database import _normalize_admin_url

    with pytest.raises(DatabaseProvisionerError, match="database administrator URL is invalid"):
        _normalize_admin_url(f"postgresql://admin:password@db.example/postgres?{query}")


def test_admin_url_query_rejection_is_static_and_redacted() -> None:
    from forge.tools.database import _normalize_admin_url

    with pytest.raises(DatabaseProvisionerError) as error:
        _normalize_admin_url(
            "postgresql://admin:password@db.example/postgres?password=ADMIN_SENTINEL"
        )
    assert str(error.value) == "database administrator URL is invalid"
    assert "ADMIN_SENTINEL" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "postgres://admin:p@db.example/postgres",
        "postgresql://admin@db.example/postgres",
        "postgresql://:password@db.example/postgres",
        "postgresql://admin:@db.example/postgres",
        "postgresql://admin:password@/postgres",
        "postgresql://admin:password@db.example:bad/postgres",
    ],
)
def test_admin_url_rejections_are_stable(value: str) -> None:
    from forge.tools.database import _normalize_admin_url

    with pytest.raises(DatabaseProvisionerError, match="database administrator URL is invalid"):
        _normalize_admin_url(value)


@pytest.mark.asyncio
async def test_owner_mismatch_is_inspection_only_and_needs_reconciliation(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity()
    connection = _FakeConnection(events)
    assert identity.database_name is not None
    assert identity.database_role is not None
    connection.roles[identity.database_role] = _safe_role(identity.database_role)
    connection.databases[identity.database_name] = "forge_other_owner"
    provisioner, _resolver, source, connection, executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )
    secret_id = _secret_id_for(identity)
    provisioner._secret_store.create(secret_id, base64.urlsafe_b64encode(bytes(range(32))))

    with pytest.raises(DatabaseReconciliationRequired):
        await provisioner.provision(identity, _enabled_policy(), policy_version=1)
    assert not source.calls
    assert not executor.requests[0].request_payload.get("password", "")
    assert not any(
        statement.startswith(("CREATE ROLE", "CREATE DATABASE"))
        for statement, _ in connection.statements
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape, expected_success, expected_mutations",
    [
        ("absent", True, []),
        ("full", True, ["terminate", "drop-database", "drop-role"]),
        ("role-secret", True, ["drop-role"]),
        ("secret", True, []),
        ("database-only", False, []),
        ("role-only", False, []),
        ("database-role-no-secret", False, []),
        ("owner-mismatch", False, []),
        ("unsafe-role", False, []),
    ],
)
async def test_teardown_matrix_is_exact_and_fail_closed(
    tmp_path: Path,
    shape: str,
    expected_success: bool,
    expected_mutations: list[str],
) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    connection = _FakeConnection(events)
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )
    secret_id = _secret_id_for(identity)
    if shape in {
        "full",
        "role-secret",
        "role-only",
        "database-role-no-secret",
        "owner-mismatch",
        "unsafe-role",
    }:
        connection.roles[identity.database_role] = _safe_role(
            identity.database_role,
            **({"rolcreatedb": True} if shape == "unsafe-role" else {}),
        )
    if shape in {
        "full",
        "database-only",
        "database-role-no-secret",
        "owner-mismatch",
        "unsafe-role",
    }:
        connection.databases[identity.database_name] = (
            "forge_other_owner" if shape == "owner-mismatch" else identity.database_role
        )
    if shape in {"full", "role-secret", "secret", "owner-mismatch", "unsafe-role"}:
        provisioner._secret_store.create(secret_id, base64.urlsafe_b64encode(bytes(range(32))))

    if expected_success:
        result = await provisioner.teardown(
            identity,
            _enabled_policy(),
            _active_resource(identity),
            policy_version=1,
        )
        assert result.state is ResourceState.REMOVED
        assert not provisioner._secret_store.exists(secret_id)
    else:
        with pytest.raises(DatabaseReconciliationRequired):
            await provisioner.teardown(
                identity,
                _enabled_policy(),
                _active_resource(identity),
                policy_version=1,
            )
    mutations = []
    for statement, _parameters in connection.statements:
        if statement.lstrip().startswith("SELECT pg_catalog.pg_terminate_backend"):
            mutations.append("terminate")
        elif statement.startswith("DROP DATABASE"):
            mutations.append("drop-database")
        elif statement.startswith("DROP ROLE"):
            mutations.append("drop-role")
    assert mutations == expected_mutations
    assert connection.closed is True


@pytest.mark.asyncio
async def test_teardown_failure_after_database_drop_is_stable_and_truthful(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    connection = _FakeConnection(events, failure_marker="DROP ROLE")
    connection.roles[identity.database_role] = _safe_role(identity.database_role)
    connection.databases[identity.database_name] = identity.database_role
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )
    secret_id = _secret_id_for(identity)
    provisioner._secret_store.create(secret_id, base64.urlsafe_b64encode(bytes(range(32))))

    with pytest.raises(DatabaseProvisionerError) as error:
        await provisioner.teardown(
            identity,
            _enabled_policy(),
            _active_resource(identity),
            policy_version=1,
        )
    assert str(error.value) == "database operation failed"
    assert "driver leaked detail" not in repr(error.value)
    assert identity.database_name not in connection.databases
    assert identity.database_role in connection.roles
    assert provisioner._secret_store.exists(secret_id)
    assert connection.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "omitted",
    ["rolsuper", "rolvaliduntil", "rolconfig", "has_memberships", "has_settings"],
)
async def test_missing_required_role_field_blocks_teardown(tmp_path: Path, omitted: str) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    role = _safe_role(identity.database_role)
    role.pop(omitted)
    connection = _FakeConnection(events)
    connection.roles[identity.database_role] = role
    connection.databases[identity.database_name] = identity.database_role
    secret_id = _secret_id_for(identity)
    store = _MemorySecretStore({secret_id: base64.urlsafe_b64encode(bytes(range(32)))})
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
        secret_store=store,
    )

    with pytest.raises(DatabaseReconciliationRequired):
        await provisioner.teardown(
            identity,
            _enabled_policy(),
            _active_resource(identity),
            policy_version=1,
        )
    assert not any(
        statement.lstrip().startswith(("SELECT pg_catalog.pg_terminate_backend", "DROP"))
        for statement, _ in connection.statements
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("database_row_omissions", [set(), {"has_database_settings"}])
async def test_database_settings_are_required_and_must_be_absent(
    tmp_path: Path, database_row_omissions: set[str]
) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    connection = _FakeConnection(
        events,
        database_row_omissions=database_row_omissions,
    )
    connection.roles[identity.database_role] = _safe_role(identity.database_role)
    connection.databases[identity.database_name] = identity.database_role
    if not database_row_omissions:
        connection.database_settings.add(identity.database_name)
    secret_id = _secret_id_for(identity)
    store = _MemorySecretStore({secret_id: base64.urlsafe_b64encode(bytes(range(32)))})
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
        secret_store=store,
    )

    with pytest.raises(DatabaseReconciliationRequired):
        await provisioner.teardown(
            identity,
            _enabled_policy(),
            _active_resource(identity),
            policy_version=1,
        )
    assert not any(
        statement.lstrip().startswith(("SELECT pg_catalog.pg_terminate_backend", "DROP"))
        for statement, _ in connection.statements
    )


@pytest.mark.asyncio
async def test_secret_create_race_never_creates_postgres_resources(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity()
    store = _MemorySecretStore(race_on_create=True)
    provisioner, _resolver, source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        secret_store=store,
    )

    with pytest.raises(DatabaseReconciliationRequired):
        await provisioner.provision(identity, _enabled_policy(), policy_version=1)
    assert source.calls == [32]
    assert len(store.create_calls) == 1
    assert not any(
        statement.startswith(("CREATE ROLE", "CREATE DATABASE"))
        for statement, _ in connection.statements
    )
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_cancellation_closes_connected_postgres_handle_once(tmp_path: Path) -> None:
    events: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    connection = _FakeConnection(events, fetch_started=started, fetch_release=release)
    identity = _identity()
    provisioner, resolver, source, connection, executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )

    task = asyncio.create_task(provisioner.provision(identity, _enabled_policy(), policy_version=1))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.close_calls == 1
    assert resolver.calls == ["secret://forge/postgres-admin"]
    assert not source.calls
    assert executor.requests


@pytest.mark.asyncio
async def test_repeated_cancellation_is_deferred_until_close_reaches_terminal_state(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    connection = _FakeConnection(
        events,
        fetch_started=fetch_started,
        fetch_release=fetch_release,
        close_started=close_started,
        close_release=close_release,
    )
    identity = _identity()
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )

    task = asyncio.create_task(provisioner.provision(identity, _enabled_policy(), policy_version=1))
    await asyncio.wait_for(fetch_started.wait(), timeout=1)
    task.cancel()
    await asyncio.wait_for(close_started.wait(), timeout=5)
    task.cancel()
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.close_calls == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_cancellation_arriving_during_close_is_deferred_until_close_completes(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    connection = _FakeConnection(
        events,
        close_started=close_started,
        close_release=close_release,
    )
    identity = _identity()
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )

    task = asyncio.create_task(provisioner.provision(identity, _enabled_policy(), policy_version=1))
    connection.cancel_task_on_close = task
    await asyncio.wait_for(close_started.wait(), timeout=5)
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.close_calls == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_close_failure_after_success_is_stable_and_redacted(tmp_path: Path) -> None:
    events: list[str] = []
    connection = _FakeConnection(
        events,
        close_failure=RuntimeError("CLOSE_SENTINEL"),
    )
    identity = _identity()
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )

    with pytest.raises(DatabaseProvisionerError) as error:
        await provisioner.provision(identity, _enabled_policy(), policy_version=1)
    assert str(error.value) == "database operation failed"
    assert "CLOSE_SENTINEL" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_binding_environment_repr_is_redacted_but_values_remain_immutable(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    identity = _identity()
    provisioner, _resolver, source, _connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
    )

    binding = await provisioner.provision(identity, _enabled_policy(), policy_version=1)
    scoped_url = binding.environment["FORGE_DATABASE_URL"]
    assert scoped_url not in repr(binding.environment)
    assert source.value.decode(errors="ignore") not in repr(binding.environment)
    with pytest.raises(TypeError):
        binding.environment["FORGE_DATABASE_URL"] = "replacement"  # type: ignore[index]


@pytest.mark.asyncio
async def test_succeeded_intent_reuses_only_resolved_admin_and_local_secret(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    secret_id = _secret_id_for(identity)
    password = base64.urlsafe_b64encode(bytes(range(32)))
    store = _MemorySecretStore({secret_id: password})
    outcome = OperationOutcome(
        status=OperationStatus.SUCCEEDED,
        payload={
            "state": ResourceState.ACTIVE.value,
            "database_name": identity.database_name,
            "database_role": identity.database_role,
            "secret_id": secret_id,
        },
    )
    executor = _SucceededExecutor(outcome)
    provisioner, resolver, source, connection, _unused = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        executor=executor,  # type: ignore[arg-type]
        secret_store=store,
    )

    binding = await provisioner.provision(identity, _enabled_policy(), policy_version=1)

    assert binding.state is ResourceState.ACTIVE
    assert resolver.calls == ["secret://forge/postgres-admin"]
    assert store.read_calls == [secret_id]
    assert not store.create_calls
    assert not source.calls
    assert not connection.statements
    assert not connection.closed
    assert not events
    request_payload = executor.requests[0].request_payload
    assert "postgresql://" not in repr(request_payload)
    assert "password" not in repr(request_payload).casefold()


@pytest.mark.asyncio
async def test_failures_are_redacted_without_driver_details_or_secret_values(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    identity = _identity()
    connection = _FakeConnection(events, failure_marker="CREATE ROLE")
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
    )

    with pytest.raises(DatabaseProvisionerError) as error:
        await provisioner.provision(identity, _enabled_policy(), policy_version=1)
    assert str(error.value) == "database operation failed"
    assert "driver leaked detail" not in str(error.value)
    assert "forge:forge" not in repr(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert connection.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"rolsuper": True},
        {"rolinherit": False},
        {"rolcreaterole": True},
        {"rolcreatedb": True},
        {"rolcanlogin": False},
        {"rolreplication": True},
        {"rolbypassrls": True},
        {"rolconnlimit": 1},
        {"rolvaliduntil": datetime.now(UTC)},
        {"rolconfig": ["search_path=private"]},
        {"has_memberships": True},
        {"has_settings": True},
    ],
)
async def test_every_unsafe_role_attribute_blocks_teardown(
    tmp_path: Path, override: dict[str, object]
) -> None:
    events: list[str] = []
    identity = _identity()
    assert identity.database_role is not None
    connection = _FakeConnection(events)
    connection.roles[identity.database_role] = _safe_role(identity.database_role, **override)
    store = _MemorySecretStore()
    provisioner, _resolver, _source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
        connection=connection,
        secret_store=store,
    )
    secret_id = _secret_id_for(identity)
    store.values[secret_id] = base64.urlsafe_b64encode(bytes(range(32)))

    with pytest.raises(DatabaseReconciliationRequired):
        await provisioner.teardown(
            identity,
            _enabled_policy(),
            _active_resource(identity),
            policy_version=1,
        )
    assert not any(
        statement.lstrip().startswith(("SELECT pg_catalog.pg_terminate_backend", "DROP"))
        for statement, _ in connection.statements
    )


@pytest.mark.asyncio
async def test_sql_capture_uses_safe_identifiers_and_parameterized_values(tmp_path: Path) -> None:
    events: list[str] = []
    identity = _identity()
    provisioner, resolver, source, connection, _executor = _provisioner(
        tmp_path,
        identity=identity,
        policy=_enabled_policy(),
        events=events,
    )
    await provisioner.provision(identity, _enabled_policy(), policy_version=1)

    generated = base64.urlsafe_b64encode(source.value).decode("ascii")
    statements = [statement for statement, _ in connection.statements]
    assert any(generated in statement for statement in statements)
    assert all("CASCADE" not in statement.upper() for statement in statements)
    assert all("DROP OWNED" not in statement.upper() for statement in statements)
    assert all(resolver.value not in statement for statement in statements)
    role_inspections = [
        (statement, parameters)
        for statement, parameters in connection.statements
        if "FROM pg_catalog.pg_roles r" in statement
    ]
    database_inspections = [
        (statement, parameters)
        for statement, parameters in connection.statements
        if "pg_database" in statement
    ]
    assert role_inspections and all(
        parameters == (identity.database_role,) for _, parameters in role_inspections
    )
    assert database_inspections and all(
        parameters == (identity.database_name,) for _, parameters in database_inspections
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_scoped_connectivity_owner_and_session_termination(
    tmp_path: Path,
) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    admin_url = "postgresql://forge:forge@127.0.0.1:5435/postgres"
    try:
        probe = await asyncpg.connect(admin_url)
    except Exception:  # noqa: BLE001 - integration availability is intentionally guarded
        pytest.skip("local PostgreSQL is unavailable")
    else:
        await probe.close()

    identity = _identity()
    assert identity.database_name is not None
    assert identity.database_role is not None
    assert re.fullmatch(r"[a-z_][a-z0-9_]*", identity.database_name)
    assert re.fullmatch(r"[a-z_][a-z0-9_]*", identity.database_role)
    resolver = _SpyResolver(value=admin_url)
    data_root = tmp_path / "data"
    data_root.mkdir()
    store = LocalSecretStore(data_root)
    repository = _MemoryOperationRepository()

    async def connect(url: str) -> Any:
        return await asyncpg.connect(url)

    from forge.application.services.recovery import OperationExecutor

    provisioner = DatabaseProvisioner(
        operation_executor=OperationExecutor(repository),
        admin_secret_resolver=resolver,
        secret_store=store,
        password_source=secrets,
        connection_factory=connect,
    )
    scoped_connection: Any | None = None
    try:
        binding = await provisioner.provision(identity, _enabled_policy(), policy_version=1)
        scoped_url = binding.environment["FORGE_DATABASE_URL"]
        scoped_connection = await asyncpg.connect(scoped_url)
        assert await scoped_connection.fetchval("SELECT current_user") == identity.database_role
        assert (
            await scoped_connection.fetchval("SELECT current_database()") == identity.database_name
        )

        removed = await provisioner.teardown(
            identity,
            _enabled_policy(),
            binding,
            policy_version=1,
        )
        assert removed.state is ResourceState.REMOVED
        with pytest.raises(Exception):  # noqa: B017 - driver termination varies by asyncpg version
            await scoped_connection.fetchval("SELECT 1")
        await scoped_connection.close()
        scoped_connection = None

        admin = await asyncpg.connect(admin_url)
        try:
            assert (
                await admin.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = $1",
                    identity.database_name,
                )
                is None
            )
            assert (
                await admin.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1",
                    identity.database_role,
                )
                is None
            )
        finally:
            await admin.close()
        assert not store.exists(binding.secret_id or "")
    finally:
        if scoped_connection is not None:
            await scoped_connection.close()
        cleanup = await asyncpg.connect(admin_url)
        try:
            await cleanup.execute(
                "SELECT pg_catalog.pg_terminate_backend(pid) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_catalog.pg_backend_pid()",
                identity.database_name,
            )
            if (
                await cleanup.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_database WHERE datname = $1",
                    identity.database_name,
                )
                is not None
            ):
                await cleanup.execute(f'DROP DATABASE "{identity.database_name}"')
            if (
                await cleanup.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1",
                    identity.database_role,
                )
                is not None
            ):
                await cleanup.execute(f'DROP ROLE "{identity.database_role}"')
        finally:
            await cleanup.close()
