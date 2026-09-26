"""Profile service keeps actor, receipt, and immutable version rules together."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.mutations import ApiMutationRecord
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import ProfileBody, SubscriptionProfileService


class Work:
    def __init__(self):
        self.profiles = {}
        self.audit_rows = []
        self.committed = False
        self.mutations = SimpleNamespace(reserve=self.reserve, complete=self.complete)
        self.subscription = SimpleNamespace(
            store_profile=self.store, profile=self.profile, list_profiles=self.list_profiles
        )
        self.audit = SimpleNamespace(append=self.audit_append)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def commit(self):
        self.committed = True

    async def reserve(self, **kwargs):
        self.reservation = kwargs
        return ApiMutationRecord(
            id=uuid4(),
            actor_id=kwargs["actor_id"],
            action=kwargs["action"],
            scope=kwargs["scope"],
            key_hash="a" * 64,
            request_digest=kwargs["request_digest"],
            lifecycle_state="RESERVED",
            response_status=None,
            response_payload=None,
            resource_kind=None,
            resource_id=None,
        )

    async def complete(self, *args, **kwargs):
        self.completed = kwargs

    async def store(self, profile):
        self.profiles[(profile.profile_id, profile.version)] = profile
        return profile

    async def profile(self, profile_id, version):
        return self.profiles[(profile_id, version)]

    async def list_profiles(self):
        return list(self.profiles.values())

    async def audit_append(self, **kwargs):
        self.audit_rows.append(kwargs)


@pytest.mark.asyncio
async def test_create_controls_profile_identity_version_and_mapping_actor():
    work = Work()
    service = SubscriptionProfileService(lambda: work)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    request = ProfileBody.model_validate(
        {
            "preferences": [
                {
                    "purpose": "primary",
                    "preferred_route": {
                        "provider": "openai",
                        "client": "codex",
                        "model": "gpt-test",
                    },
                }
            ],
            "approved_mappings": [
                {"requested_model": "old", "effective_model": "new", "reason": "operator approval"}
            ],
        }
    )
    profile = await service.create(actor=actor, idempotency_key="create-1", request=request)
    assert profile.version == 1 and profile.approved_mappings[0].approved_by == str(actor.actor_id)
    assert work.reservation["action"] == "subscription.profile.create"
    assert work.completed["response_payload"]["profile_version"] == 1
    assert work.audit_rows[0]["payload"]["request_digest"] == work.reservation["request_digest"]
