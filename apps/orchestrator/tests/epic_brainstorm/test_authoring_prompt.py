import json
from uuid import uuid4

from forge.agents.epic_brainstorm_protocol import authoring_prompt
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    BrainstormTurn,
    FrozenBriefContent,
    FrozenRequirement,
)
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget


def _job(*, kind="brainstorm", content=None):
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    return AuthoringJobSnapshot(
        job_id=uuid4(), epic_id=uuid4(), project_id=uuid4(), conversation_id=uuid4(),
        kind=kind, prompt_turn_id=uuid4(), input_brief_revision_id=None,
        input_brief_digest=None, input_draft_digest="0" * 64,
        draft_content=content or FrozenBriefContent(), expected_epic_version=1,
        conversation_version=1, reservation_id=uuid4(),
        route=RouteBinding(requested=route, effective=route), budget=TaskBudget(),
    )


def test_brainstorm_prompt_collaboratively_elaborates_rough_and_empty_drafts():
    job = _job(content=FrozenBriefContent(problem="A rough idea"))
    prompt = authoring_prompt(job, ())
    assert "rough idea" in prompt.lower()
    assert "incomplete" in prompt.lower()
    assert "acceptance criteria" in prompt.lower()
    assert "open_questions" in prompt
    assert "assumptions" in prompt
    assert '"prompt_turn_id": "' + str(job.prompt_turn_id) + '"' in prompt
    assert "proposal matching the supplied schema" in prompt


def test_brainstorm_prompt_preserves_confirmed_decisions_when_iterating():
    accepted = FrozenBriefContent(
        decisions=("Use email alerts",),
        requirements=(
            FrozenRequirement(
                requirement_id=uuid4(),
                text="Notify the team",
                acceptance_criteria=("Within five minutes",),
            ),
        ),
        open_questions=("Which channel should alerts use?",),
    )
    job = _job(content=FrozenBriefContent(decisions=("Use email alerts",)))
    job = job.model_copy(update={"accepted_content": accepted})
    history = (
        BrainstormTurn(
            conversation_id=job.conversation_id,
            role="operator",
            text="Keep the alert timing, but help me choose the channel.",
        ),
    )
    prompt = authoring_prompt(job, history)
    assert "preserve confirmed decisions" in prompt.lower()
    assert "few most important unanswered choices" in prompt.lower()
    context = json.loads(prompt.split("\n", 1)[1])
    assert context["accepted_content"]["decisions"] == ["Use email alerts"]
    assert context["accepted_content"]["requirements"][0]["acceptance_criteria"] == [
        "Within five minutes"
    ]
    assert context["accepted_content"]["open_questions"] == ["Which channel should alerts use?"]
    assert context["turns"][0]["text"] == "Keep the alert timing, but help me choose the channel."


def test_empty_brainstorm_draft_is_accepted_by_prompt():
    job = _job()
    context = json.loads(authoring_prompt(job, ()).split("\n", 1)[1])
    assert context["draft_content"]["problem"] == ""
