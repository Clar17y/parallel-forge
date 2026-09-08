"""The generated API contract matches the enforced operator boundaries."""

from forge.api.app import create_app


def test_api_docs_and_openapi_live_under_api_prefix():
    app = create_app()
    assert app.openapi_url == "/api/openapi.json"
    assert app.docs_url == "/api/docs"


def test_mutations_declare_session_and_csrf_with_explicit_bootstrap_exception():
    schema = create_app().openapi()
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"post", "patch", "delete"}:
                continue
            if path == "/api/auth/bootstrap":
                assert operation.get("x-forge-bootstrap-exchange") is True
                assert operation.get("security", []) == []
            else:
                assert operation["security"] == [{"OperatorSession": [], "CSRF": []}]
    command = schema["paths"]["/api/runs/{run_id}/commands"]["post"]
    assert any(
        parameter["in"] == "header"
        and parameter["name"] == "Idempotency-Key"
        and parameter["required"]
        for parameter in command["parameters"]
    )


def test_download_contract_is_binary_and_events_are_streamed():
    paths = create_app().openapi()["paths"]
    download = paths["/api/artifacts/{digest}/download"]["get"]["responses"]["200"]
    assert download["content"] == {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }
    events = paths["/api/runs/{run_id}/events"]["get"]
    assert events["security"] == [{"OperatorSession": []}]
    assert set(events["responses"]["200"]["content"]) == {"text/event-stream"}


def test_generated_schema_matches_tracked_contract():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/export-openapi.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_api_inputs_do_not_expose_runtime_command_or_credential_channels():
    schema = create_app().openapi()
    forbidden = {
        "shell",
        "shelltext",
        "commandtext",
        "githubtoken",
        "modelcredentials",
        "apikey",
        "password",
        "hiddenreasoning",
        "chainofthought",
        "storagepointer",
    }
    project_only = {
        "argv",
        "repositorypath",
        "allowed environment files".replace(" ", ""),
        "secretpaths",
        "adminsecretreference",
    }

    def names(node, seen):
        if not isinstance(node, dict):
            return set()
        found = set(node.get("properties", {}))
        reference = node.get("$ref")
        if reference and reference not in seen:
            target = schema
            for part in reference.removeprefix("#/").split("/"):
                target = target[part]
            found |= names(target, seen | {reference})
        for value in node.values():
            if isinstance(value, dict):
                found |= names(value, seen)
            elif isinstance(value, list):
                for item in value:
                    found |= names(item, seen)
        return found

    for path, methods in schema["paths"].items():
        for operation in methods.values():
            fields = names(operation.get("requestBody", {}), set())
            fields.update(p["name"] for p in operation.get("parameters", []))
            normalized = {field.lower().replace("_", "").replace("-", "") for field in fields}
            assert not normalized & forbidden, (path, normalized & forbidden)
            if not path.startswith("/api/projects"):
                assert not normalized & project_only, (path, normalized & project_only)


async def test_docs_use_local_assets_without_external_validator():
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:3000"
    ) as client:
        response = await client.get("/api/docs")
        assert response.status_code == 200
        assert "https://" not in response.text and "http://" not in response.text
        assert '"validatorUrl": null' in response.text
        for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png"):
            asset_response = await client.get(f"/api/docs-assets/{asset}")
            assert asset_response.status_code == 200 and asset_response.content
