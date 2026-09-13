"""Route boundary coverage for operator subscription profiles."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.domain.subscription import OperatorProfile, RolePreference, RouteSpec, SpecialistPurpose


def _profile() -> OperatorProfile:
    return OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(
                purpose=SpecialistPurpose.PRIMARY,
                preferred_route=RouteSpec(provider="openai", client="codex", model="gpt-test"),
            ),
        ),
    )


class FakeProfileService:
    def __init__(self) -> None:
        self.profile = _profile()
        self.calls: list[str] = []

    async def list(self):
        self.calls.append("list")
        return [self.profile]

    async def get(self, profile_id, version):
        self.calls.append("get")
        assert profile_id == self.profile.profile_id and version == 1
        return self.profile

    async def create(self, **kwargs):
        self.calls.append("create")
        return self.profile

    async def append(self, **kwargs):
        self.calls.append("append")
        return self.profile

    async def selected(self, project_id):
        self.calls.append("selected")

    async def select(self, **kwargs):
        self.calls.append("select")
        return self.profile


@pytest.mark.asyncio
async def test_profile_routes_enforce_operator_mutation_and_return_safe_profile(
    task10_client, task10_route_context, route_headers
) -> None:
    service = FakeProfileService()
    task10_route_context.app.state.subscription_profile_service = service
    payload = {
        "preferences": [
            {
                "purpose": "primary",
                "preferred_route": {"provider": "openai", "client": "codex", "model": "gpt-test"},
            }
        ]
    }
    missing = await task10_client.post(
        "/api/subscription-profiles", json=payload, headers=route_headers
    )
    assert missing.status_code == 422
    denied = await task10_client.post(
        "/api/subscription-profiles",
        json=payload,
        headers={**route_headers, "Idempotency-Key": "profile-1", "X-CSRF-Token": "wrong"},
    )
    assert denied.status_code == 403
    created = await task10_client.post(
        "/api/subscription-profiles",
        json=payload,
        headers={**route_headers, "Idempotency-Key": "profile-1"},
    )
    assert created.status_code == 201
    assert created.json()["preferences"][0]["preferred_route"]["model"] == "gpt-test"
    assert service.calls == ["create"]


@pytest.mark.asyncio
async def test_profile_routes_reject_unknown_body_and_expose_explicit_missing_selection(
    task10_client, task10_route_context, route_headers
) -> None:
    service = FakeProfileService()
    task10_route_context.app.state.subscription_profile_service = service
    invalid = await task10_client.post(
        "/api/subscription-profiles",
        json={"preferences": [], "provider_authority": "forged"},
        headers={**route_headers, "Idempotency-Key": "profile-invalid"},
    )
    assert invalid.status_code == 422
    selected = await task10_client.get(
        f"/api/projects/{uuid4()}/subscription-profile", headers={"Host": route_headers["Host"]}
    )
    assert selected.status_code == 200 and selected.json() is None


@pytest.mark.asyncio
async def test_profile_selection_rejects_partial_expected_identity(
    task10_client, task10_route_context, route_headers
) -> None:
    service = FakeProfileService()
    task10_route_context.app.state.subscription_profile_service = service
    response = await task10_client.put(
        f"/api/projects/{uuid4()}/subscription-profile",
        headers={**route_headers, "Idempotency-Key": "selection-partial"},
        json={
            "profile_id": str(service.profile.profile_id),
            "profile_version": 1,
            "expected_profile_id": str(uuid4()),
        },
    )
    assert response.status_code == 422
    assert service.calls == []
