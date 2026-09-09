"""Fixed-target Git publication; durable admission belongs to the controller."""

from __future__ import annotations

import asyncio
import base64
import re

from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.policy import ProjectPolicy
from forge.release.credentials import (
    GitHubCredentialResolverPort,
    validate_github_credential_reference,
)
from forge.tools.git import ControlledGit, ControlledGitError

_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
# Network publication reads only these inert repository settings. In particular,
# includes, URL rewrites, pushurl, helpers, HTTP settings and executable overrides
# cannot become part of the authenticated Git process.
_LOCAL_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "core.ignorecase",
        "core.autocrlf",
        "core.safecrlf",
        "core.eol",
        "core.longpaths",
        "core.precomposeunicode",
        "user.name",
        "user.email",
        "remote.origin.url",
        "remote.origin.fetch",
    }
)


class ManagedPushError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("managed Git publication requires reconciliation")


class ManagedPush:
    """Trusted release adapter, deliberately absent from agent tool bundles."""

    def __init__(
        self, git: ControlledGit, credentials: GitHubCredentialResolverPort, reference: str
    ) -> None:
        self._git = git
        self._credentials = credentials
        self._reference = validate_github_credential_reference(reference)

    def __repr__(self) -> str:
        return "ManagedPush()"

    async def push(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, approved_sha: str
    ) -> None:
        try:
            token = await self._credentials.resolve(self._reference)
            if not isinstance(token, str) or not 1 <= len(token) <= 4096:
                raise ManagedPushError()
            if any(not 33 <= ord(character) <= 126 for character in token):
                raise ManagedPushError()
            await asyncio.to_thread(self._publish, worktree, policy, approved_sha, token)
        except asyncio.CancelledError:
            # Cancellation cannot establish the remote outcome. The durable
            # operation intent must remain unsettled for remote-SHA inspection.
            raise
        except OSError, RuntimeError, TypeError, ValueError:
            raise ManagedPushError() from None

    def _publish(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, approved_sha: str, token: str
    ) -> None:
        if (
            not isinstance(approved_sha, str)
            or _SHA.fullmatch(approved_sha) is None
            or _REPOSITORY.fullmatch(policy.github_repository) is None
            or worktree.identity.run_id is None
            or worktree.identity.branch == policy.default_branch
        ):
            raise ManagedPushError()
        with self._git.open_worktree_capability(
            worktree, policy, allow_committed_changes=True
        ) as capability:
            if self._git.head_sha(worktree) != approved_sha:
                raise ManagedPushError()
            configuration = _remote_configuration(self._git, worktree, policy, token)
            capability.revalidate()
            result = self._git._run(
                worktree.path,
                (
                    "push",
                    "--porcelain",
                    "--no-follow-tags",
                    "--recurse-submodules=no",
                    "origin",
                    f"{approved_sha}:refs/heads/{worktree.identity.branch}",
                ),
                configuration=configuration,
            )
            if result.stdout_truncated or result.stderr_truncated:
                raise ControlledGitError()
            capability.revalidate()


def _remote_configuration(
    git: ControlledGit,
    worktree: ManagedWorktree,
    policy: ProjectPolicy,
    token: str,
) -> tuple[tuple[str, str], ...]:
    """Shared fixed-origin credentials/configuration for trusted release transfers."""
    if (
        _REPOSITORY.fullmatch(policy.github_repository) is None
        or worktree.identity.run_id is None
        or worktree.identity.branch == policy.default_branch
        or not isinstance(token, str)
        or not 1 <= len(token) <= 4096
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise ManagedPushError()
    url = f"https://github.com/{policy.github_repository}.git"
    keys = git._run(
        worktree.path,
        (
            "config",
            "--local",
            "--no-includes",
            "--name-only",
            "--null",
            "--list",
        ),
    )
    if keys.stdout_truncated or keys.stderr_truncated:
        raise ManagedPushError()
    for key in keys.stdout.removesuffix("\x00").split("\x00"):
        if key.lower() not in _LOCAL_KEYS and not (
            key.startswith("branch.") and key.rsplit(".", 1)[-1] in {"remote", "merge"}
        ):
            raise ManagedPushError()
    origin = git._run(
        worktree.path,
        (
            "config",
            "--local",
            "--no-includes",
            "--get-all",
            "remote.origin.url",
        ),
    )
    if origin.stdout_truncated or origin.stdout != url + "\n":
        raise ManagedPushError()
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return (
        ("http.followRedirects", "false"),
        ("http.sslVerify", "true"),
        ("http.extraHeader", ""),
        (f"http.{url}.extraHeader", f"Authorization: Basic {authorization}"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("push.followTags", "false"),
        ("push.gpgSign", "false"),
        ("remote.origin.mirror", "false"),
        ("http.lowSpeedLimit", "1"),
        ("http.lowSpeedTime", "15"),
    )
