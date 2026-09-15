from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"


def _load_workflow(path: Path) -> dict[str, object]:
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    return workflow


def _trigger_names(workflow: dict[str, object]) -> set[str]:
    triggers = workflow["on"]
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        assert all(isinstance(trigger, str) for trigger in triggers)
        return set(triggers)
    assert isinstance(triggers, dict)
    return set(triggers)


def test_pull_requests_run_only_one_bounded_smoke_job() -> None:
    workflow_paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))

    for path in workflow_paths:
        workflow = _load_workflow(path)
        expected = {"pull_request"} if path.name == "pr-smoke.yml" else {"workflow_dispatch"}
        assert _trigger_names(workflow) == expected, path.name

    smoke_workflow = _load_workflow(WORKFLOWS / "pr-smoke.yml")
    assert smoke_workflow["permissions"] == {"contents": "read"}
    concurrency = smoke_workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] == "true"
    jobs = smoke_workflow["jobs"]
    assert isinstance(jobs, dict)
    assert list(jobs) == ["smoke"]

    smoke = jobs["smoke"]
    assert isinstance(smoke, dict)
    assert smoke["runs-on"] == "ubuntu-24.04"
    assert int(smoke["timeout-minutes"]) <= 5
    assert "services" not in smoke
    assert "strategy" not in smoke

    steps = smoke["steps"]
    assert isinstance(steps, list)
    secret_scans = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Scan committed candidate for secrets"
    ]
    assert len(secret_scans) == 1
    scan_command = secret_scans[0]["run"]
    assert isinstance(scan_command, str)
    assert "gitleaks_8.30.1_linux_x64.tar.gz" in scan_command
    assert "sha256sum --check --strict" in scan_command
    assert "git archive HEAD" in scan_command
