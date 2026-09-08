"""Self-contained API documentation with pinned local Swagger UI assets."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


def install_docs(app: FastAPI) -> None:
    app.docs_url = "/api/docs"
    app.mount(
        "/api/docs-assets",
        StaticFiles(directory=Path(__file__).parent / "static" / "swagger"),
        name="docs-assets",
    )

    @app.get(app.docs_url, include_in_schema=False)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title="Forge API",
            swagger_js_url="/api/docs-assets/swagger-ui-bundle.js",
            swagger_css_url="/api/docs-assets/swagger-ui.css",
            swagger_favicon_url="/api/docs-assets/favicon-32x32.png",
            swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False},
        )
