"""Runtime inspection is bounded, read-only and credential-free on failure."""

from datetime import UTC, datetime

from forge.api.schemas.subscription_runtime import (
    EvidenceReferenceView,
    SubscriptionRuntimeRouteView,
    SubscriptionRuntimeStatusPage,
    SubscriptionWorkerStatusView,
)
from forge.cli import subscription_runtime as cli
from forge.cli.main import app
from typer.testing import CliRunner


def test_runtime_cli_prints_unknown_inventory_and_forwards_bounds(monkeypatch):
    calls = []

    async def query(offset, limit):
        calls.append((offset, limit))
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 12, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(
        app, ["subscription-runtime", "status", "--offset", "2", "--limit", "10"]
    )
    assert result.exit_code == 0 and '"workers":[]' in result.output
    assert calls == [(2, 10)]
    assert (
        CliRunner().invoke(app, ["subscription-runtime", "status", "--limit", "101"]).exit_code != 0
    )
    assert len(calls) == 1


def test_runtime_cli_failure_does_not_echo_connection_values(monkeypatch):
    async def query(*_args):
        raise ValueError("password=fixture-secret")

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status"])
    assert result.exit_code == 1 and "unavailable" in result.output
    assert "fixture-secret" not in result.output


def test_runtime_cli_text_output_is_actionable_and_preserves_unknown_quota(monkeypatch):
    async def query(_offset, _limit):
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[
                SubscriptionWorkerStatusView(
                    worker_instance_id="11111111-1111-4111-8111-111111111111",
                    last_seen_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                    stopped_at=None,
                    state="current",
                    routes=[
                        SubscriptionRuntimeRouteView(
                            schema_version=2,
                            provider="anthropic",
                            client="claude_code",
                            model="claude-opus-5",
                            effort="medium",
                            auth_mode="subscription",
                            billing_mode="allowance_only",
                            configured=True,
                            admitted=False,
                            reason="signed_out",
                            effective_reason="signed_out",
                            quota="unknown",
                            evidence=[
                                EvidenceReferenceView(
                                    scope="opus-review",
                                    evidence_id="22222222-2222-4222-8222-222222222222",
                                    revision=4,
                                    observed_at=datetime(2026, 9, 12, 19, tzinfo=UTC),
                                    expires_at=datetime(2026, 9, 12, 21, tzinfo=UTC),
                                )
                            ],
                        )
                    ],
                )
            ],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status", "--format", "text"])
    assert result.exit_code == 0
    assert "anthropic / claude_code / claude-opus-5" in result.output
    assert "configured=yes admitted=no" in result.output
    assert "claude auth login" in result.output
    assert "claude auth status" in result.output
    assert "quota=unknown" in result.output
    assert "not a zero-balance or availability claim" in result.output
    assert "opus-review" in result.output and "revision 4" in result.output
    assert "API key" not in result.output


def test_runtime_cli_text_output_never_turns_stale_or_blocked_into_ready(monkeypatch):
    async def query(_offset, _limit):
        route = SubscriptionRuntimeRouteView(
            schema_version=2,
            provider="openai",
            client="codex_app_server",
            model="gpt-6-astra",
            effort="medium",
            auth_mode="subscription",
            billing_mode="allowance_only",
            configured=True,
            admitted=True,
            reason="ready",
            effective_reason="quota_exhausted",
            quota="blocked",
            evidence=[],
            quota_revision=2,
            quota_reset_at=datetime(2026, 9, 13, 1, tzinfo=UTC),
        )
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[
                SubscriptionWorkerStatusView(
                    worker_instance_id="11111111-1111-4111-8111-111111111111",
                    last_seen_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                    stopped_at=None,
                    state="stale",
                    routes=[route.model_copy(update={"effective_reason": "stale_worker"})],
                )
            ],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status", "--format", "text"])
    assert result.exit_code == 0
    assert "state=stale_worker" in result.output
    assert "Capability ready" not in result.output


def test_runtime_cli_missing_evidence_points_to_offline_verification(monkeypatch):
    async def query(_offset, _limit):
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[
                SubscriptionWorkerStatusView(
                    worker_instance_id="11111111-1111-4111-8111-111111111111",
                    last_seen_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
                    stopped_at=None,
                    state="current",
                    routes=[
                        SubscriptionRuntimeRouteView(
                            schema_version=2,
                            provider="openai",
                            client="codex_app_server",
                            model="gpt-6-astra",
                            effort="low",
                            auth_mode="subscription",
                            billing_mode="allowance_only",
                            configured=True,
                            admitted=False,
                            reason="evidence_missing",
                            effective_reason="evidence_missing",
                            quota="unknown",
                            evidence=[],
                        )
                    ],
                )
            ],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status", "--format", "text"])
    assert result.exit_code == 0
    assert "subscription-capabilities status and verify offline" in result.output
    assert "API key" not in result.output


def test_runtime_cli_warns_that_antigravity_tools_are_not_guaranteed(monkeypatch):
    async def query(_offset, _limit):
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 22, 8, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[
                SubscriptionWorkerStatusView(
                    worker_instance_id="11111111-1111-4111-8111-111111111111",
                    last_seen_at=datetime(2026, 9, 22, 8, tzinfo=UTC),
                    stopped_at=None,
                    state="current",
                    routes=[
                        SubscriptionRuntimeRouteView(
                            schema_version=2,
                            provider="google",
                            client="gemini_cli",
                            model="gemini-3.8-flash",
                            effort="medium",
                            auth_mode="subscription",
                            billing_mode="allowance_only",
                            configured=True,
                            admitted=True,
                            reason="evidence_missing",
                            effective_reason="evidence_missing",
                            quota="unknown",
                            evidence=[],
                            warnings=["approved_tools_unproved"],
                        )
                    ],
                )
            ],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status", "--format", "text"])

    assert result.exit_code == 0
    assert "WARNING:" in result.output
    assert (
        "cannot guarantee that Antigravity limits itself to Forge-approved tools" in result.output
    )

    json_result = CliRunner().invoke(app, ["subscription-runtime", "status"])
    assert json_result.exit_code == 0
    assert '"warnings":["approved_tools_unproved"]' in json_result.output
