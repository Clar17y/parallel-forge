"""Trusted, isolated PostgreSQL provisioning for one persisted Forge run."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.engine import URL, make_url

from forge.application.ports.operations import OperationAdapter, OperationRepository
from forge.application.ports.worktrees import (
    AdminSecretResolverPort,
    DatabaseBinding,
    DatabaseProvisionerPort,
    SecretStorePort,
)
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import DatabaseProvisioningPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.runner import await_deferred_cancellation
from forge.tools.secrets import SecretAlreadyExistsError

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SECRET_REFERENCE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_PASSWORD_BYTES = 32
_PROTOCOL_VERSION = 1
_PROVISION_KIND = "database.provision"
_TEARDOWN_KIND = "database.teardown"

_ERROR = "database operation failed"
_INTEGRITY_ERROR = "database resource identity is invalid"
_REFERENCE_ERROR = "database administrator reference is invalid"
_URL_ERROR = "database administrator URL is invalid"
_RECONCILIATION_ERROR = "database resource requires reconciliation"
_ROW_MISSING = object()
_SAFE_QUERY_OPTIONS = {
    "sslmode": frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}),
    "target_session_attrs": frozenset(
        {"any", "primary", "standby", "read-write", "read-only", "prefer-standby"}
    ),
}


def _sanitize_error(error: DatabaseProvisionerError) -> DatabaseProvisionerError:
    """Remove any exception chain before a stable public error is re-raised."""

    error.__cause__ = None
    error.__context__ = None
    error.__traceback__ = None
    return error


_ROLE_QUERY = """
SELECT r.rolname,
       r.rolsuper,
       r.rolinherit,
       r.rolcreaterole,
       r.rolcreatedb,
       r.rolcanlogin,
       r.rolreplication,
       r.rolbypassrls,
       r.rolconnlimit,
       r.rolvaliduntil,
       r.rolconfig,
       EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members m
           WHERE m.roleid = r.oid OR m.member = r.oid
       ) AS has_memberships,
       EXISTS (
           SELECT 1 FROM pg_catalog.pg_db_role_setting s
           WHERE s.setrole = r.oid
       ) AS has_settings
FROM pg_catalog.pg_roles r
WHERE r.rolname = $1
"""
_DATABASE_QUERY = """
SELECT d.datname,
       owner_role.rolname AS owner,
       EXISTS (
           SELECT 1 FROM pg_catalog.pg_db_role_setting s
           WHERE s.setdatabase = d.oid
       ) AS has_database_settings
