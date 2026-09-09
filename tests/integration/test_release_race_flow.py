"""Process-level release race acceptance scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.integration.test_vertical_slice import run_vertical_slice_process_acceptance

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.asyncio
async def test_head_advance_between_preflight_and_merge_rechecks_approval(
    tmp_path: Path, migrated_database_url: str
) -> None:
    await run_vertical_slice_process_acceptance(
        tmp_path, migrated_database_url, race_mode="head"
    )


@pytest.mark.asyncio
async def test_strict_base_advance_revalidates_and_requires_new_approval(
    tmp_path: Path, migrated_database_url: str
) -> None:
    await run_vertical_slice_process_acceptance(
        tmp_path, migrated_database_url, race_mode="base"
    )


def test_release_harness_keeps_loopback_boundary(tmp_path: Path) -> None:
    """Harness construction owns temporary fixture data and loopback only."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    harness = ForgeProcessHarness(
        database_url="postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge_test_" + "a" * 32,
        data_root=data_root,
        prompt_root=Path.cwd() / "agents",
    )
    try:
        assert harness.base_url.startswith("http://127.0.0.1:")
        assert harness.fake_github_url.startswith("http://127.0.0.1:")
        assert (data_root / "acceptance-pricing.json").exists()
    finally:
        harness.close()
