from typing import Protocol
from uuid import UUID


class DashboardQueryPort(Protocol):
    async def summary(self) -> dict[str, object]: ...
    async def run_projection(self, run_id: UUID) -> dict[str, object] | None: ...
