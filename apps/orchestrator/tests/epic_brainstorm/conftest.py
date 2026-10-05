"""Use the shared migration-owned, disposable PostgreSQL schema for brainstorming."""

import pytest


@pytest.fixture
def brainstorm_session_factory(session_factory):
    return session_factory
