"""Run-level decisions from frozen references and measured cumulative usage."""

from __future__ import annotations

from typing import Literal

from forge.domain.task_usage_contract import BaselineReference, MonitoringMode, MonitoringPolicy

MonitorDecision = Literal["report_only", "warning", "checkpoint"]


def evaluate_run_usage(
    baseline: BaselineReference | None,
    policy: MonitoringPolicy,
    *,
    measured_tokens: int | None,
    completeness: str,
    next_checkpoint_threshold_tokens: int | None = None,
    usage_pauses_disabled: bool = False,
) -> MonitorDecision:
    """Unknown totals cannot cause an automatic pause; partial totals are a lower bound."""
    if measured_tokens is None or completeness == "unknown" or baseline is None:
        return "report_only"
    if completeness not in {"complete", "lower_bound"} or measured_tokens < 0:
        raise ValueError("invalid measured usage")
    if baseline.binding.subject.project_id != policy.project_id:
        raise ValueError("monitor policy differs from subject project")
    warning = baseline.warning_threshold_tokens
    checkpoint = next_checkpoint_threshold_tokens or baseline.checkpoint_threshold_tokens
    if (
        policy.mode is MonitoringMode.CHECKPOINT
        and not usage_pauses_disabled
        and checkpoint is not None
        and measured_tokens >= checkpoint
    ):
        return "checkpoint"
    if (
        policy.mode in {MonitoringMode.CHECKPOINT, MonitoringMode.WARN}
        and warning is not None
        and measured_tokens >= warning
    ):
        return "warning"
    return "report_only"
