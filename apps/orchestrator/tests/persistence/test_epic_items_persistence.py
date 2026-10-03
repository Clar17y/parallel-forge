"""Atomic graph replacement, replay, binding, and retained history on PostgreSQL."""

import asyncio
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_items import EpicItemsService
from forge.domain.epic_brief import (
    BriefAdoptionRequest,
    BriefBindingConflict,
    BriefContent,
    BriefRequirement,
    BriefRevisionCreateRequest,
    EpicCreateRequest,
    EpicVersionConflict,
)
from forge.domain.epic_items import (
    GraphAdoptionRequest,
    GraphBindingConflict,
    GraphNotAccepted,
    GraphRevisionCreateRequest,
    GraphValidationError,
    ItemInput,
)
from forge.persistence.models import ApiMutation, OperatorAuditEvent, Project
from forge.persistence.models.epic_items import EpicGraphRevision
from forge.persistence.repositories.mutations import MutationConflict, PostgresMutationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    yield test_database_url


@pytest.fixture
def actor():
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


async def setup_epic(session_factory, actor, project_id):
    brief = EpicBriefService(lambda: PostgresUnitOfWork(session_factory))
    graph = EpicItemsService(lambda: PostgresUnitOfWork(session_factory))
    epic = await brief.create(
        actor=actor,
        idempotency_key="epic",
        request=EpicCreateRequest(project_id=project_id, title="Epic"),
    )
    requirement = uuid4()
    revision = await brief.save_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="brief-save",
        request=BriefRevisionCreateRequest(
            expected_epic_version=1,
            content=BriefContent(
                problem="Build",
                outcomes=["Useful"],
                requirements=[
                    BriefRequirement(
                        requirement_id=requirement, text="Need", acceptance_criteria=["Works"]
                    )
                ],
            ),
        ),
    )
    await brief.adopt_revision(
        actor=actor,
        epic_id=epic.epic_id,
        idempotency_key="brief-adopt",
        request=BriefAdoptionRequest(
            expected_epic_version=2,
            brief_revision_id=revision.brief_revision_id,
            brief_digest=revision.content_digest,
        ),
    )
    return brief, graph, epic.epic_id, revision, requirement


def item(identifier, requirement, dependencies=(), disposition="required"):
    return ItemInput(
        item_id=UUID(int=identifier),
        disposition=disposition,
        ordinal=identifier,
        title=f"Item {identifier}",
        outcome="Outcome",
        acceptance_criteria=["Done"],
        source_requirement_ids=[requirement],
        dependency_item_ids=[UUID(int=value) for value in dependencies],
    )


def request(version, revision, items):
    return GraphRevisionCreateRequest(
        expected_epic_version=version,
        brief_revision_id=revision.brief_revision_id,
        brief_digest=revision.content_digest,
        items=items,
    )


@pytest.mark.asyncio
async def test_replace_graph_replays_and_preserves_selected_history(
    session_factory, actor, project_id
):
    brief, graph, epic_id, revision, requirement = await setup_epic(
        session_factory, actor, project_id
    )
    initial = request(3, revision, [item(1, requirement), item(2, requirement, (1,))])
    first, duplicate = await asyncio.gather(
        *(
            graph.save_revision(
                actor=actor, epic_id=epic_id, idempotency_key="save-one", request=initial
            )
            for _ in range(2)
        )
    )
    assert first == duplicate
    selected = await graph.adopt_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="adopt-one",
        request=GraphAdoptionRequest(
            expected_epic_version=4,
            graph_revision_id=first.graph_revision_id,
            graph_digest=first.graph_digest,
        ),
    )
    assert selected.accepted_graph_revision_id == first.graph_revision_id
    # Split one item into two and rewrite the successor edge in one replacement.
    replacement = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="save-two",
        request=request(
            5,
            revision,
            [item(1, requirement), item(3, requirement, (1,)), item(2, requirement, (3,))],
        ),
    )
    assert replacement.revision_number == 2
    assert (await graph.accepted(epic_id)).graph_revision_id == first.graph_revision_id
    assert (await graph.get_revision(epic_id, first.graph_revision_id)) == first
    assert len(await graph.list_revisions(epic_id)) == 2
    combined = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="save-three",
        request=request(6, revision, [item(1, requirement), item(2, requirement, (1,))]),
    )
    assert combined.revision_number == 3
    assert (await graph.get_revision(epic_id, replacement.graph_revision_id)).items[
        1
    ].dependency_item_ids == [UUID(int=3)]
    assert (await brief.get(epic_id)).accepted_graph_revision_id == first.graph_revision_id


