"""Document the security dependencies actually enforced by each API route."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.security import CSRF_HEADER, SESSION_COOKIE


def _uses(dependant: Dependant, target: Callable[..., Any]) -> bool:
    return dependant.call is target or any(_uses(child, target) for child in dependant.dependencies)


def install_openapi(app: FastAPI) -> None:
    def schema() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        result = get_openapi(title=app.title, version=app.version, routes=app.routes)
        result.setdefault("components", {}).setdefault("securitySchemes", {}).update(
            {
                "OperatorSession": {"type": "apiKey", "in": "cookie", "name": SESSION_COOKIE},
                "CSRF": {"type": "apiKey", "in": "header", "name": CSRF_HEADER},
            }
        )
        for route in iter_route_contexts(app.routes):
            if not isinstance(route.original_route, APIRoute) or not route.include_in_schema:
                continue
            for method in route.methods or ():
                operation = result["paths"][route.path_format].get(method.lower())
                if operation is None:
                    continue
                if _uses(route.dependant, require_operator_mutation):
                    operation["security"] = [{"OperatorSession": [], "CSRF": []}]
                elif _uses(route.dependant, require_operator):
                    operation["security"] = [{"OperatorSession": []}]
                if _uses(route.dependant, require_idempotency_key):
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                        }
                    )
                if route.path_format == "/api/auth/bootstrap" and method == "POST":
                    operation["security"] = []
                    operation["x-forge-bootstrap-exchange"] = True
                    operation["description"] = (
                        "Exchange a single-use short-lived bootstrap token for a session. "
                        "Requires the configured Host and Origin; no prior session exists."
                    )
                if route.path_format == "/api/runs/{run_id}/events":
                    operation.setdefault("parameters", []).append(
                        {
                            "name": "Last-Event-ID",
                            "in": "header",
                            "required": False,
                            "description": "Takes precedence over after.",
                            "schema": {"type": "string", "pattern": "^[0-9]{1,19}$"},
                        }
                    )
                    operation["responses"]["200"]["content"] = {
                        "text/event-stream": {"schema": {"type": "string"}}
                    }
        app.openapi_schema = result
        return result

    app.openapi = schema  # type: ignore[method-assign]
