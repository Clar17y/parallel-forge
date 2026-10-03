"""Transactional epic lifecycle and durable replay on PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brief import EpicBriefService
from forge.domain.epic_brief import (
    BriefAdoptionRequest,
    BriefBindingConflict,
    BriefContent,
    BriefRequirement,
    BriefRevisionCreateRequest,
    BriefRevisionNotFound,
    EpicCreateRequest,
    EpicDraftUpdateRequest,
    EpicVersionConflict,
)
from forge.persistence.models import ApiMutation, OperatorAuditEvent, Project
from forge.persistence.models.epic_brief import Epic, EpicBriefRevision
from forge.persistence.repositories.mutations import MutationConflict, PostgresMutationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory) -> Iterator[str]:
    # The data-protecting migration intentionally refuses downgrade with records.
    command.upgrade(alembic_config_factory(test_database_url), "head")
    yield test_database_url


@pytest.fixture
def actor() -> AuthenticatedActor:
    return AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())


@pytest.fixture
async def project_id(session_factory):
    identity = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            Project(
                id=identity,
                canonical_path=f"/tmp/forge-{identity}",
                github_repository=f"example/forge-{identity}",
                default_branch="main",
            )
        )
    return identity


def _service(session_factory) -> EpicBriefService:
    return EpicBriefService(lambda: PostgresUnitOfWork(session_factory))


def _content(label: str = "A") -> BriefContent:
    return BriefContent(
        problem="Build a brief",
        outcomes=[label],
        requirements=[
            BriefRequirement(requirement_id=uuid4(), text="Need", acceptance_criteria=["Works"])
        ],
    )


@pytest.mark.asyncio
async def test_reopen_revision_selection_and_all_replays(session_factory, project_id, actor):
    service = _service(session_factory)
    created = await service.create(
        actor=actor,
        idempotency_key="create",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )
    draft = BriefContent(problem="Draft only")
    edited = await service.update_draft(
        actor=actor,
        epic_id=created.epic_id,
        idempotency_key="edit",
        request=EpicDraftUpdateRequest(expected_epic_version=1, title="Edited", draft=draft),
    )
    content = _content()
    saved = await service.save_revision(
        actor=actor,
        epic_id=created.epic_id,
        idempotency_key="save",
        request=BriefRevisionCreateRequest(expected_epic_version=2, content=content),
    )
    selected = await service.adopt_revision(
        actor=actor,
        epic_id=created.epic_id,
        idempotency_key="adopt",
        request=BriefAdoptionRequest(
            expected_epic_version=3,
            brief_revision_id=saved.brief_revision_id,
            brief_digest=saved.content_digest,
        ),
    )
    changed = await service.update_draft(
        actor=actor,
        epic_id=created.epic_id,
        idempotency_key="edit-next",
        request=EpicDraftUpdateRequest(
            expected_epic_version=4, title="Later", draft=BriefContent()
        ),
    )
    reopened = _service(session_factory)
    assert changed.version == 5 and changed.accepted_brief_revision_id == saved.brief_revision_id
    assert (await reopened.accepted(created.epic_id)).requirements[
        0
    ].requirement_id == content.requirements[0].requirement_id
    assert (
        await reopened.get_revision(created.epic_id, saved.brief_revision_id)
    ).content == content
    assert (
        await reopened.create(
            actor=actor,
            idempotency_key="create",
            request=EpicCreateRequest(project_id=project_id, title="Epic"),
        )
        == created
    )
    assert (
        await reopened.update_draft(
            actor=actor,
            epic_id=created.epic_id,
            idempotency_key="edit",
            request=EpicDraftUpdateRequest(expected_epic_version=1, title="Edited", draft=draft),
        )
        == edited
    )
    assert (
        await reopened.save_revision(
            actor=actor,
            epic_id=created.epic_id,
            idempotency_key="save",
            request=BriefRevisionCreateRequest(expected_epic_version=2, content=content),
        )
        == saved
    )
    assert (
        await reopened.adopt_revision(
            actor=actor,
            epic_id=created.epic_id,
            idempotency_key="adopt",
            request=BriefAdoptionRequest(
                expected_epic_version=3,
                brief_revision_id=saved.brief_revision_id,
                brief_digest=saved.content_digest,
            ),
        )
        == selected
    )
    assert (await reopened.get(created.epic_id)).version == 5
    assert len(await reopened.list_revisions(created.epic_id)) == 1
    async with session_factory() as session:
        assert len((await session.execute(select(OperatorAuditEvent))).scalars().all()) == 5
        assert len((await session.execute(select(ApiMutation))).scalars().all()) == 5


@pytest.mark.asyncio
async def test_selection_binding_graph_transition_and_rollback(session_factory, project_id, actor):
    service = _service(session_factory)
    epic = await service.create(
        actor=actor,
        idempotency_key="create",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )
    first = await service.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="save1",
        request=BriefRevisionCreateRequest(expected_epic_version=1, content=_content("one")),
    )
    await service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt1",
        request=BriefAdoptionRequest(
            expected_epic_version=2,
            brief_revision_id=first.brief_revision_id,
            brief_digest=first.content_digest,
        ),
    )
    graph_id = uuid4()
    async with session_factory() as session, session.begin():
        row = await session.get(Epic, epic.epic_id)
        assert row is not None
        row.accepted_graph_revision_id = graph_id
        row.accepted_graph_digest = "a" * 64
    same = await service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt-same",
        request=BriefAdoptionRequest(
            expected_epic_version=3,
            brief_revision_id=first.brief_revision_id,
            brief_digest=first.content_digest,
        ),
    )
    assert same.accepted_graph_revision_id == graph_id
    second = await service.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="save2",
        request=BriefRevisionCreateRequest(expected_epic_version=4, content=_content("two")),
    )
    with pytest.raises(BriefBindingConflict):
        await service.adopt_revision(
            actor=actor,
            epic_id=epic.epic_id,
            idempotency_key="bad",
            request=BriefAdoptionRequest(
                expected_epic_version=5,
                brief_revision_id=second.brief_revision_id,
                brief_digest=first.content_digest,
            ),
        )
    with pytest.raises(BriefRevisionNotFound):
        await service.adopt_revision(
            actor=actor,
            epic_id=epic.epic_id,
            idempotency_key="foreign",
            request=BriefAdoptionRequest(
                expected_epic_version=5,
                brief_revision_id=uuid4(),
                brief_digest=second.content_digest,
            ),
        )
    assert (await service.get(epic.epic_id)).version == 5
    changed = await service.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="adopt2",
        request=BriefAdoptionRequest(
            expected_epic_version=5,
            brief_revision_id=second.brief_revision_id,
            brief_digest=second.content_digest,
        ),
    )
    assert changed.accepted_graph_revision_id is None and changed.accepted_graph_digest is None
    assert changed.accepted_brief_revision_id == second.brief_revision_id
    async with session_factory() as session:
        assert len((await session.execute(select(ApiMutation))).scalars().all()) == 6


@pytest.mark.asyncio
async def test_concurrent_version_and_duplicate_key_writers(session_factory, project_id, actor):
    service = _service(session_factory)
    epic = await service.create(
        actor=actor,
        idempotency_key="create",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )
    request = BriefRevisionCreateRequest(expected_epic_version=1, content=_content())
    duplicate = await asyncio.gather(
        service.save_revision(
            actor=actor, epic_id=epic.epic_id, idempotency_key="same", request=request
        ),
        service.save_revision(
            actor=actor, epic_id=epic.epic_id, idempotency_key="same", request=request
        ),
    )
    assert duplicate[0] == duplicate[1]
    assert len(await service.list_revisions(epic.epic_id)) == 1

    async def writer(key: str):
        return await service.update_draft(
            actor=actor,
            epic_id=epic.epic_id,
            idempotency_key=key,
            request=EpicDraftUpdateRequest(
                expected_epic_version=2, title=key, draft=BriefContent()
            ),
        )

    outcomes = await asyncio.gather(writer("left"), writer("right"), return_exceptions=True)
    assert sum(isinstance(item, EpicVersionConflict) for item in outcomes) == 1
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    with pytest.raises(MutationConflict):
        await service.save_revision(
            actor=actor,
            epic_id=epic.epic_id,
            idempotency_key="same",
            request=BriefRevisionCreateRequest(
                expected_epic_version=2, content=_content("different")
            ),
        )
    async with session_factory() as session:
        assert len((await session.execute(select(EpicBriefRevision))).scalars().all()) == 1
        assert len((await session.execute(select(ApiMutation))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_failed_receipt_completion_rolls_back_revision_and_audit(
    session_factory, project_id, actor, monkeypatch
):
    service = _service(session_factory)
    epic = await service.create(
        actor=actor,
        idempotency_key="create",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )

    async def fail_complete(*args, **kwargs):
        raise RuntimeError("injected completion failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMutationRepository, "complete", fail_complete)
        with pytest.raises(RuntimeError, match="injected completion failure"):
            await service.save_revision(
                actor=actor,
                epic_id=epic.epic_id,
                idempotency_key="failed-save",
                request=BriefRevisionCreateRequest(expected_epic_version=1, content=_content()),
            )
    assert (await service.get(epic.epic_id)).version == 1
    assert await service.list_revisions(epic.epic_id) == []
    async with session_factory() as session:
        assert len((await session.execute(select(OperatorAuditEvent))).scalars().all()) == 1
        assert len((await session.execute(select(ApiMutation))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_foreign_revision_and_incomplete_selection_are_rejected(
    session_factory, project_id, actor
):
    service = _service(session_factory)
    target = await service.create(
        actor=actor,
        idempotency_key="target",
        request=EpicCreateRequest(project_id=project_id, title="Target"),
    )
    foreign = await service.create(
        actor=actor,
        idempotency_key="foreign",
        request=EpicCreateRequest(project_id=project_id, title="Foreign"),
    )
    revision = await service.save_revision(
        actor=actor,
        epic_id=foreign.epic_id,
        idempotency_key="foreign-save",
        request=BriefRevisionCreateRequest(expected_epic_version=1, content=_content()),
    )
    with pytest.raises(BriefRevisionNotFound):
        await service.adopt_revision(
            actor=actor,
            epic_id=target.epic_id,
            idempotency_key="foreign-adopt",
            request=BriefAdoptionRequest(
                expected_epic_version=1,
                brief_revision_id=revision.brief_revision_id,
                brief_digest=revision.content_digest,
            ),
        )
    incomplete = await service.save_revision(
        actor=actor,
        epic_id=target.epic_id,
        idempotency_key="incomplete-save",
        request=BriefRevisionCreateRequest(
            expected_epic_version=1, content=BriefContent(problem="Only a problem")
        ),
    )
    with pytest.raises(ValueError, match="incomplete"):
        await service.adopt_revision(
            actor=actor,
            epic_id=target.epic_id,
            idempotency_key="incomplete-adopt",
            request=BriefAdoptionRequest(
                expected_epic_version=2,
                brief_revision_id=incomplete.brief_revision_id,
                brief_digest=incomplete.content_digest,
            ),
        )
    assert (await service.get(target.epic_id)).accepted_brief_revision_id is None
