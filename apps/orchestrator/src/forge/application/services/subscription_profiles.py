"""Operator-managed immutable subscription profiles."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Self
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.subscription import SubscriptionRepository
from forge.application.services.auth import AuthenticatedActor
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ModelMapping,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
)


class SubscriptionProfileServiceError(RuntimeError):
    pass


_LOCAL_OPERATOR_ACTOR_ID = uuid5(
    UUID("6b5d3f2c-7e2a-4f0e-9d0a-2d1f3a6b8c4e"), "local-cli-operator"
)


@dataclass(frozen=True, slots=True)
class LocalOperatorProfileActor:
    """Stable privileged CLI identity without a fabricated browser session."""

    actor_id: UUID = _LOCAL_OPERATOR_ACTOR_ID
    actor_class: Literal["operator"] = "operator"


ProfileActor = AuthenticatedActor | LocalOperatorProfileActor


class ProfileUnitOfWork(Protocol):
    subscription: SubscriptionRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


class RouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=96)
    client: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=255)
    effort: ReasoningEffort = ReasoningEffort.LOW
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY

    def route(self) -> RouteSpec:
        return RouteSpec(**self.model_dump())


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: SpecialistPurpose
    preferred_route: RouteInput
    fallback_routes: tuple[RouteInput, ...] = Field(default=(), max_length=16)

    def preference(self) -> RolePreference:
        return RolePreference(
            purpose=self.purpose,
            preferred_route=self.preferred_route.route(),
            fallback_routes=tuple(route.route() for route in self.fallback_routes),
        )


class MappingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_model: str = Field(min_length=1, max_length=255)
    effective_model: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=1000)


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferences: tuple[PreferenceInput, ...] = Field(min_length=1, max_length=16)
    approved_mappings: tuple[MappingInput, ...] = Field(default=(), max_length=64)
    default_billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY


class ProfileVersionRequest(ProfileBody):
    expected_current_version: int = Field(ge=1)


class ProjectProfileSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID
    profile_version: int = Field(ge=1)
    expected_profile_id: UUID | None = None
    expected_profile_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def expected_identity_is_complete(self) -> ProjectProfileSelectionRequest:
        if (self.expected_profile_id is None) != (self.expected_profile_version is None):
            raise ValueError("expected profile identity is incomplete")
        return self


class SubscriptionProfileService:
    def __init__(self, unit_of_work_factory: Callable[[], ProfileUnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def create(
        self, *, actor: ProfileActor, idempotency_key: str, request: ProfileBody
    ) -> OperatorProfile:
        body = _coerce(request, ProfileBody)
        digest = _digest(body.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="subscription.profile.create",
                scope="profiles",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                profile = await work.subscription.profile(
                    _resource_id(receipt, "subscription_profile"),
                    _version(receipt, "profile_version"),
                )
                await work.commit()
                return profile
            profile = _profile(body, profile_id=uuid4(), version=1, actor=actor)
            await work.subscription.store_profile(profile)
            await self._complete(
                work, receipt, actor, profile, "subscription.profile_created", digest
            )
            await work.commit()
            return profile

    async def append(
        self,
        *,
        actor: ProfileActor,
        profile_id: UUID,
        idempotency_key: str,
        request: ProfileVersionRequest,
    ) -> OperatorProfile:
        body = _coerce(request, ProfileVersionRequest)
        digest = _digest({"profile_id": str(profile_id), "request": body.model_dump(mode="json")})
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="subscription.profile.append",
                scope=f"profile:{profile_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                result = await work.subscription.profile(
                    profile_id, _version(receipt, "profile_version")
                )
                await work.commit()
                return result
            profile = _profile(
                body, profile_id=profile_id, version=body.expected_current_version + 1, actor=actor
            )
            await work.subscription.append_profile(
                profile, expected_current_version=body.expected_current_version
            )
            await self._complete(
                work, receipt, actor, profile, "subscription.profile_version_appended", digest
            )
            await work.commit()
            return profile

    async def select(
        self,
        *,
        actor: ProfileActor,
        project_id: UUID,
        idempotency_key: str,
        request: ProjectProfileSelectionRequest,
    ) -> OperatorProfile:
        body = _coerce(request, ProjectProfileSelectionRequest)
        digest = _digest({"project_id": str(project_id), "request": body.model_dump(mode="json")})
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="subscription.project_profile.select",
                scope=f"project:{project_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                result = await work.subscription.profile(
                    _resource_id(receipt, "project_subscription_profile"),
                    _version(receipt, "profile_version"),
                )
                await work.commit()
                return result
            profile = await work.subscription.profile(body.profile_id, body.profile_version)
            await work.subscription.select_project_profile_expected(
                project_id,
                profile,
                expected_profile_id=body.expected_profile_id,
                expected_profile_version=body.expected_profile_version,
            )
            await self._complete(
                work,
                receipt,
                actor,
                profile,
                "subscription.project_profile_selected",
                digest,
                subject_id=project_id,
                resource_kind="project_subscription_profile",
            )
            await work.commit()
            return profile

    async def list(self) -> Sequence[OperatorProfile]:
        async with self._unit_of_work_factory() as work:
            rows = await work.subscription.list_profiles()
            await work.commit()
            return rows

    async def get(self, profile_id: UUID, version: int) -> OperatorProfile:
        async with self._unit_of_work_factory() as work:
            row = await work.subscription.profile(profile_id, version)
            await work.commit()
            return row

    async def selected(self, project_id: UUID) -> OperatorProfile | None:
        async with self._unit_of_work_factory() as work:
            row = await work.subscription.project_profile(project_id)
            await work.commit()
            return row

    async def _complete(
        self,
        work: ProfileUnitOfWork,
        receipt: ApiMutationRecord,
        actor: ProfileActor,
        profile: OperatorProfile,
        event: str,
        digest: str,
        *,
        subject_id: UUID | None = None,
        resource_kind: str = "subscription_profile",
    ) -> None:
        await work.audit.append(
            actor_id=actor.actor_id,
            event_type=event,
            subject_type=resource_kind,
            subject_id=subject_id or profile.profile_id,
            correlation_id=receipt.id,
            payload={
                "request_digest": digest,
                "profile_id": str(profile.profile_id),
                "profile_version": profile.version,
                "source": _actor_source(actor),
            },
        )
        await work.mutations.complete(
            receipt.id,
            response_status=201,
            response_payload={
                "profile_id": str(profile.profile_id),
                "profile_version": profile.version,
            },
            resource_kind=resource_kind,
            resource_id=profile.profile_id,
        )


def _profile(
    body: ProfileBody, *, profile_id: UUID, version: int, actor: ProfileActor
) -> OperatorProfile:
    preferences = tuple(value.preference() for value in body.preferences)
    if not any(value.purpose is SpecialistPurpose.PRIMARY for value in preferences):
        raise ValueError("operator profile requires a primary preference")
    return OperatorProfile(
        profile_id=profile_id,
        version=version,
        preferences=preferences,
        approved_mappings=tuple(
            ModelMapping(
                requested_model=value.requested_model,
                effective_model=value.effective_model,
                approved_by=str(actor.actor_id),
                reason=value.reason,
            )
            for value in body.approved_mappings
        ),
        default_billing_mode=body.default_billing_mode,
    )


def _coerce(value: object, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("request must be a validated profile request")


def _resource_id(receipt: ApiMutationRecord, kind: str) -> UUID:
    if receipt.resource_kind != kind or receipt.resource_id is None:
        raise SubscriptionProfileServiceError("mutation receipt resource is unavailable")
    return receipt.resource_id


def _version(receipt: ApiMutationRecord, name: str) -> int:
    value = None if receipt.response_payload is None else receipt.response_payload.get(name)
    if type(value) is not int or value < 1:
        raise SubscriptionProfileServiceError("mutation receipt response is unavailable")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _actor_source(actor: ProfileActor) -> str:
    return "local_cli" if isinstance(actor, LocalOperatorProfileActor) else "web_session"


__all__ = [
    "LocalOperatorProfileActor",
    "PreferenceInput",
    "ProfileBody",
    "ProfileVersionRequest",
    "ProjectProfileSelectionRequest",
    "RouteInput",
    "SubscriptionProfileService",
    "SubscriptionProfileServiceError",
]
