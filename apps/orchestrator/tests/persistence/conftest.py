"""PostgreSQL fixtures with fail-closed test-database cleanup."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TEST_DATABASE_NAME = re.compile(r"\Aforge_test_[0-9a-f]{32}\Z")


def validated_test_database_name(value: str) -> str:
    """Return a safe test database identifier or fail before destructive SQL."""

    if TEST_DATABASE_NAME.fullmatch(value) is None:
        raise ValueError("refusing to manage a database outside the forge_test namespace")
    return value


def _connection_kwargs(url: URL, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "127.0.0.1",
        "port": url.port or 5432,
        "database": database,
    }


async def _create_database(admin_url: URL, database_name: str) -> None:
    name = validated_test_database_name(database_name)
    connection = await asyncpg.connect(**_connection_kwargs(admin_url, "postgres"))
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


async def _drop_database(admin_url: URL, database_name: str) -> None:
    name = validated_test_database_name(database_name)
    connection = await asyncpg.connect(**_connection_kwargs(admin_url, "postgres"))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'DROP DATABASE "{name}"')
    finally:
        await connection.close()


@pytest.fixture
def test_database_url() -> Iterator[str]:
    """Create a unique PostgreSQL database and remove only that validated name."""

    source = make_url("postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge")
    database_name = validated_test_database_name(f"forge_test_{uuid4().hex}")
    asyncio.run(_create_database(source, database_name))
    try:
        yield source.set(database=database_name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_drop_database(source, database_name))


def alembic_config(database_url: str) -> Config:
    """Build an Alembic config bound to one disposable test database."""

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "apps/orchestrator/migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def alembic_config_factory() -> Callable[[str], Config]:
    """Expose test-local Alembic configuration without importing conftest."""

    return alembic_config


@pytest.fixture
def database_name_validator() -> Callable[[str], str]:
    """Expose the destructive-operation guard for direct regression tests."""

    return validated_test_database_name


@pytest.fixture
def migrated_database_url(test_database_url: str) -> Iterator[str]:
    """Upgrade a disposable database and prove the migration can be removed."""

    config = alembic_config(test_database_url)
    command.upgrade(config, "head")
    try:
        yield test_database_url
    finally:
        command.downgrade(config, "base")
