"""Share the disposable PostgreSQL fixtures across all orchestrator suites."""

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
