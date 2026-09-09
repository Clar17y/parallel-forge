"""Evaluation CLI commands for deterministic and live agent harnesses."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from forge.application.ports.provider_credentials import ProviderCredentialError
from forge.application.services.evaluations import EvaluationService
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.evaluations import EvaluationConflict
from forge.settings import Settings
from forge.tools.provider_credentials import LocalProviderCredentialResolver
from forge.tools.secrets import LocalSecretStore

eval_app = typer.Typer(add_completion=False, no_args_is_help=True)


def _parse_metric_bounds(entries: list[str] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    if not entries:
        return result
    for entry in entries:
        if "=" not in entry:
            raise typer.BadParameter(f"expected 'metric=value', got '{entry}'")
        k, v = entry.split("=", 1)
        try:
            result[k.strip()] = float(v.strip())
        except ValueError:
            raise typer.BadParameter(f"metric value must be a float: '{v}'")
    return result


@eval_app.command("run")
def run_evaluations(
    suite: Annotated[
        str,
        typer.Option("--suite", "-s", help="Evaluation suite ('deterministic' or 'live')"),
    ] = "deterministic",
    provider_reference: Annotated[
        str | None,
        typer.Option(
            "--provider-reference", help="Explicit provider secret reference for live runs"
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Explicit configured model identity for live runs"),
    ] = None,
    fixtures_dir: Annotated[
        Path | None,
        typer.Option("--fixtures-dir", help="Optional path to evaluation fixtures"),
    ] = None,
    expected_dir: Annotated[
        Path | None,
        typer.Option("--expected-dir", help="Optional path to expected outputs"),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option("--idempotency-key", help="Explicit idempotency key for this suite"),
    ] = None,
    metric_version: Annotated[
        str | None,
        typer.Option("--metric-version", help="Optional metric version override"),
    ] = None,
    baseline_fixture_version: Annotated[
        str | None,
        typer.Option("--baseline-fixture-version", help="Baseline fixture version"),
    ] = None,
    baseline_metric_version: Annotated[
        str | None,
        typer.Option("--baseline-metric-version", help="Baseline metric version"),
    ] = None,
    promoted_baseline: Annotated[
        bool,
        typer.Option("--promoted-baseline", help="Compare against persisted promoted baseline"),
    ] = False,
    baseline_name: Annotated[
        str | None,
        typer.Option("--baseline-name", help="Specific baseline name to compare against"),
    ] = None,
    baseline_id: Annotated[
        UUID | None,
        typer.Option("--baseline-id", help="Specific baseline ID to compare against"),
    ] = None,
) -> None:
    """Run deterministic or live evaluation suites against Forge agents."""
    if suite == "live" and not provider_reference:
        typer.echo("Error: live evaluation requires an explicit --provider-reference", err=True)
        raise typer.Exit(code=2)
    if suite == "live" and not model:
        typer.echo("Error: live evaluation requires an explicit --model", err=True)
        raise typer.Exit(code=2)

    has_baseline_selection = (
        promoted_baseline or baseline_name is not None or baseline_id is not None
    )

    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    async def _run() -> None:
        try:
            credential_resolver: LocalProviderCredentialResolver | None = None
            if suite == "live":
                secrets_dir = settings.data_root / "secrets"
                secrets_dir.mkdir(parents=True, exist_ok=True)
                secret_store = LocalSecretStore(settings.data_root)
                credential_resolver = LocalProviderCredentialResolver(secret_store)

            service = EvaluationService(
                session_factory=session_factory,
                credential_resolver=credential_resolver,
                fixtures_dir=fixtures_dir,
                expected_dir=expected_dir,
                settings=settings,
            )

            result = await service.run_suite(
                suite_name=suite,
                provider_reference=provider_reference,
                live_model=model,
                idempotency_key=idempotency_key,
                metric_version=metric_version,
                baseline_fixture_version=baseline_fixture_version,
                baseline_metric_version=baseline_metric_version,
                is_live=(suite == "live"),
                promoted_baseline=promoted_baseline,
                baseline_name=baseline_name,
                baseline_id=baseline_id,
            )

            typer.echo(f"Evaluation suite '{result.name}' ({result.suite_id}): {result.status.upper()}")
            for case in result.cases:
                typer.echo(f"  [{case.status.upper()}] {case.case_key} ({case.role.value})")
                for k, v in sorted(case.metrics.items()):
                    typer.echo(f"    {k}: {v}")
                if case.regression_failures:
                    typer.echo(f"    REGRESSIONS: {', '.join(case.regression_failures)}")

            if suite == "deterministic" and result.status != "passed":
                raise typer.Exit(code=1)
            if suite == "live" and has_baseline_selection and result.status != "passed":
                raise typer.Exit(code=1)

        except EvaluationConflict as exc:
            typer.echo(f"Evaluation conflict error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except ProviderCredentialError as exc:
            typer.echo(f"Provider credential error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await engine.dispose()

    asyncio.run(_run())


@eval_app.command("promote")
def promote_evaluation(
    suite_id: Annotated[UUID, typer.Option("--suite-id", help="Suite ID to promote as baseline")],
    name: Annotated[str, typer.Option("--name", help="Baseline name")] = "live",
    floor: Annotated[list[str] | None, typer.Option("--floor", help="Floor threshold ('metric=value')")] = None,
    ceiling: Annotated[list[str] | None, typer.Option("--ceiling", help="Ceiling threshold ('metric=value')")] = None,
    promoted_by: Annotated[str, typer.Option("--promoted-by", help="Operator identity")] = "operator",
) -> None:
    """Promote an already recorded valid evaluation suite as a durable baseline."""
    floors = _parse_metric_bounds(floor)
    ceilings = _parse_metric_bounds(ceiling)

    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    async def _promote() -> None:
        try:
            service = EvaluationService(
                session_factory=session_factory,
                settings=settings,
            )
            baseline = await service.promote_baseline(
                suite_id=suite_id,
                name=name,
                floors=floors,
                ceilings=ceilings,
                promoted_by=promoted_by,
            )
            typer.echo(
                f"Promoted baseline '{baseline.name}' ({baseline.id}) from suite {baseline.suite_id}"
            )
            typer.echo(f"  Fixture version: {baseline.fixture_version}")
            typer.echo(f"  Metric version:  {baseline.metric_version}")
            typer.echo(f"  Cases: {len(baseline.cases)}")
        except EvaluationConflict as exc:
            typer.echo(f"Baseline promotion error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            await engine.dispose()

    asyncio.run(_promote())


__all__ = ["eval_app", "promote_evaluation", "run_evaluations"]
