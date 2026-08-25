from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.worktree_manifest import (
    DeveloperWorktreeManifest,
    WorktreeManifestError,
    WorktreeManifestStore,
)
from pydantic import ValidationError


def _manifest(root: Path, branch: str = "feature/a") -> DeveloperWorktreeManifest:
    project_id = uuid4()
    identity = WorktreeIdentity.for_developer(project_id, branch, False)
    return DeveloperWorktreeManifest(
        project_id=project_id,
        repository_path=str(root.resolve()),
        branch=branch,
        worktree_name=identity.worktree_name,
        worktree_path=str((root / ".worktrees" / identity.worktree_name).resolve()),
        base_sha="a" * 40,
        policy_version=3,
        database_state=ResourceState.DISABLED,
        completed_checkpoints=("manifest.created",),
    )


def test_filename_uses_full_branch_digest(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    first = _manifest(tmp_path, "feature/a")
    second = first.model_copy(update={"branch": "feature-a"})
    assert (
        store.path_for(first.project_id, first.branch).name
        != store.path_for(second.project_id, second.branch).name
    )
    assert (
        hashlib.sha256(first.branch.encode()).hexdigest()
        in store.path_for(first.project_id, first.branch).name
    )


def test_manifest_rejects_extra_secret_fields(tmp_path: Path) -> None:
    payload = _manifest(tmp_path).model_dump(mode="json")
    payload["database_url"] = "postgres://secret"
    with pytest.raises(ValidationError):
        DeveloperWorktreeManifest.model_validate(payload)


def test_store_round_trip_update_and_delete(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    assert store.load(manifest.project_id, manifest.branch) == manifest
    updated = manifest.model_copy(
        update={"completed_checkpoints": ("manifest.created", "worktree.created")}
    )
    store.save(updated)
    assert store.load(manifest.project_id, manifest.branch) == updated
    store.delete(updated)
    assert not store.exists(manifest.project_id, manifest.branch)


def test_different_existing_manifest_is_not_overwritten(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    forged = manifest.model_copy(update={"base_sha": "b" * 40})
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.save(forged)
    assert store.load(manifest.project_id, manifest.branch) == manifest


def test_manifest_target_link_is_rejected(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    target = store.path_for(manifest.project_id, manifest.branch)
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("{}")
    try:
        target.symlink_to(outside)
    except OSError, NotImplementedError:
        pytest.skip("links unavailable")
    with pytest.raises(WorktreeManifestError, match="manifest operation failed"):
        store.load(manifest.project_id, manifest.branch)
    assert outside.read_text() == "{}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_manifest_is_owner_only_on_posix(tmp_path: Path) -> None:
    store = WorktreeManifestStore(tmp_path / "data")
    manifest = _manifest(tmp_path)
    store.create(manifest)
    assert store.path_for(manifest.project_id, manifest.branch).stat().st_mode & 0o777 == 0o600
