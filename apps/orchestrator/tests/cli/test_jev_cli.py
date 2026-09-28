from forge.cli import jev as jev_module
from forge.cli.main import app
from typer.testing import CliRunner


def test_jev_report_json_is_read_only_projection(monkeypatch):
    async def report(_run_id):
        return {"schema_version": 1, "run_id": "123", "requested_mode": "shadow",
                "effective_mode": "not_yet_observed", "requested_model": "jev-latest",
                "actual_model": None, "calls": 0, "attempts": 0, "cache_hits": 0, "unknown": 0,
                "actual_input_units": 0, "actual_output_units": 0, "reserved_input_units": 0,
                "duration_ms": 0, "remaining_requests": 64, "remaining_input_units": 250000,
                "by_kind": {}, "by_status": {}, "review_focus_available": False,
                "availability": "no_samples"}

    monkeypatch.setattr(jev_module, "_report", report)
    result = CliRunner().invoke(app, ["jev", "report", "00000000-0000-0000-0000-000000000123", "--json"])
    assert result.exit_code == 0
    assert '"requested_mode": "shadow"' in result.stdout
    assert '"effective_mode": "not_yet_observed"' in result.stdout