FROM pg_catalog.pg_database d
LEFT JOIN pg_catalog.pg_roles owner_role ON owner_role.oid = d.datdba
WHERE d.datname = $1
"""
_TERMINATE_QUERY = """
SELECT pg_catalog.pg_terminate_backend(pid)
FROM pg_catalog.pg_stat_activity
WHERE datname = $1 AND pid <> pg_catalog.pg_backend_pid()
"""


class _AsyncPostgresConnection(Protocol):
    async def execute(self, statement: str, *parameters: object) -> object: ...

    async def fetchrow(
        self, statement: str, *parameters: object
    ) -> Mapping[str, object] | None: ...

    async def close(self) -> object: ...


_ConnectionFactory = Callable[[str], Awaitable[_AsyncPostgresConnection]]


class _TokenBytesSource(Protocol):
    def token_bytes(self, size: int) -> bytes: ...


class _OperationExecutor(Protocol):
    async def execute(
        self, request: OperationRequest, adapter: OperationAdapter
    ) -> OperationOutcome: ...


class DatabaseProvisionerError(RuntimeError):
    """A database operation failed with a stable, redacted category."""


class DatabaseIntegrityError(DatabaseProvisionerError):
    """The caller-supplied identity or persisted resource is not exact."""


class DatabaseReconciliationRequired(DatabaseProvisionerError):
    """Observed database state is incomplete, mismatched, or ambiguous."""


@dataclass(frozen=True, slots=True)
class _ObservedState:
    role: Mapping[str, object] | None
    database: Mapping[str, object] | None
    secret_exists: bool

    @property
    def database_exists(self) -> bool:
        return self.database is not None

    @property
    def database_owner(self) -> str | None:
        if self.database is None:
            return None
        owner = _row_value(self.database, "owner")
        return owner if isinstance(owner, str) else None

    @property
    def database_safe(self) -> bool:
        return self.database is not None and (
            _as_bool(self.database, "has_database_settings") is False
        )

    @property
    def role_safe(self) -> bool:
        role = self.role
        if role is None:
            return False
        return (
            _as_bool(role, "rolsuper") is False
            and _as_bool(role, "rolinherit") is True
            and _as_bool(role, "rolcreaterole") is False
            and _as_bool(role, "rolcreatedb") is False
            and _as_bool(role, "rolcanlogin") is True
            and _as_bool(role, "rolreplication") is False
            and _as_bool(role, "rolbypassrls") is False
            and _as_int(role, "rolconnlimit") == -1
            and _as_optional_datetime(role, "rolvaliduntil") is None
            and _as_setting_empty(role, "rolconfig")
            and _as_bool(role, "has_memberships") is False
            and _as_bool(role, "has_settings") is False
        )

    def complete_for(self, database_name: str, role_name: str) -> bool:
        database = self.database
        return (
            self.role is not None
            and self.role_safe
            and _row_value(self.role, "rolname") == role_name
            and database is not None
            and _row_value(database, "datname") == database_name
            and self.database_owner == role_name
            and self.database_safe
            and self.secret_exists
        )

    @property
    def absent(self) -> bool:
        return self.role is None and not self.database_exists and not self.secret_exists


class _ProvisionAdapter:
    def __init__(
        self,
        owner: DatabaseProvisioner,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        secret_id: str,
    ) -> None:
        self._owner = owner
        self._identity = identity
        self._policy = policy
        self._secret_id = secret_id
        self.normalized_admin_url: URL | None = None

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        return await self._run(mutate=True)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        return await self._run(mutate=False)

    async def _run(self, *, mutate: bool) -> OperationOutcome:
        url = await self._resolve_admin_url()
        connection: _AsyncPostgresConnection | None = None
        failure: DatabaseProvisionerError | None = None
        caller_cancelled = False
        try:
            database_name = _require_name(self._identity.database_name)
            role_name = _require_name(self._identity.database_role)
            connection = await self._owner._connect(url)
            observed = await self._owner._inspect(
                connection,
                database_name=database_name,
                role_name=role_name,
                secret_id=self._secret_id,
            )
            if observed.complete_for(database_name, role_name):
                return self._owner._outcome(
                    state=ResourceState.ACTIVE,
                    identity=self._identity,
                    secret_id=self._secret_id,
                )
            if not mutate or not observed.absent:
                return self._owner._needs_reconciliation()

            raw_password = self._owner._generate_password()
            try:
                self._owner._secret_store.create(self._secret_id, raw_password)
            except SecretAlreadyExistsError:
                return self._owner._needs_reconciliation()
            await connection.execute(_create_role_sql(role_name, raw_password))
            await connection.execute(
                "CREATE DATABASE "
                f"{_quote_identifier(database_name)} "
                "OWNER "
                f"{_quote_identifier(role_name)}"
            )
            final = await self._owner._inspect(
                connection,
                database_name=database_name,
                role_name=role_name,
                secret_id=self._secret_id,
            )
            if not final.complete_for(database_name, role_name):
                return self._owner._needs_reconciliation()
            return self._owner._outcome(
                state=ResourceState.ACTIVE,
                identity=self._identity,
                secret_id=self._secret_id,
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - external driver failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        finally:
            caller_cancelled = await self._owner._close(
                connection, already_cancelled=caller_cancelled
            )
            if caller_cancelled:
                raise asyncio.CancelledError()
        if failure is not None:
            raise failure
        raise AssertionError("database provision adapter returned no outcome")

    async def _resolve_admin_url(self) -> URL:
        if self.normalized_admin_url is None:
            failure: DatabaseProvisionerError | None = None
            try:
                reference = self._policy.admin_url_secret_reference
                assert reference is not None
                resolved = await self._owner._resolver.resolve(reference)
                self.normalized_admin_url = _normalize_admin_url(resolved)
            except asyncio.CancelledError:
                raise
            except DatabaseProvisionerError as error:
                failure = _sanitize_error(error)
            except Exception:  # noqa: BLE001 - resolver failures are redacted
                failure = DatabaseProvisionerError(_ERROR)
            if failure is not None:
                raise failure
        if self.normalized_admin_url is None:
            raise AssertionError("administrator URL was not normalized")
        return self.normalized_admin_url


class _TeardownAdapter:
    def __init__(
        self,
        owner: DatabaseProvisioner,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        secret_id: str,
    ) -> None:
        self._owner = owner
        self._identity = identity
        self._policy = policy
        self._secret_id = secret_id
        self.normalized_admin_url: URL | None = None

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        return await self._run(mutate=True)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        del intent
        return await self._run(mutate=False)

    async def _run(self, *, mutate: bool) -> OperationOutcome:
        url = await self._resolve_admin_url()
        connection: _AsyncPostgresConnection | None = None
        failure: DatabaseProvisionerError | None = None
        caller_cancelled = False
        try:
            connection = await self._owner._connect(url)
            database_name = _require_name(self._identity.database_name)
            role_name = _require_name(self._identity.database_role)
            observed = await self._owner._inspect(
                connection,
                database_name=database_name,
                role_name=role_name,
                secret_id=self._secret_id,
            )
            if not mutate:
                return (
                    self._owner._outcome(
                        state=ResourceState.REMOVED,
                        identity=self._identity,
                        secret_id=None,
                    )
                    if observed.absent
                    else self._owner._needs_reconciliation()
                )
            if observed.absent:
                return self._owner._outcome(
                    state=ResourceState.REMOVED,
                    identity=self._identity,
                    secret_id=None,
                )
            if not self._owner._teardown_state_is_safe(observed, database_name, role_name):
                return self._owner._needs_reconciliation()

            if observed.database_exists:
                await connection.execute(_TERMINATE_QUERY, database_name)
                await connection.execute(f"DROP DATABASE {_quote_identifier(database_name)}")
            if observed.role is not None:
                await connection.execute(f"DROP ROLE {_quote_identifier(role_name)}")
            if observed.secret_exists:
                self._owner._secret_store.delete(self._secret_id)

            final = await self._owner._inspect(
                connection,
                database_name=database_name,
                role_name=role_name,
                secret_id=self._secret_id,
            )
            if not final.absent:
                return self._owner._needs_reconciliation()
            return self._owner._outcome(
                state=ResourceState.REMOVED,
                identity=self._identity,
                secret_id=None,
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - external driver failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        finally:
            caller_cancelled = await self._owner._close(
                connection, already_cancelled=caller_cancelled
            )
            if caller_cancelled:
                raise asyncio.CancelledError()
        if failure is not None:
            raise failure
        raise AssertionError("database teardown adapter returned no outcome")

    async def _resolve_admin_url(self) -> URL:
        if self.normalized_admin_url is None:
            failure: DatabaseProvisionerError | None = None
            try:
                reference = self._policy.admin_url_secret_reference
                assert reference is not None
                resolved = await self._owner._resolver.resolve(reference)
                self.normalized_admin_url = _normalize_admin_url(resolved)
            except asyncio.CancelledError:
                raise
            except DatabaseProvisionerError as error:
                failure = _sanitize_error(error)
            except Exception:  # noqa: BLE001 - resolver failures are redacted
                failure = DatabaseProvisionerError(_ERROR)
            if failure is not None:
                raise failure
        if self.normalized_admin_url is None:
            raise AssertionError("administrator URL was not normalized")
        return self.normalized_admin_url


class DatabaseProvisioner(DatabaseProvisionerPort):
    """Provision one exact run-scoped PostgreSQL role and database."""

    def __init__(
        self,
        *,
        operation_executor: _OperationExecutor,
        operation_repository: OperationRepository,
        admin_secret_resolver: AdminSecretResolverPort,
        secret_store: SecretStorePort,
        password_source: _TokenBytesSource,
        connection_factory: _ConnectionFactory,
    ) -> None:
        self._operation_executor = operation_executor
        self._operation_repository = operation_repository
        self._resolver = admin_secret_resolver
        self._secret_store = secret_store
        self._password_source = password_source
        self._connection_factory = connection_factory

    def validate_binding(
        self, identity: WorktreeIdentity, binding: DatabaseBinding
    ) -> DatabaseBinding:
        """Validate one exact persisted identity without contacting any dependency."""

        try:
            expected = _validate_identity(identity, enabled=True)
            if not isinstance(binding, DatabaseBinding):
                raise DatabaseIntegrityError(_INTEGRITY_ERROR)
            if binding.state not in {
                ResourceState.PROVISIONING,
                ResourceState.FAILED,
                ResourceState.ACTIVE,
            }:
                raise DatabaseIntegrityError(_INTEGRITY_ERROR)
            expected_secret = _secret_id(expected)
            for actual, wanted in (
                (binding.database_name, expected.database_name),
                (binding.database_role, expected.database_role),
                (binding.secret_id, expected_secret),
            ):
                if actual is not None and actual != wanted:
                    raise DatabaseIntegrityError(_INTEGRITY_ERROR)
            if binding.state is ResourceState.ACTIVE and (
                binding.database_name != expected.database_name
                or binding.database_role != expected.database_role
                or binding.secret_id != expected_secret
            ):
                raise DatabaseIntegrityError(_INTEGRITY_ERROR)
            return binding
        except DatabaseProvisionerError:
            raise
        except Exception:  # noqa: BLE001 - binding failures expose no adapter diagnostics
            raise DatabaseIntegrityError(_INTEGRITY_ERROR) from None

    async def verify_active(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> UUID:
        """Prove one active binding's durable intent and live database state."""

        _validate_policy_version(policy_version)
        _validate_policy(policy)
        if not policy.enabled:
            raise DatabaseIntegrityError(_INTEGRITY_ERROR)
        expected = _validate_identity(identity, enabled=True)
        self.validate_binding(expected, resource)
        if resource.state is not ResourceState.ACTIVE:
            raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
        run_id = expected.run_id
        if run_id is None:
            raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
        _validate_secret_reference(_require_reference(policy))
        secret_id = _secret_id(expected)
        request = _request(expected, policy_version, _PROVISION_KIND, ResourceState.ACTIVE)
        failure: DatabaseProvisionerError | None = None
        try:
            intent = await self._operation_repository.get_by_idempotency_key(
                request.idempotency_key
            )
            if intent is None:
                raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
            if (
                intent.run_id != run_id
                or intent.kind != request.kind
                or intent.idempotency_key != request.idempotency_key
                or intent.request_digest != request.request_digest
                or intent.request_schema_version != request.request_schema_version
                or canonical_digest(intent.request_payload)
                != canonical_digest(request.request_payload)
                or intent.status is not OperationStatus.SUCCEEDED
            ):
                raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
            expected_outcome = self._outcome(
                state=ResourceState.ACTIVE,
                identity=expected,
                secret_id=secret_id,
            )
            if (
                intent.remote_resource_id != expected_outcome.remote_resource_id
                or intent.outcome_schema_version != expected_outcome.outcome_schema_version
                or canonical_digest(intent.outcome or {})
                != canonical_digest(expected_outcome.payload)
            ):
                raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
            adapter = _ProvisionAdapter(self, expected, policy, secret_id)
            live = await adapter._run(mutate=False)
            if (
                live.status is not OperationStatus.SUCCEEDED
                or live.remote_resource_id != expected_outcome.remote_resource_id
                or live.outcome_schema_version != expected_outcome.outcome_schema_version
                or canonical_digest(live.payload) != canonical_digest(expected_outcome.payload)
            ):
                raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
            return intent.id
        except asyncio.CancelledError:
            raise
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - verification failures use a static category
            failure = DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("database active verification returned no result")

    async def provision(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        _validate_policy_version(policy_version)
        _validate_policy(policy)
        enabled = policy.enabled
        expected = _validate_identity(identity, enabled=enabled)
        if not enabled:
            return DatabaseBinding(state=ResourceState.DISABLED)
        _validate_secret_reference(_require_reference(policy))
        secret_id = _secret_id(expected)
        adapter = _ProvisionAdapter(self, expected, policy, secret_id)
        outcome = await self._execute(
            _request(expected, policy_version, _PROVISION_KIND, ResourceState.ACTIVE),
            adapter,
        )
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
        url = adapter.normalized_admin_url
        if url is None:
            url = await self._resolve_for_binding(policy)
        password = self._read_password(secret_id)
        scoped = _scoped_url(url, expected, password)
        return DatabaseBinding(
            state=ResourceState.ACTIVE,
            database_name=expected.database_name,
            database_role=expected.database_role,
            secret_id=secret_id,
            environment={policy.injected_environment_key: scoped},
        )

    async def teardown(
        self,
        identity: WorktreeIdentity,
        policy: DatabaseProvisioningPolicy,
        resource: DatabaseBinding,
        *,
        policy_version: int,
    ) -> DatabaseBinding:
        _validate_policy_version(policy_version)
        _validate_policy(policy)
        expected = _validate_identity(identity, enabled=policy.enabled)
        if not policy.enabled:
            _validate_disabled_resource(resource)
            return DatabaseBinding(state=ResourceState.DISABLED)
        _validate_teardown_resource(resource, expected)
        _validate_secret_reference(_require_reference(policy))
        adapter = _TeardownAdapter(self, expected, policy, _secret_id(expected))
        outcome = await self._execute(
            _request(expected, policy_version, _TEARDOWN_KIND, ResourceState.REMOVED),
            adapter,
        )
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise DatabaseReconciliationRequired(_RECONCILIATION_ERROR)
        return DatabaseBinding(state=ResourceState.REMOVED)

    async def _execute(
        self, request: OperationRequest, adapter: OperationAdapter
    ) -> OperationOutcome:
        failure: DatabaseProvisionerError | None = None
        try:
            return await self._operation_executor.execute(request, adapter)
        except asyncio.CancelledError:
            raise
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - executor/repository failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("operation executor returned no outcome")

    async def _resolve_for_binding(self, policy: DatabaseProvisioningPolicy) -> URL:
        failure: DatabaseProvisionerError | None = None
        try:
            resolved = await self._resolver.resolve(_require_reference(policy))
            return _normalize_admin_url(resolved)
        except asyncio.CancelledError:
            raise
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - resolver failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("administrator resolver returned no URL")

    async def _connect(self, url: URL) -> _AsyncPostgresConnection:
        failure: DatabaseProvisionerError | None = None
        try:
            connection = await self._connection_factory(
                url.set(drivername="postgresql").render_as_string(hide_password=False)
            )
            if connection is None:
                raise DatabaseProvisionerError(_ERROR)
            return connection
        except asyncio.CancelledError:
            raise
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - external driver failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("connection factory returned no connection")

    async def _close(
        self,
        connection: _AsyncPostgresConnection | None,
        *,
        already_cancelled: bool,
    ) -> bool:
        if connection is None:
            return already_cancelled
        failure: DatabaseProvisionerError | None = None
        try:
            _, caller_cancelled = await await_deferred_cancellation(
                connection.close(), already_cancelled=already_cancelled
            )
            return caller_cancelled
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - driver cleanup failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("connection cleanup returned no result")

    async def _inspect(
        self,
        connection: _AsyncPostgresConnection,
        *,
        database_name: str,
        role_name: str,
        secret_id: str,
    ) -> _ObservedState:
        failure: DatabaseProvisionerError | None = None
        try:
            role = await connection.fetchrow(_ROLE_QUERY, role_name)
            database = await connection.fetchrow(_DATABASE_QUERY, database_name)
            secret_exists = self._secret_store.exists(secret_id)
            return _ObservedState(
                role=role,
                database=database,
                secret_exists=secret_exists,
            )
        except asyncio.CancelledError:
            raise
        except DatabaseProvisionerError as error:
            failure = _sanitize_error(error)
        except Exception:  # noqa: BLE001 - external driver failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("inspection returned no state")

    def _generate_password(self) -> bytes:
        failure: DatabaseProvisionerError | None = None
        try:
            raw = self._password_source.token_bytes(_PASSWORD_BYTES)
            if type(raw) is not bytes or len(raw) != _PASSWORD_BYTES:
                raise ValueError
            return base64.urlsafe_b64encode(raw)
        except Exception:  # noqa: BLE001 - password source failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("password source returned no password")

    @staticmethod
    def _teardown_state_is_safe(
        observed: _ObservedState, database_name: str, role_name: str
    ) -> bool:
        if observed.database_exists:
            database = observed.database
            if database is None:
                return False
            return (
                observed.role is not None
                and observed.role_safe
                and _row_value(observed.role, "rolname") == role_name
                and _row_value(database, "datname") == database_name
                and observed.database_owner == role_name
                and observed.database_safe
                and observed.secret_exists
            )
        if observed.role is not None:
            return (
                observed.role_safe
                and _row_value(observed.role, "rolname") == role_name
                and observed.secret_exists
            )
        return observed.secret_exists

    @staticmethod
    def _outcome(
        *, state: ResourceState, identity: WorktreeIdentity, secret_id: str | None
    ) -> OperationOutcome:
        return OperationOutcome(
            status=OperationStatus.SUCCEEDED,
            payload={
                "state": state.value,
                "database_name": identity.database_name,
                "database_role": identity.database_role,
                "secret_id": secret_id,
            },
        )

    @staticmethod
    def _needs_reconciliation() -> OperationOutcome:
        return OperationOutcome(
            status=OperationStatus.NEEDS_RECONCILIATION,
            error=_RECONCILIATION_ERROR,
        )

    def _read_password(self, secret_id: str) -> str:
        failure: DatabaseProvisionerError | None = None
        try:
            password = self._secret_store.read(secret_id)
            if type(password) is not bytes or not password:
                raise ValueError
            value = password.decode("ascii")
            decoded = base64.urlsafe_b64decode(password)
            if len(decoded) != _PASSWORD_BYTES or base64.urlsafe_b64encode(decoded) != password:
                raise ValueError
            return value
        except Exception:  # noqa: BLE001 - secret store failures are redacted
            failure = DatabaseProvisionerError(_ERROR)
        if failure is not None:
            raise failure
        raise AssertionError("secret store returned no password")


def _validate_policy(policy: DatabaseProvisioningPolicy) -> None:
    if not isinstance(policy, DatabaseProvisioningPolicy):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    if type(policy.enabled) is not bool:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    if (
        not isinstance(policy.injected_environment_key, str)
        or _ENVIRONMENT_KEY.fullmatch(policy.injected_environment_key) is None
    ):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    reference = policy.admin_url_secret_reference
    if (policy.enabled and reference is None) or (not policy.enabled and reference is not None):
        raise DatabaseIntegrityError(_REFERENCE_ERROR)


def _validate_policy_version(value: int) -> None:
    if type(value) is not int or value < 1:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)


