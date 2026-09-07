import pytest
from forge.cli.main import app
from forge.settings import Settings
from typer.testing import CliRunner


def test_non_loopback_bind_requires_explicit_remote_mode() -> None:
    with pytest.raises(ValueError, match="remote exposure requires a later authentication design"):
        Settings(bind_host="0.0.0.0", allow_remote=False)


def test_cli_status_entrypoint_is_executable() -> None:
    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert result.stdout == "Forge CLI is ready.\n"


def test_pricing_and_prompt_settings_default_to_none() -> None:
    settings = Settings()
    assert settings.pricing_catalog_path is None
    assert settings.prompt_root is None


def test_pricing_and_prompt_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_PRICING_CATALOG_PATH", "/tmp/pricing.json")
    monkeypatch.setenv("FORGE_PROMPT_ROOT", "/tmp/prompts")
    settings = Settings()
    from pathlib import Path

    assert settings.pricing_catalog_path == Path("/tmp/pricing.json")
    assert settings.prompt_root == Path("/tmp/prompts")


def test_effective_provider_secret_reference_precedence() -> None:
    # Default is empty string
    assert Settings().effective_provider_secret_reference == ""

    # Provider reference alone
    settings_generic = Settings(provider_secret_reference="secret://forge/generic-key")
    assert settings_generic.effective_provider_secret_reference == "secret://forge/generic-key"

    # Google reference alone
    settings_google = Settings(google_api_key_reference="secret://forge/google-key")
    assert settings_google.effective_provider_secret_reference == "secret://forge/google-key"

    # Both matching
    settings_both = Settings(
        provider_secret_reference="secret://forge/same-key",
        google_api_key_reference="secret://forge/same-key",
    )
    assert settings_both.effective_provider_secret_reference == "secret://forge/same-key"

    # Both conflicting raises ValueError
    with pytest.raises(ValueError, match="generic and Google provider references conflict"):
        Settings(
            provider_secret_reference="secret://forge/key-a",
            google_api_key_reference="secret://forge/key-b",
        )


def test_provider_secret_reference_validation() -> None:
    from forge.application.ports.provider_credentials import ProviderCredentialError

    for bad_ref in ("not-a-secret", "http://example.com/key", "env://API_KEY", "file:///key"):
        with pytest.raises(ProviderCredentialError):
            Settings(provider_secret_reference=bad_ref)
        with pytest.raises(ProviderCredentialError):
            Settings(google_api_key_reference=bad_ref)


def test_provider_secret_reference_not_in_repr() -> None:
    secret_ref = "secret://forge/sensitive-secret-token"
    settings = Settings(provider_secret_reference=secret_ref)
    assert secret_ref not in repr(settings)


def test_settings_ignores_raw_provider_environment_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "raw-secret-key")
    monkeypatch.setenv("GEMINI_API_KEY", "raw-secret-key")
    settings = Settings()
    assert settings.provider_secret_reference == ""
    assert settings.google_api_key_reference == ""
    assert settings.effective_provider_secret_reference == ""
