from uuid import UUID

import pytest
from forge.domain.epic_items import (
    GraphValidationError,
    ItemInput,
    make_snapshot,
    project_readiness,
    validate_graph,
)
from pydantic import ValidationError


def item(identifier: int, dependencies: tuple[int, ...] = ()) -> ItemInput:
    return ItemInput(
        item_id=UUID(int=identifier),
        disposition="required",
        ordinal=identifier,
        title=f"Item {identifier}",
        outcome="An outcome",
        acceptance_criteria=["Done"],
        source_requirement_ids=[UUID(int=100)],
        dependency_item_ids=[UUID(int=value) for value in dependencies],
    )


def test_graph_rejects_cycle_and_dangling_edges() -> None:
    with pytest.raises(GraphValidationError):
        validate_graph([item(1, (2,)), item(2, (1,))])
    with pytest.raises(GraphValidationError):
        validate_graph([item(1, (2,))])


def test_adoption_rejects_transitive_required_on_deferred_but_save_retains_draft() -> None:
    deferred = item(1).model_copy(update={"disposition": "deferred"})
    graph = [deferred, item(2, (1,)), item(3, (2,))]
    validate_graph(graph)
    with pytest.raises(GraphValidationError, match="deferred"):
        validate_graph(graph, adoption=True)
    validate_graph(
        [
            item(1),
            deferred.model_copy(
                update={"item_id": UUID(int=4), "dependency_item_ids": [UUID(int=1)]}
            ),
        ],
        adoption=True,
    )


def test_snapshot_canonical_order_digests_and_readiness() -> None:
    first = item(1)
    second = item(2, (1,)).model_copy(
        update={"source_requirement_ids": [UUID(int=102), UUID(int=100)]}
    )
    one, digest_one = make_snapshot(UUID(int=500), [second, first], UUID(int=50), "a" * 64)
    two, digest_two = make_snapshot(
        UUID(int=500),
        [
            first,
            second.model_copy(update={"source_requirement_ids": [UUID(int=100), UUID(int=102)]}),
        ],
        UUID(int=50),
        "a" * 64,
    )
    assert one == two and digest_one == digest_two
    assert [entry.status for entry in project_readiness(one)] == ["ready", "blocked"]
    assert make_snapshot(UUID(int=500), [first, second], UUID(int=50), "b" * 64)[1] != digest_one
    assert (
        make_snapshot(
            UUID(int=500),
            [first.model_copy(update={"acceptance_criteria": ["Other"]}), second],
            UUID(int=50),
            "a" * 64,
        )[1]
        != digest_one
    )


def test_closed_item_bounds_and_mutation_revalidation() -> None:
    with pytest.raises(ValidationError):
        ItemInput.model_validate({**item(1).model_dump(), "status": "done"})
    with pytest.raises(ValidationError):
        item(1).model_copy(update={"acceptance_criteria": []}).__class__.model_validate(
            item(1).model_copy(update={"acceptance_criteria": []}).model_dump()
        )
    with pytest.raises(ValidationError):
        ItemInput.model_validate({**item(1).model_dump(), "title": "é" * 129})
