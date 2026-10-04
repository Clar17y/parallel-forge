"""Closed epic launch request and immutable attempt response."""

from forge.domain.epic_run_bridge import EpicAttempt, LaunchRequest


class EpicLaunchRequest(LaunchRequest):
    pass


class EpicAttemptResponse(EpicAttempt):
    pass
