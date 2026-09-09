"""Recoverable standalone developer worktree orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from forge.application.ports.runner import RunCommandRequest
from forge.application.ports.worktrees import DatabaseBinding, ManagedWorktree
from forge.domain.policy import ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import ResourceState, WorktreeIdentity, database_secret_id
from forge.domain.validation import command_spec_digest
from forge.tools.worktree_manifest import (
    DeveloperWorktreeManifest,
    WorktreeManifestStore,
)

_ERROR = "developer worktree operation failed"
_SETUP_KINDS = (
    StepKind.BOOTSTRAP,
    StepKind.INSTALL,
    StepKind.MIGRATION,
    StepKind.SEED,
)


class DeveloperWorktreeError(RuntimeError):
    """A standalone lifecycle action failed without exposing diagnostics."""

    def __init__(self) -> None:
        super().__init__(_ERROR)


class DeveloperWorktreeLifecycle:
    """Prepare and remove one exact developer identity through local checkpoints."""

    def __init__(
        self,
        *,
        git: Any,
        database: Any,
        environment_stager: Any,
        runner_factory: Any,
        manifests: WorktreeManifestStore,
    ) -> None:
        self._git = git
        self._database = database
        self._stager = environment_stager
        self._runner_factory = runner_factory
        self._manifests = manifests

    async def setup(
        self,
        policy: ProjectPolicy,
        branch: str,
        *,
        bootstrap: bool = True,
    ) -> ManagedWorktree:
        """Create or resume one standalone worktree and its approved setup."""

        try:
            identity = self._identity(policy, branch)
            manifest = await self._load_or_initialize(policy, identity)
            worktree = self._git.expected_worktree(identity, manifest.base_sha)
            manifest = await self._ensure_worktree(policy, manifest, worktree)
            binding, manifest = await self._ensure_database(policy, manifest, identity)
            plan = await asyncio.to_thread(
                self._stager.build_plan,
                worktree,
                policy,
                binding,
                policy_version=policy.version,
            )
            if "environment.staged" not in manifest.completed_checkpoints:
                await asyncio.to_thread(self._stager.publish, worktree, policy, plan)
                inspection = await asyncio.to_thread(self._stager.inspect, worktree, policy, plan)
                if not inspection.present or inspection.evidence != plan.evidence:
                    raise DeveloperWorktreeError()
                manifest = await self._checkpoint(manifest, "environment.staged")
            else:
                inspection = await asyncio.to_thread(self._stager.inspect, worktree, policy, plan)
                if not inspection.present or inspection.evidence != plan.evidence:
                    raise DeveloperWorktreeError()

            if bootstrap:
                manifest = await self._run_setup_commands(policy, manifest, worktree, binding)
            if "setup.complete" not in manifest.completed_checkpoints:
                manifest = await self._checkpoint(manifest, "setup.complete")
            inspected = await asyncio.to_thread(
                self._git.inspect_worktree, identity, manifest.base_sha
            )
            if inspected != worktree:
                raise DeveloperWorktreeError()
            return cast(ManagedWorktree, worktree)
        except asyncio.CancelledError:
            raise
        except DeveloperWorktreeError:
            raise
        except Exception:  # noqa: BLE001 - public lifecycle failures are redacted
            raise DeveloperWorktreeError() from None

    async def teardown(self, policy: ProjectPolicy, branch: str) -> None:
        """Remove the exact manifested worktree, then its optional database."""

        try:
            identity = self._identity(policy, branch)
            manifest = await asyncio.to_thread(self._manifests.load, policy.id, branch)
            self._validate_manifest(policy, identity, manifest)
            worktree = self._git.expected_worktree(identity, manifest.base_sha)

            if "worktree.removed" not in manifest.completed_checkpoints:
                try:
                    inspected = await asyncio.to_thread(
                        self._git.inspect_worktree, identity, manifest.base_sha
                    )
                except Exception:  # noqa: BLE001 - exact absence proof decides recovery
                    await asyncio.to_thread(self._git.verify_worktree_absent, worktree)
                    inspected = None
                if inspected is not None:
                    if inspected != worktree:
                        raise DeveloperWorktreeError()
                    await asyncio.to_thread(self._git.remove_worktree, worktree)
                await asyncio.to_thread(self._git.verify_worktree_absent, worktree)
                await asyncio.to_thread(self._git.prune)
                manifest = await self._checkpoint(manifest, "worktree.removed")
            else:
                await asyncio.to_thread(self._git.verify_worktree_absent, worktree)

            if policy.database.enabled and "database.removed" not in manifest.completed_checkpoints:
                binding = DatabaseBinding(
                    state=manifest.database_state,
                    database_name=manifest.database_name,
                    database_role=manifest.database_role,
                    secret_id=manifest.secret_id,
                )
                removed = await self._database.teardown_standalone(
                    identity,
                    policy.database,
                    binding,
                    policy_version=policy.version,
                )
                if removed.state is not ResourceState.REMOVED:
                    raise DeveloperWorktreeError()
                manifest = await self._checkpoint(manifest, "database.removed")
            await asyncio.to_thread(self._manifests.delete, manifest)
        except asyncio.CancelledError:
            raise
        except DeveloperWorktreeError:
            raise
        except Exception:  # noqa: BLE001 - public lifecycle failures are redacted
            raise DeveloperWorktreeError() from None

    async def _load_or_initialize(
        self,
        policy: ProjectPolicy,
        identity: WorktreeIdentity,
    ) -> DeveloperWorktreeManifest:
        exists = await asyncio.to_thread(self._manifests.exists, policy.id, identity.branch)
        if exists:
            manifest = await asyncio.to_thread(self._manifests.load, policy.id, identity.branch)
            self._validate_manifest(policy, identity, manifest)
            return manifest
        base_sha = await asyncio.to_thread(self._git.resolve_default_base_sha)
        expected = self._git.expected_worktree(identity, base_sha)
        enabled = policy.database.enabled
        manifest = DeveloperWorktreeManifest(
            project_id=policy.id,
            repository_path=str(Path(policy.repository_path)),
            branch=identity.branch,
            worktree_name=identity.worktree_name,
            worktree_path=str(expected.path),
            base_sha=base_sha,
            policy_version=policy.version,
            database_state=(ResourceState.PROVISIONING if enabled else ResourceState.DISABLED),
            database_name=identity.database_name,
            database_role=identity.database_role,
            secret_id=database_secret_id(identity) if enabled else None,
            completed_checkpoints=("manifest.created",),
        )
        await asyncio.to_thread(self._manifests.create, manifest)
        return manifest

    async def _ensure_worktree(
        self,
        policy: ProjectPolicy,
        manifest: DeveloperWorktreeManifest,
        expected: ManagedWorktree,
    ) -> DeveloperWorktreeManifest:
        del policy
        inspected = await asyncio.to_thread(
            self._git.inspect_worktree, expected.identity, expected.base_sha
        )
        if "worktree.created" in manifest.completed_checkpoints:
            if inspected != expected:
                raise DeveloperWorktreeError()
            return manifest
        if inspected is None:
            inspected = await asyncio.to_thread(
                self._git.create_worktree, expected.identity, expected.base_sha
            )
        if inspected != expected:
            raise DeveloperWorktreeError()
        return await self._checkpoint(manifest, "worktree.created")

    async def _ensure_database(
        self,
        policy: ProjectPolicy,
        manifest: DeveloperWorktreeManifest,
        identity: WorktreeIdentity,
    ) -> tuple[DatabaseBinding, DeveloperWorktreeManifest]:
        if not policy.database.enabled:
            return DatabaseBinding(state=ResourceState.DISABLED), manifest
        resource = DatabaseBinding(
            state=manifest.database_state,
            database_name=manifest.database_name,
            database_role=manifest.database_role,
            secret_id=manifest.secret_id,
        )
        if "database.active" in manifest.completed_checkpoints:
            binding = await self._database.rematerialize_standalone(
                identity,
                policy.database,
                resource,
                policy_version=policy.version,
            )
            return binding, manifest
        binding = await self._database.provision_standalone(
            identity,
            policy.database,
            policy_version=policy.version,
        )
        if binding.state is not ResourceState.ACTIVE:
            raise DeveloperWorktreeError()
        updated = manifest.model_copy(
            update={
                "database_state": ResourceState.ACTIVE,
                "database_name": binding.database_name,
                "database_role": binding.database_role,
                "secret_id": binding.secret_id,
            }
        )
        await asyncio.to_thread(self._manifests.save, updated)
        updated = await self._checkpoint(updated, "database.active")
        return binding, updated

    async def _run_setup_commands(
        self,
        policy: ProjectPolicy,
        manifest: DeveloperWorktreeManifest,
        worktree: ManagedWorktree,
        binding: DatabaseBinding,
    ) -> DeveloperWorktreeManifest:
        runner = self._runner_factory.create(worktree, policy)
        ordinal = 0
        for kind in _SETUP_KINDS:
            for command in policy.commands_for(kind):
                digest = command_spec_digest(command)
                checkpoint = f"setup.command:{ordinal}:{digest}"
                started = f"setup.command-started:{ordinal}:{digest}"
                if checkpoint not in manifest.completed_checkpoints:
                    if started in manifest.completed_checkpoints:
                        raise DeveloperWorktreeError()
                    manifest = await self._checkpoint(manifest, started)
                    selected = {
                        key: binding.environment[key]
                        for key in command.environment_keys
                        if key in binding.environment
                    }
                    terminal = await runner.run_terminal(
                        RunCommandRequest(
                            command_name=command.name,
                            kind=kind,
                            environment=selected,
                        )
                    )
                    result = terminal.result
                    if (
                        terminal.caller_cancelled
                        or result.command_name != command.name
                        or result.kind is not kind
                        or result.command_digest != digest
                        or result.policy_version != policy.version
                        or result.runner_mode is not policy.runner_mode
                        or result.unsandboxed != (policy.runner_mode is RunnerMode.TRUSTED_HOST)
                        or result.exit_code != 0
                        or result.timed_out
                    ):
                        raise DeveloperWorktreeError()
                    manifest = await self._checkpoint(manifest, checkpoint)
                ordinal += 1
        return manifest

    async def _checkpoint(
        self,
        manifest: DeveloperWorktreeManifest,
        checkpoint: str,
    ) -> DeveloperWorktreeManifest:
        if checkpoint in manifest.completed_checkpoints:
            return manifest
        updated = manifest.model_copy(
            update={
                "completed_checkpoints": (*manifest.completed_checkpoints, checkpoint),
            }
        )
        await asyncio.to_thread(self._manifests.save, updated)
        return updated

    def _identity(self, policy: ProjectPolicy, branch: str) -> WorktreeIdentity:
        if not isinstance(policy, ProjectPolicy):
            raise DeveloperWorktreeError()
        if Path(policy.repository_path) != Path(self._git.repository_path):
            raise DeveloperWorktreeError()
        return WorktreeIdentity.for_developer(
            policy.id,
            branch,
            policy.database.enabled,
        )

    def _validate_manifest(
        self,
        policy: ProjectPolicy,
        identity: WorktreeIdentity,
        manifest: DeveloperWorktreeManifest,
    ) -> None:
        expected = self._git.expected_worktree(identity, manifest.base_sha)
        if (
            manifest.project_id != policy.id
            or manifest.repository_path != str(Path(policy.repository_path))
            or manifest.branch != identity.branch
            or manifest.worktree_name != identity.worktree_name
            or Path(manifest.worktree_path) != expected.path
            or manifest.policy_version != policy.version
            or (manifest.database_state is ResourceState.DISABLED) != (not policy.database.enabled)
        ):
            raise DeveloperWorktreeError()


__all__ = ["DeveloperWorktreeError", "DeveloperWorktreeLifecycle"]
