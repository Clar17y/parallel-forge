"""Shared safety bounds for durable worker and effect leases."""

from __future__ import annotations

import math

# A one-second floor leaves enough time for a database round trip and the
# worker's one-third renewal cadence on supported local and CI environments.
MIN_LEASE_SECONDS = 1.0
MAX_LEASE_SECONDS = 24 * 60 * 60


def validate_lease_seconds(value: float) -> None:
    """Reject unsafe or unbounded lease durations consistently."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < MIN_LEASE_SECONDS
        or value > MAX_LEASE_SECONDS
    ):
        raise ValueError(
            f"lease duration must be at least {MIN_LEASE_SECONDS:g} second and at most "
            f"{MAX_LEASE_SECONDS:g} seconds"
        )


__all__ = ["MAX_LEASE_SECONDS", "MIN_LEASE_SECONDS", "validate_lease_seconds"]
