"""Slice D domain: PR states, settings, repair prompt bounds."""

from forge.domain.direct import DirectRunState, build_direct_pr_repair_prompt
from forge.settings import Settings


def test_slice_d_states_and_settings() -> None:
    assert DirectRunState.PUBLISHING == "PUBLISHING"
    assert DirectRunState.PR_OPEN == "PR_OPEN"
    s = Settings()
    assert s.direct_max_pr_rounds == 3
    assert s.direct_pr_poll_seconds == 60
    assert s.direct_pr_quiet_seconds == 180
    assert s.direct_pr_watch_timeout_seconds == 1800


def test_pr_repair_prompt_bounded_and_marked_untrusted() -> None:
    prompt = build_direct_pr_repair_prompt("Fix bug", "x" * 20000, has_agents_md=False)
    assert "untrusted" in prompt.lower()
    assert "PR comment" in prompt
    assert len(prompt) < 20000
