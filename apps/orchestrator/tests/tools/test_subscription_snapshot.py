"""Real managed Git snapshots include dirty bytes without changing the index."""

from pathlib import Path

from test_git import _controlled, _managed_repository, _registration_for


def test_snapshot_change_set_respects_checkout_line_endings(tmp_path):
    import hashlib
    from dataclasses import replace

    from test_git import _git

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    (handle.path / ".gitattributes").write_bytes(b"README.md text eol=crlf\n")
    readme = handle.path / "README.md"
    readme.write_bytes(b"forge\n")
    _git(handle.path, "add", ".gitattributes", "README.md")
    _git(handle.path, "commit", "-m", "normalize baseline")
    handle = replace(handle, base_sha=controlled.head_sha(handle))
    content = readme.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    readme.write_bytes(content)
    index = _registration_for(handle.path) / "index"
    before = index.read_bytes()
    snapshot = controlled.working_tree_snapshot(handle, secret_paths=())
    assert snapshot.changed_paths == ()
    assert (
        next(item for item in snapshot.files if item.path == "README.md").content_digest
        == hashlib.sha256(content).hexdigest()
    )
    assert index.read_bytes() == before


def test_dirty_working_tree_snapshot_is_complete_stable_and_read_only(tmp_path: Path):
    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    index = _registration_for(handle.path) / "index"
    before = index.read_bytes()
    (handle.path / "README.md").write_bytes(b"edited\n")
    (handle.path / "new.bin").write_bytes(bytes(range(256)))
    snapshot = controlled.working_tree_snapshot(handle, secret_paths=())
    assert snapshot == controlled.working_tree_snapshot(handle, secret_paths=())
    assert snapshot.head_sha == handle.base_sha
    assert snapshot.changed_paths == ("README.md", "new.bin")
    assert {item.path for item in snapshot.files} == {"README.md", "new.bin"}
    assert len(snapshot.candidate_tree_digest) == 64
    assert index.read_bytes() == before
    (handle.path / "new.bin").write_bytes(b"changed")
    assert (
        controlled.working_tree_snapshot(handle, secret_paths=()).candidate_tree_digest
        != snapshot.candidate_tree_digest
    )


def test_snapshot_detects_deletion_rename_and_mode_but_ignores_ignored_files(tmp_path):
    import os

    from test_git import _git

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    (handle.path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (handle.path / "ignored").mkdir()
    ignored = handle.path / "ignored" / "noise"
    ignored.write_bytes(b"first")
    first = controlled.working_tree_snapshot(handle, secret_paths=())
    ignored.write_bytes(b"second")
    assert controlled.working_tree_snapshot(handle, secret_paths=()) == first
    if os.name == "nt":
        _git(handle.path, "update-index", "--chmod=+x", "README.md")
    else:
        (handle.path / "README.md").chmod(0o755)
    mode = controlled.working_tree_snapshot(handle, secret_paths=())
    assert mode.candidate_tree_digest != first.candidate_tree_digest
    (handle.path / "README.md").rename(handle.path / "renamed.md")
    renamed = controlled.working_tree_snapshot(handle, secret_paths=())
    assert renamed.changed_paths == (".gitignore", "README.md", "renamed.md")
    assert "README.md" not in {item.path for item in renamed.files}
    assert renamed.candidate_tree_digest != mode.candidate_tree_digest


def test_snapshot_rejects_secret_paths_and_hardlink_aliases(tmp_path):
    import os

    import pytest
    from forge.tools.git import ControlledGitError

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    with pytest.raises(ControlledGitError) as error:
        controlled.working_tree_snapshot(handle, secret_paths=("README.md",))
    assert error.value.reason.value == "path_rejected"
    assert "README" not in str(error.value)
    os.link(handle.path / "README.md", handle.path / "alias")
    with pytest.raises(ControlledGitError) as error:
        controlled.working_tree_snapshot(handle, secret_paths=())
    assert error.value.reason.value == "path_rejected"


def test_snapshot_rejects_change_between_reads(tmp_path, monkeypatch):
    import pytest
    from forge.tools.git import ControlledGitError

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    original = controlled._run
    reads = 0

    def changing(worktree, arguments, **kwargs):
        nonlocal reads
        if arguments[0] == "ls-tree":
            reads += 1
            if reads == 2:
                (handle.path / "README.md").write_bytes(b"raced")
        return original(worktree, arguments, **kwargs)

    monkeypatch.setattr(controlled, "_run", changing)
    with pytest.raises(ControlledGitError) as error:
        controlled.working_tree_snapshot(handle, secret_paths=())
    assert error.value.reason.value == "tree_changed"


def test_snapshot_rejects_truncated_listing_and_oversized_file(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest
    from forge.tools.git import ControlledGitError

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    original = controlled._run

    def truncated(worktree, arguments, **kwargs):
        result = original(worktree, arguments, **kwargs)
        return replace(result, stdout_truncated=True) if arguments[0] == "ls-files" else result

    with monkeypatch.context() as patch:
        patch.setattr(controlled, "_run", truncated)
        with pytest.raises(ControlledGitError) as error:
            controlled.working_tree_snapshot(handle, secret_paths=())
        assert error.value.reason.value == "listing_invalid"
    (handle.path / "large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ControlledGitError) as error:
        controlled.working_tree_snapshot(handle, secret_paths=())
    assert error.value.reason.value == "size_limit"


def test_snapshot_rejects_symlink_to_outside_content(tmp_path):
    import os

    import pytest
    from forge.tools.git import ControlledGitError

    repository, _, handle = _managed_repository(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside boundary")
    try:
        os.symlink(outside, handle.path / "outside-link")
    except OSError:
        pytest.skip("host does not permit symlink creation")
    controlled = _controlled(repository, tmp_path / "state")
    with pytest.raises(ControlledGitError):
        controlled.working_tree_snapshot(handle, secret_paths=())


def test_snapshot_reports_unmerged_index_without_path_or_git_output(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest
    from forge.tools.git import ControlledGitError

    repository, _, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    original = controlled._run

    def unmerged(worktree, arguments, **kwargs):
        result = original(worktree, arguments, **kwargs)
        if arguments[:2] == ("ls-files", "--stage"):
            return replace(result, stdout=result.stdout.replace(" 0\t", " 1\t"))
        return result

    monkeypatch.setattr(controlled, "_run", unmerged)
    with pytest.raises(ControlledGitError) as error:
        controlled.working_tree_snapshot(handle, secret_paths=())
    assert error.value.reason.value == "index_conflict"
    assert "README" not in str(error.value)
