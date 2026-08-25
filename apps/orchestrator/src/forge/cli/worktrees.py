"""Standalone developer worktree CLI wiring."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, cast

import asyncpg  # type: ignore[import-untyped]
import typer

from forge.application.ports.operations import OperationRepository
from forge.application.services.recovery import OperationExecutor
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.tools.database import DatabaseProvisioner
from forge.tools.developer_worktree import DeveloperWorktreeError, DeveloperWorktreeLifecycle
from forge.tools.environment import EnvironmentStager
from forge.tools.git import ControlledGit
from forge.tools.paths import CanonicalRoot
from forge.tools.secrets import LocalSecretStore
from forge.tools.worktree_manifest import WorktreeManifestStore
from forge.tools.worktree_runner import WorktreeRunnerFactory

_ERROR = "Forge worktree operation failed."
_ENV_REFERENCE = re.compile(r"secret://environment/([A-Z][A-Z0-9_]{0,127})\Z")

worktree_app = typer.Typer(add_completion=False, no_args_is_help=True)


class EnvironmentAdminSecretResolver:
    """Resolve one explicitly named process environment secret on demand."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def resolve(self, reference: str) -> str:
        match = _ENV_REFERENCE.fullmatch(reference)
        if match is None:
            raise DeveloperWorktreeError()
        value = os.environ.get(match.group(1))
        if not value:
            raise DeveloperWorktreeError()
        return value


@worktree_app.command("setup")
def setup_worktree(
    branch: str = typer.Option(..., "--branch"),
    bootstrap: bool = typer.Option(True, "--bootstrap/--no-bootstrap"),
) -> None:
    """Create or resume the exact developer worktree for this repository."""

    try:
        path = asyncio.run(_setup(Settings(process_role="cli"), Path.cwd(), branch, bootstrap))
    except KeyboardInterrupt, asyncio.CancelledError:
        raise typer.Exit(code=130) from None
    except Exception:  # noqa: BLE001 - CLI errors are intentionally one redacted category
        typer.echo(_ERROR, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Forge worktree is ready: {path}")


@worktree_app.command("teardown")
def teardown_worktree(
    branch: str = typer.Option(..., "--branch"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Remove the exact manifested worktree while retaining its branch."""

    if not yes and not typer.confirm(
        "Remove this Forge worktree and its optional database? The branch will be retained."
    ):
        typer.echo("Forge worktree teardown cancelled; no resources were changed.")
        return
    try:
        asyncio.run(_teardown(Settings(process_role="cli"), Path.cwd(), branch))
    except KeyboardInterrupt, asyncio.CancelledError:
        raise typer.Exit(code=130) from None
    except Exception:  # noqa: BLE001 - CLI errors are intentionally one redacted category
        typer.echo(_ERROR, err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Forge worktree resources were removed; the branch was retained.")


async def _setup(settings: Settings, cwd: Path, branch: str, bootstrap: bool) -> Path:
    policy = await _load_policy(settings, cwd)
    service = _build_lifecycle(settings, policy)
    worktree = await service.setup(policy, branch, bootstrap=bootstrap)
    return worktree.path


async def _teardown(settings: Settings, cwd: Path, branch: str) -> None:
    policy = await _load_policy(settings, cwd)
    service = _build_lifecycle(settings, policy)
    await service.teardown(policy, branch)


async def _load_policy(settings: Settings, cwd: Path) -> ProjectPolicy:
    root = CanonicalRoot(cwd)
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with PostgresUnitOfWork(factory) as work:
            matches = [
                record
                for record in await work.projects.list()
                if os.path.normcase(str(Path(record.canonical_path)))
                == os.path.normcase(str(root.path))
            ]
            await work.commit()
        if len(matches) != 1:
            raise DeveloperWorktreeError()
        project = matches[0]
        current = project.policy
        if current is None or project.current_policy_version != current.version:
            raise DeveloperWorktreeError()
        values = dict(current.document)
        values.update(
            {
                "id": project.id,
                "version": current.version,
                "repository_path": project.canonical_path,
                "github_repository": project.github_repository,
                "default_branch": project.default_branch,
            }
        )
        return ProjectPolicy.model_validate(values)
    finally:
        await engine.dispose()


def _build_lifecycle(settings: Settings, policy: ProjectPolicy) -> DeveloperWorktreeLifecycle:
    repository = CanonicalRoot(policy.repository_path)
    data_root = settings.data_root.resolve()
    if _overlaps(repository.path, data_root):
        raise DeveloperWorktreeError()
    git_path = shutil.which("git")
    if git_path is None:
        raise DeveloperWorktreeError()
    git = ControlledGit(
        repository,
        default_branch=policy.default_branch,
        state_root=data_root / "git" / policy.id.hex,
        git_executable=Path(git_path).resolve(strict=True),
    )
    operations = cast(OperationRepository, _UnavailableOperations())
    database = DatabaseProvisioner(
        operation_executor=OperationExecutor(operations),
        operation_repository=operations,
        admin_secret_resolver=EnvironmentAdminSecretResolver(),
        secret_store=LocalSecretStore(data_root),
        password_source=secrets,
        connection_factory=asyncpg.connect,
    )
    runner_factory = WorktreeRunnerFactory(
        git,
        image_digest=(settings.runner_image if policy.runner_mode is RunnerMode.DOCKER else None),
    )
    return DeveloperWorktreeLifecycle(
        git=git,
        database=database,
        environment_stager=EnvironmentStager(git),
        runner_factory=runner_factory,
        manifests=WorktreeManifestStore(data_root),
    )


def _overlaps(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


class _UnavailableOperations:
    """Guard proving the standalone path cannot use run operation persistence."""

    async def create_or_get(self, request: Any) -> Any:
        del request
        raise DeveloperWorktreeError()

    async def claim(self, intent_id: Any, *, lease_owner: str, lease_seconds: int) -> Any:
        del intent_id, lease_owner, lease_seconds
        raise DeveloperWorktreeError()

    async def mark_succeeded(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise DeveloperWorktreeError()

    async def mark_failed(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise DeveloperWorktreeError()

    async def get_by_idempotency_key(self, key: str) -> Any:
        del key
        raise DeveloperWorktreeError()


__all__ = ["EnvironmentAdminSecretResolver", "worktree_app"]
