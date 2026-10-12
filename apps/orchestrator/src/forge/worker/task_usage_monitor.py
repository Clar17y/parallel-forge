"""Observe first, then evaluate the durable run projection."""

from __future__ import annotations

from forge.application.ports.task_usage import TaskUsageObserver
from forge.application.services.task_usage_monitor import RunTaskUsageMonitor
from forge.domain.task_usage_contract import Observation


class MonitoringObservationSink:
    def __init__(self, observations: TaskUsageObserver, monitor: RunTaskUsageMonitor) -> None:
        self._observations, self._monitor = observations, monitor

    async def observe_usage(self, observation: Observation) -> None:
        await self._observations.observe_usage(observation)
        if observation.binding.subject.kind == "run":
            checkpoint = await self._monitor.observe(observation)
            if checkpoint is not None:
                await self._monitor.advance(checkpoint.checkpoint_id)
