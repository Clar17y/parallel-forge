from uuid import uuid4

import pytest
from forge.domain.epic_brainstorm import AuthoringOutcome, BrainstormProposal, BrainstormTurn


def test_proposal_cannot_silently_answer_pending_question() -> None:
    turn = BrainstormTurn(
        conversation_id=uuid4(), role="operator", text="Which database?", pending=True
    )
    with pytest.raises(ValueError, match="pending"):
        BrainstormProposal(
            turn_id=turn.turn_id,
            problem="Use Postgres",
            decisions=("Postgres",),
            resolved_turn_ids=(turn.turn_id,),
        )


@pytest.mark.parametrize(
    "override",
    [
        {"state": "provider-secret"},
        {"failure": "oauth token leaked"},
        {"usage": {"input_tokens": -1, "output_tokens": 0, "tool_call_count": 0, "duration_ms": 0}},
        {
            "usage": {
                "duration_ms": None,
                "duration_lower_bound_ms": -1,
                "tool_call_count": 0,
                "unknown_fields": [
                    "duration_ms",
                    "input_tokens",
                    "output_tokens",
                    "estimated_api_cost_minor",
                ],
            }
        },
        {
            "usage": {
                "duration_ms": 4,
                "duration_lower_bound_ms": 5,
                "tool_call_count": 0,
                "unknown_fields": ["input_tokens", "output_tokens", "estimated_api_cost_minor"],
            }
        },
        {
            "usage": {
                "duration_ms": None,
                "duration_lower_bound_ms": 2**80,
                "tool_call_count": 0,
                "unknown_fields": [
                    "duration_ms",
                    "input_tokens",
                    "output_tokens",
                    "estimated_api_cost_minor",
                ],
            }
        },
        {
            "usage": {
                "input_tokens": None,
                "output_tokens": 0,
                "tool_call_count": 0,
                "duration_ms": 0,
            }
        },
        {
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "tool_call_count": 0,
                "duration_ms": 0,
                "stderr": "secret",
            }
        },
        {"reservation": {"duration_ms": -1, "tool_call_count": 1}},
        {"reservation": {"duration_ms": 1000, "tool_call_count": 1, "extra": 7}},
        {"cumulative_usage": {"input_tokens": -1}},
        {"cumulative_usage": {"input_tokens": 2**80}},
        {"held_reservations": {"tool_call_count": True}},
        {"held_reservations": {"secret_dimension": 1}},
        {"held_reasons": {"secret_dimension": "provider error"}},
        {"currency": "a raw provider error or credential"},
        {"unknown_usage_fields": ("stderr",)},
        {"uncertain_attempts": 2**80},
    ],
)
def test_outcome_rejects_unclosed_or_fabricated_projection(override: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AuthoringOutcome.model_validate(
            {"job_id": uuid4(), "job_version": 1, "state": "queued", **override}
        )


def test_outcome_serializes_explicit_unknown_usage_without_provider_text() -> None:
    outcome = AuthoringOutcome.model_validate(
        {
            "job_id": uuid4(),
            "job_version": 2,
            "state": "failed",
            "failure": "lost_result",
            "usage": {
                "duration_ms": 12,
                "duration_lower_bound_ms": 12,
                "tool_call_count": 2,
                "input_tokens": None,
                "output_tokens": None,
                "estimated_api_cost_minor": None,
                "unknown_fields": ["input_tokens", "output_tokens", "estimated_api_cost_minor"],
            },
            "unknown_usage_fields": ["input_tokens", "output_tokens", "estimated_api_cost_minor"],
            "held_reservations": {"input_tokens": 100},
            "held_reasons": {"input_tokens": "unsettled_or_unknown"},
        }
    )
    rendered = outcome.model_dump(mode="json")
    assert rendered["usage"]["input_tokens"] is None
    assert rendered["usage"]["duration_lower_bound_ms"] == 12
    assert rendered["usage"]["unknown_fields"] == [
        "input_tokens",
        "output_tokens",
        "estimated_api_cost_minor",
    ]
    assert rendered["held_reservations"]["input_tokens"] == 100
    assert rendered["held_reasons"]["input_tokens"] == "unsettled_or_unknown"
