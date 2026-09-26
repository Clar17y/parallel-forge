"""A release label may change; the historical interpreter's dependencies may not."""

import historical_v01
import pytest


def _environment(root, version):
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "parallel-forge"\nversion = "{version}"\n'
        'requires-python = ">=3.14,<3.15"\ndependencies = ["example==1.0"]\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "parallel-forge"\n'
        f'version = "{version}"\nsource = {{ editable = "." }}\n'
        '[[package]]\nname = "example"\nversion = "1.0"\n'
        'source = { registry = "https://example.test/simple" }\n'
        'wheels = [{ hash = "sha256:original" }]\n',
        encoding="utf-8",
    )
    return root


def test_historical_environment_allows_only_release_label_and_marker_changes(tmp_path):
    historical = _environment(tmp_path / "historical", "0.1.0")
    current = _environment(tmp_path / "current", "0.2.0")
    metadata = current / "pyproject.toml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8")
        + '\n[tool.pytest.ini_options]\nmarkers = ["docker: Docker fixtures"]\n',
        encoding="utf-8",
    )
    # Marker registration may vary, while the enclosing metadata stays identical.
    old_metadata = historical / "pyproject.toml"
    old_metadata.write_text(
        old_metadata.read_text(encoding="utf-8") + "\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )

    historical_v01._assert_matching_environment(historical, current)


@pytest.mark.parametrize(
    ("filename", "before", "after"),
    [
        ("pyproject.toml", "example==1.0", "example==2.0"),
        ("pyproject.toml", ">=3.14,<3.15", ">=3.15,<3.16"),
        ("uv.lock", 'version = "1.0"', 'version = "2.0"'),
        ("uv.lock", "sha256:original", "sha256:changed"),
        ("uv.lock", 'editable = "."', 'editable = "../other"'),
        ("uv.lock", 'version = "0.2.0"', 'version = "0.3.0"'),
    ],
)
def test_historical_environment_rejects_changed_runtime_inputs(tmp_path, filename, before, after):
    historical = _environment(tmp_path / "historical", "0.1.0")
    current = _environment(tmp_path / "current", "0.2.0")
    target = current / filename
    original = target.read_text(encoding="utf-8")
    assert before in original
    target.write_text(original.replace(before, after), encoding="utf-8")

    with pytest.raises(AssertionError):
        historical_v01._assert_matching_environment(historical, current)