@pytest.mark.asyncio
async def test_stale_concurrent_save_adopt_binding_and_rollback(
    session_factory, actor, project_id, monkeypatch
):
    brief, graph, epic_id, revision, requirement = await setup_epic(
        session_factory, actor, project_id
    )
    left = request(3, revision, [item(1, requirement)])
    right = request(3, revision, [item(2, requirement)])
    outcomes = await asyncio.gather(
        graph.save_revision(actor=actor, epic_id=epic_id, idempotency_key="left", request=left),
        graph.save_revision(actor=actor, epic_id=epic_id, idempotency_key="right", request=right),
        return_exceptions=True,
    )
    assert sum(isinstance(result, EpicVersionConflict) for result in outcomes) == 1
    saved = next(result for result in outcomes if not isinstance(result, Exception))
    with pytest.raises(MutationConflict):
        await graph.save_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="left" if saved.items[0].item_id == UUID(int=1) else "right",
            request=request(4, revision, []),
        )
    with pytest.raises(GraphBindingConflict):
        await graph.adopt_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="bad-digest",
            request=GraphAdoptionRequest(
                expected_epic_version=4,
                graph_revision_id=saved.graph_revision_id,
                graph_digest="a" * 64,
            ),
        )

    async def fail(*args, **kwargs):
        raise RuntimeError("receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMutationRepository, "complete", fail)
        with pytest.raises(RuntimeError, match="receipt failure"):
            await graph.adopt_revision(
                actor=actor,
                epic_id=epic_id,
                idempotency_key="failed-adopt",
                request=GraphAdoptionRequest(
                    expected_epic_version=4,
                    graph_revision_id=saved.graph_revision_id,
                    graph_digest=saved.graph_digest,
                ),
            )
    assert (await brief.get(epic_id)).version == 4
    assert (await brief.get(epic_id)).accepted_graph_revision_id is None
    adoption = GraphAdoptionRequest(
        expected_epic_version=4,
        graph_revision_id=saved.graph_revision_id,
        graph_digest=saved.graph_digest,
    )
    adopted, replay = await asyncio.gather(
        *(
            graph.adopt_revision(
                actor=actor, epic_id=epic_id, idempotency_key="adopt", request=adoption
            )
            for _ in range(2)
        )
    )
    assert adopted == replay
    async with session_factory() as session:
        assert len((await session.execute(select(EpicGraphRevision))).scalars().all()) == 1
        assert len((await session.execute(select(ApiMutation))).scalars().all()) == 5
        assert len((await session.execute(select(OperatorAuditEvent))).scalars().all()) == 5


@pytest.mark.asyncio
async def test_deferred_draft_cannot_be_adopted(session_factory, actor, project_id):
    _, graph, epic_id, revision, requirement = await setup_epic(session_factory, actor, project_id)
    draft = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="draft",
        request=request(
            3, revision, [item(1, requirement, disposition="deferred"), item(2, requirement, (1,))]
        ),
    )
    with pytest.raises(GraphValidationError):
        await graph.adopt_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="adopt",
            request=GraphAdoptionRequest(
                expected_epic_version=4,
                graph_revision_id=draft.graph_revision_id,
                graph_digest=draft.graph_digest,
            ),
        )


@pytest.mark.asyncio
async def test_brief_change_clears_graph_and_preserves_old_revision(
    session_factory, actor, project_id
):
    brief, graph, epic_id, first_brief, requirement = await setup_epic(
        session_factory, actor, project_id
    )
    old = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="graph-old",
        request=request(3, first_brief, [item(1, requirement)]),
    )
    await graph.adopt_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="graph-adopt",
        request=GraphAdoptionRequest(
            expected_epic_version=4,
            graph_revision_id=old.graph_revision_id,
            graph_digest=old.graph_digest,
        ),
    )
    new_requirement = uuid4()
    second_brief = await brief.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="brief-new",
        request=BriefRevisionCreateRequest(
            expected_epic_version=5,
            content=BriefContent(
                problem="Changed",
                outcomes=["Different"],
                requirements=[
                    BriefRequirement(
                        requirement_id=new_requirement, text="New", acceptance_criteria=["Works"]
                    )
                ],
            ),
        ),
    )
    changed = await brief.adopt_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="brief-switch",
        request=BriefAdoptionRequest(
            expected_epic_version=6,
            brief_revision_id=second_brief.brief_revision_id,
            brief_digest=second_brief.content_digest,
        ),
    )
    assert changed.accepted_graph_revision_id is None
    assert await graph.get_revision(epic_id, old.graph_revision_id) == old
    with pytest.raises(GraphNotAccepted):
        await graph.accepted(epic_id)
    with pytest.raises(BriefBindingConflict):
        await graph.adopt_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="old-adopt",
            request=GraphAdoptionRequest(
                expected_epic_version=7,
                graph_revision_id=old.graph_revision_id,
                graph_digest=old.graph_digest,
            ),
        )
    with pytest.raises(GraphValidationError):
        await graph.save_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="stale-requirement",
            request=request(7, second_brief, [item(1, requirement)]),
        )
    current = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="graph-new",
        request=request(7, second_brief, [item(1, new_requirement)]),
    )
    assert current.revision_number == 2


@pytest.mark.asyncio
async def test_concurrent_distinct_adoptions_cas_one_winner(session_factory, actor, project_id):
    _, graph, epic_id, revision, requirement = await setup_epic(session_factory, actor, project_id)
    one = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="one",
        request=request(3, revision, [item(1, requirement)]),
    )
    two = await graph.save_revision(
        actor=actor,
        epic_id=epic_id,
        idempotency_key="two",
        request=request(4, revision, [item(2, requirement)]),
    )
    outcomes = await asyncio.gather(
        graph.adopt_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="adopt-one",
            request=GraphAdoptionRequest(
                expected_epic_version=5,
                graph_revision_id=one.graph_revision_id,
                graph_digest=one.graph_digest,
            ),
        ),
        graph.adopt_revision(
            actor=actor,
            epic_id=epic_id,
            idempotency_key="adopt-two",
            request=GraphAdoptionRequest(
                expected_epic_version=5,
                graph_revision_id=two.graph_revision_id,
                graph_digest=two.graph_digest,
            ),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, EpicVersionConflict) for result in outcomes) == 1
    winner = next(result for result in outcomes if not isinstance(result, Exception))
    assert (await graph.accepted(epic_id)).graph_revision_id == winner.accepted_graph_revision_id
