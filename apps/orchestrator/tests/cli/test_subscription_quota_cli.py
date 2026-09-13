from datetime import UTC, datetime
from uuid import uuid4

from forge.cli import subscription_quota as cli
from forge.cli.main import app
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey
from typer.testing import CliRunner


class _Engine:
    disposed = False

    async def dispose(self):
        self.disposed = True


class _Service:
    def __init__(self):
        self.kwargs = None

    async def report_exhaustion(self, **kwargs):
        self.kwargs = kwargs
        return PoolQuotaStatus(
            key=QuotaPoolKey(provider="openai", account="team-a", pool="allowance"),
            revision=2,
            status="blocked",
            observed_at=datetime(2026, 9, 12, tzinfo=UTC),
            reason="operator_report",
            reset_at=None,
            next_eligible_at=None,
            retry_basis=None,
        )


def test_quota_help_and_report_use_explicit_identity_and_local_actor(monkeypatch):
    assert CliRunner().invoke(app, ["subscription-quota", "--help"]).exit_code == 0
    service, engine = _Service(), _Engine()
    monkeypatch.setattr(cli, "_service", lambda: (service, engine))
    result = CliRunner().invoke(
        app,
        [
            "subscription-quota", "report-exhaustion", "--provider", "openai",
            "--account", "team-a", "--pool", "allowance", "--reason", "monthly limit",
            "--reset-at", "2026-09-12T13:00:00+00:00",
            "--idempotency-key", "quota-cli-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert service.kwargs["idempotency_key"] == "quota-cli-1"
    assert service.kwargs["request"].reset_at == datetime(2026, 9, 12, 13, tzinfo=UTC)
    assert service.kwargs["actor"].actor_id == cli.LocalOperatorProfileActor().actor_id
    assert engine.disposed


def test_cli_jsonable_handles_probe_uuid() -> None:
    value = uuid4()
    assert cli._jsonable(value) == str(value)