def _validate_identity(identity: WorktreeIdentity, *, enabled: bool) -> WorktreeIdentity:
    if not isinstance(identity, WorktreeIdentity) or identity.run_id is None:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    failure: DatabaseIntegrityError | None = None
    try:
        expected = WorktreeIdentity.for_run(
            identity.project_id,
            identity.run_id,
            identity.branch,
            enabled,
        )
    except Exception:  # noqa: BLE001 - malformed identity failures are redacted
        failure = DatabaseIntegrityError(_INTEGRITY_ERROR)
    if failure is not None:
        raise failure
    if expected != identity:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    return expected


def _validate_disabled_resource(resource: DatabaseBinding) -> None:
    if (
        not isinstance(resource, DatabaseBinding)
        or resource.state is not ResourceState.DISABLED
        or any(
            value is not None
            for value in (resource.database_name, resource.database_role, resource.secret_id)
        )
    ):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)


def _validate_teardown_resource(resource: DatabaseBinding, identity: WorktreeIdentity) -> None:
    if not isinstance(resource, DatabaseBinding):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    if resource.state is ResourceState.REMOVED and all(
        value is None
        for value in (resource.database_name, resource.database_role, resource.secret_id)
    ):
        return
    expected_secret = _secret_id(identity)
    if resource.state is ResourceState.ACTIVE and (
        resource.database_name != identity.database_name
        or resource.database_role != identity.database_role
        or resource.secret_id != expected_secret
    ):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    if resource.state in {ResourceState.PROVISIONING, ResourceState.FAILED}:
        for actual, expected in (
            (resource.database_name, identity.database_name),
            (resource.database_role, identity.database_role),
            (resource.secret_id, expected_secret),
        ):
            if actual is not None and actual != expected:
                raise DatabaseIntegrityError(_INTEGRITY_ERROR)
        return
    if resource.state is not ResourceState.ACTIVE:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)


