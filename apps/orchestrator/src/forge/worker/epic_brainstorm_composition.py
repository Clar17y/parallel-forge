"""Composition helpers for subject-specific brainstorm worker gateway and readers."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.client_process import ClientProcessSupervisor
from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway
from forge.application.ports.epic_brainstorm import BrainstormGateway
from forge.application.ports.repository import RepositoryReader as RepositoryReaderPort
from forge.domain.epic_brainstorm import AuthoringJobSnapshot
from forge.domain.local_cli import LocalCliTrust
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription_installations import (
    SubscriptionInstallationSpec,
    load_subscription_installation_manifest,
    quota_route_for,
)
from forge.domain.subscription_quota import QuotaPoolKey
from forge.persistence.repositories.projects import PostgresProjectRepository
from forge.settings import Settings
from forge.tools.repository import RepositoryReader


def _matches_route(
    spec: SubscriptionInstallationSpec,
    snapshot: AuthoringJobSnapshot,
    quota_key: QuotaPoolKey,
) -> bool:
    route = snapshot.route.effective
    expected = quota_route_for(spec)
    if not expected.matches(route) or quota_key != QuotaPoolKey(
        expected.provider, expected.account, expected.pool
    ):
        return False
    return not (route.effort is not None and spec.effort != route.effort.value)


def make_brainstorm_gateway_factory(
    settings: Settings,
    *,
    installations: Sequence[SubscriptionInstallationSpec] | None = None,
    supervisor: ClientProcessSupervisor | None = None,
    trust: LocalCliTrust | None = None,
) -> Callable[[AuthoringJobSnapshot], BrainstormGateway]:
    """Resolve the installed route frozen in each admitted job."""
    resolved_supervisor = supervisor or ClientProcessSupervisor()
    resolved_trust = trust if trust is not None else settings.subscription_client_trust
    if installations is None:
        manifest = load_subscription_installation_manifest(settings.subscription_installations_path)
        installations = manifest.installations if manifest is not None else ()
    frozen_installations = tuple(installations)

    def gateway_factory(snapshot: AuthoringJobSnapshot) -> BrainstormGateway:
        quota_key = settings.subscription_quota_policy.key_for(snapshot.route.effective)
        matched = next(
            (item for item in frozen_installations if _matches_route(item, snapshot, quota_key)),
            None,
        )
        return EpicBrainstormGateway(
            installation=matched,
            supervisor=resolved_supervisor,
            trust=resolved_trust,
        )

    return gateway_factory


ProjectResolver = Callable[[UUID], tuple[str, Sequence[str]] | Awaitable[tuple[str, Sequence[str]]]]


def make_brainstorm_reader_factory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_resolver: ProjectResolver | None = None,
) -> Callable[[AuthoringJobSnapshot], Awaitable[RepositoryReaderPort]]:
    """Read current project policy for each job without blocking the event loop."""

    async def reader_factory(snapshot: AuthoringJobSnapshot) -> RepositoryReaderPort:
        secret_paths: Sequence[str]
        if project_resolver is not None:
            resolved = project_resolver(snapshot.project_id)
            canonical_path, secret_paths = (
                await resolved if inspect.isawaitable(resolved) else resolved
            )
        else:
            async with session_factory() as session:
                projects = PostgresProjectRepository(session)
                project = await projects.get(snapshot.project_id)
                secret_paths = ()
                if project.current_policy_version is not None:
                    record = await projects.get_policy(
                        snapshot.project_id, project.current_policy_version
                    )
                    policy = ProjectPolicy.model_validate(record.document)
                    secret_paths = policy.effective_secret_paths
                canonical_path = project.canonical_path
        return RepositoryReader(root=canonical_path, secret_paths=secret_paths)

    return reader_factory


__all__ = ["make_brainstorm_gateway_factory", "make_brainstorm_reader_factory"]
