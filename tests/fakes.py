"""Test doubles. FakeSource implements JobSource from in-memory data, no network."""

import hashlib
from collections.abc import AsyncIterator, Sequence

from jobagent.core.models import HealthReport, JobDetail, RawJob, SearchSpec


class FakeSource:
    def __init__(self, jobs: Sequence[RawJob], details: dict[str, JobDetail] | None = None) -> None:
        self._jobs = list(jobs)  # newest first
        self._details = details or {}

    async def search(self, spec: SearchSpec, stop_at_job_id: str | None) -> AsyncIterator[RawJob]:
        for job in self._jobs:
            if job.source_job_id == stop_at_job_id:
                return
            yield job

    async def get_details(self, job_id: str) -> JobDetail:
        return self._details[job_id]

    async def health_check(self) -> HealthReport:
        keys = sorted({key for job in self._jobs for key in job.raw})
        schema_hash = hashlib.sha256(",".join(keys).encode()).hexdigest()[:16]
        return HealthReport(ok=bool(self._jobs), job_count=len(self._jobs), schema_hash=schema_hash)
