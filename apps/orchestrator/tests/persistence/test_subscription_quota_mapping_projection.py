"""Status distinguishes a frozen preferred model mapping from quota fallback."""

from uuid import uuid4

import pytest
from forge.domain.subscription import (
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    RolePreference,
    RouteBinding,
    RouteMapping,
    SpecialistPurpose,
    TaskBudget,
)
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401


@pytest.mark.integration
async def test_only_departure_from_frozen_preferred_route_is_labelled_fallback(
    session_factory, persisted_run
):
    purpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION
    requested, preferred, fallback = _route("requested"), _route("preferred"), _route("fallback")
    primary_route = RouteBinding(requested=preferred, effective=preferred, is_primary=True)
    primary_id = uuid4()
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=preferred),
            RolePreference(purpose=purpose, preferred_route=requested, fallback_routes=(fallback,)),
        ),
    )

    def binding(effective):
        return RouteBinding(
            requested=requested,
            effective=effective,
            mapping_applied=RouteMapping(
                requested=requested,
                effective=effective,
                approved_by="operator",
                approval_id="approved-profile",
                reason="Explicit route mapping",
            ),
        )

    task_ids = []
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.store_profile(profile)
        await work.subscription.freeze_envelope(
            ExecutionEnvelope(
                run_id=persisted_run.id,
                profile_id=profile.profile_id,
                profile_version=1,
                safety_policy_version=1,
                routes=(
                    (SpecialistPurpose.PRIMARY, primary_route),
                    (purpose, binding(preferred)),
                ),
                allowed_fallbacks=((purpose, (fallback,)),),
            )
        )
        await work.subscription.create_task(
            LogicalTaskContract(
                run_id=persisted_run.id,
                task_id=primary_id,
                purpose=SpecialistPurpose.PRIMARY,
                route=primary_route,
                budget=TaskBudget(),
            ),
            idempotency_key=str(primary_id),
        )
        for route in (preferred, fallback):
            task_id = uuid4()
            task_ids.append(task_id)
            await work.subscription.create_task(
                LogicalTaskContract(
                    run_id=persisted_run.id,
                    task_id=task_id,
                    purpose=purpose,
                    parent_task_id=primary_id,
                    route=binding(route),
                    budget=TaskBudget(),
                ),
                idempotency_key=str(task_id),
            )
        await work.commit()
    page = await SubscriptionTaskQuery(session_factory).tasks(persisted_run.id)
    by_id = {task["task_id"]: task for task in page["tasks"]}
    assert by_id[task_ids[0]]["requested_route"] != by_id[task_ids[0]]["effective_route"]
    assert by_id[task_ids[0]]["fallback_selected"] is False
    assert by_id[task_ids[1]]["fallback_selected"] is True
