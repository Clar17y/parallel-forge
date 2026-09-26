"""Freeze selected routing independently of provider capability verification."""

from uuid import UUID

from forge.domain.subscription import (
    ExecutionEnvelope,
    OperatorProfile,
    RouteBinding,
    SpecialistPurpose,
)


def freeze_profile(
    profile: OperatorProfile, *, run_id: UUID, safety_policy_version: int
) -> ExecutionEnvelope:
    try:
        profile.preference_for(SpecialistPurpose.PRIMARY)
    except KeyError:
        raise ValueError("subscription profile requires a primary preference") from None
    return ExecutionEnvelope(
        run_id=run_id,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        safety_policy_version=safety_policy_version,
        billing_mode=profile.default_billing_mode,
        routes=tuple(
            (
                preference.purpose,
                RouteBinding(
                    requested=preference.preferred_route,
                    effective=preference.preferred_route,
                    is_primary=preference.purpose is SpecialistPurpose.PRIMARY,
                ),
            )
            for preference in profile.preferences
        ),
        allowed_fallbacks=tuple(
            (preference.purpose, preference.fallback_routes)
            for preference in profile.preferences
            if preference.fallback_routes
        ),
    )
