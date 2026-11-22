"""The contract every job source (platform or custom adapter) implements."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from jobagent.core.models import HealthReport, JobDetail, RawJob, SearchSpec


@runtime_checkable
class JobSource(Protocol):
    def search(self, spec: SearchSpec, stop_at_job_id: str | None) -> AsyncIterator[RawJob]:
        """Yield jobs matching `spec`, newest first.

        Stops *before* `stop_at_job_id` (that job is already stored, so it is not yielded).
        If it is None or never seen, yields everything up to `spec.max_pages`.

        Declared with plain `def` on purpose: implementations are async generators
        (`async def search(...)` containing `yield`), whose type is
        `(...) -> AsyncIterator[RawJob]`. Writing `async def` here would instead mean
        "coroutine returning an iterator", and real adapters would not match.
        """
        ...

    async def get_details(self, job_id: str) -> JobDetail:
        """Fetch the full description for one job. Called only for new, filtered jobs."""
        ...

    async def health_check(self) -> HealthReport:
        """Cheap check that the source still works: ok, job count, response schema hash."""
        ...