def _secret_id(identity: WorktreeIdentity) -> str:
    if identity.run_id is None:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    return f"forge_db_{identity.project_id.hex}_{identity.run_id.hex}"


def _request(
    identity: WorktreeIdentity,
    policy_version: int,
    operation_kind: str,
    state: ResourceState,
) -> OperationRequest:
    run_id = identity.run_id
    if run_id is None:
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    database_name = _require_name(identity.database_name)
    database_role = _require_name(identity.database_role)
    secret_id = _secret_id(identity)
    branch_digest = hashlib.sha256(identity.branch.encode("utf-8")).hexdigest()
    payload: dict[str, object] = {
        "project_id": str(identity.project_id),
        "run_id": str(run_id),
        "policy_version": policy_version,
        "branch_digest": branch_digest,
        "worktree_name": identity.worktree_name,
        "database_name": database_name,
        "database_role": database_role,
        "secret_id": secret_id,
        "target_state": state.value,
    }
    return OperationRequest(
        run_id=run_id,
        kind=operation_kind,
        idempotency_key=(
            f"forge-db-v{_PROTOCOL_VERSION}:{operation_kind}:{identity.project_id.hex}:"
            f"{run_id.hex}:{policy_version}"
        ),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _validate_secret_reference(reference: str) -> None:
    if not isinstance(reference, str) or not reference.startswith("secret://"):
        raise DatabaseIntegrityError(_REFERENCE_ERROR)
    if reference != reference.strip() or any(
        marker in reference for marker in ("?", "#", "%", "\\")
    ):
        raise DatabaseIntegrityError(_REFERENCE_ERROR)
    remainder = reference.removeprefix("secret://")
    segments = remainder.split("/")
    if not segments or any(
        not segment
        or segment in {".", ".."}
        or _SECRET_REFERENCE_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise DatabaseIntegrityError(_REFERENCE_ERROR)


def _normalize_admin_url(value: str) -> URL:
    if not isinstance(value, str) or not value.strip():
        raise DatabaseProvisionerError(_URL_ERROR)
    # Fragments are not sent to a PostgreSQL driver; strip only the fragment
    # delimiter while leaving percent-encoded '#' characters untouched.
    raw = value.split("#", 1)[0]
    failure: DatabaseProvisionerError | None = None
    try:
        parsed = make_url(raw)
        driver = parsed.drivername.casefold()
        if driver not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError
        if not parsed.host or not parsed.username or not parsed.password:
            raise ValueError
        _ = parsed.port
        safe_query: dict[str, str] = {}
        for key, values in parsed.normalized_query.items():
            allowed_values = _SAFE_QUERY_OPTIONS.get(key)
            if allowed_values is None or len(values) != 1 or values[0] not in allowed_values:
                raise ValueError
            safe_query[key] = values[0]
        return parsed.set(drivername="postgresql", database="postgres", query=safe_query)
    except Exception:  # noqa: BLE001 - URL parser failures are redacted
        failure = DatabaseProvisionerError(_URL_ERROR)
    if failure is not None:
        raise failure
    raise AssertionError("URL parser returned no URL")


def _scoped_url(admin_url: URL, identity: WorktreeIdentity, password: str) -> str:
    failure: DatabaseProvisionerError | None = None
    try:
        scoped = admin_url.set(
            drivername="postgresql",
            username=_require_name(identity.database_role),
            password=password,
            database=_require_name(identity.database_name),
        ).render_as_string(hide_password=False)
    except Exception:  # noqa: BLE001 - URL builder failures are redacted
        failure = DatabaseProvisionerError(_URL_ERROR)
    if failure is not None:
        raise failure
    return scoped


def _quote_identifier(value: str) -> str:
    return f'"{_require_name(value)}"'


def _quote_literal(value: bytes) -> str:
    text: str | None = None
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        pass
    if text is None:
        raise DatabaseProvisionerError(_ERROR)
    return "'" + text.replace("'", "''") + "'"


def _create_role_sql(role_name: str, password: bytes) -> str:
    return (
        f"CREATE ROLE {_quote_identifier(role_name)} LOGIN INHERIT "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
        "CONNECTION LIMIT -1 PASSWORD "
        f"{_quote_literal(password)}"
    )


def _require_name(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or len(value.encode("utf-8")) > 63
    ):
        raise DatabaseIntegrityError(_INTEGRITY_ERROR)
    return value


def _require_reference(policy: DatabaseProvisioningPolicy) -> str:
    reference = policy.admin_url_secret_reference
    if reference is None:
        raise DatabaseIntegrityError(_REFERENCE_ERROR)
    return reference


def _raw_row_value(row: Mapping[str, object], key: str) -> object:
    try:
        return row[key]
    except KeyError, IndexError, TypeError, AttributeError:
        return _ROW_MISSING


def _row_value(row: Mapping[str, object], key: str) -> object | None:
    value = _raw_row_value(row, key)
    return None if value is _ROW_MISSING else value


def _as_bool(row: Mapping[str, object], key: str) -> bool | None:
    value = _raw_row_value(row, key)
    return value if type(value) is bool else None


def _as_int(row: Mapping[str, object], key: str) -> int:
    value = _raw_row_value(row, key)
    return value if type(value) is int else -2


def _as_optional_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    value = _raw_row_value(row, key)
    return (
        value
        if isinstance(value, datetime)
        else None
        if value is None
        else datetime.min.replace(tzinfo=UTC)
    )


def _as_setting_empty(row: Mapping[str, object], key: str) -> bool:
    value = _raw_row_value(row, key)
    return value is None or value == [] or value == ()


__all__ = [
    "DatabaseBinding",
    "DatabaseIntegrityError",
    "DatabaseProvisioner",
    "DatabaseProvisionerError",
    "DatabaseReconciliationRequired",
]
