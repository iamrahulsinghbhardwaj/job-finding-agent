"""Test doubles. FakeSource implements JobSource from in-memory data, no network."""

import hashlib
from collections.abc import AsyncIterator, Sequence
from collections.abc import Set as AbstractSet

from jobagent.core.contracts import QueryLedger
from jobagent.core.models import HealthReport, JobDetail, RawJob, SearchSpec


class FakeSource:
    """In-memory JobSource.

    - full_listing=True (default): every search yields all `jobs` (one "page"), like a board.
    - full_listing=False: ONE query ("fake") in pages of `page_size`, stopping at a short page, at
      spec.max_pages, or (once the ledger says it is backfilled) at the first page whose ids are
      all in `known_job_ids`, recording progress in the ledger, like Amazon.
    - `fail_after=n, error=exc`: raise `exc` after yielding n jobs (n=0 fails before any job).
    `jobs` is a public list: tests add or remove listings between runs.
    """

    def __init__(
        self,
        jobs: Sequence[RawJob],
        details: dict[str, JobDetail] | None = None,
        *,
        full_listing: bool = True,
        page_size: int = 2,
        fail_after: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.jobs = list(jobs)  # newest first
        self._details = details or {}
        self.full_listing = full_listing
        self._page_size = page_size
        self._fail_after = fail_after
        self._error = error
        self.known_seen: list[frozenset[str]] = []  # known_job_ids received, one per search
        self.pages_fetched = 0  # in the last search

    QUERY = "fake"

    async def search(
        self, spec: SearchSpec, known_job_ids: AbstractSet[str], ledger: QueryLedger | None = None
    ) -> AsyncIterator[RawJob]:
        self.known_seen.append(frozenset(known_job_ids))
        ledger = ledger if ledger is not None else QueryLedger()
        paged = not self.full_listing
        if paged:
            ledger.started.add(self.QUERY)
        self.pages_fetched = 0
        yielded = 0
        pages = self._pages()
        for number, page in enumerate(pages):
            if number == spec.max_pages:
                ledger.full_pass.add(self.QUERY)
                ledger.capped.add(self.QUERY)
                break
            self.pages_fetched += 1
            for job in page:
                if self._fail_after is not None and yielded >= self._fail_after:
                    raise self._error or RuntimeError("fake failure")
                yield job
                yielded += 1
            if number == len(pages) - 1:
                ledger.full_pass.add(self.QUERY)  # last page
            elif (
                paged
                and ledger.can_stop_early(self.QUERY)
                and all(j.source_job_id in known_job_ids for j in page)
            ):
                break
        if self._fail_after is not None and yielded >= self._fail_after:
            raise self._error or RuntimeError("fake failure")
        if paged:
            ledger.finished.add(self.QUERY)

    def _pages(self) -> list[list[RawJob]]:
        if self.full_listing:
            return [self.jobs]
        size = self._page_size
        return [self.jobs[i : i + size] for i in range(0, len(self.jobs), size)] or [[]]

    async def get_details(self, job_id: str) -> JobDetail:
        return self._details[job_id]

    async def health_check(self) -> HealthReport:
        keys = sorted({key for job in self.jobs for key in job.raw})
        schema_hash = hashlib.sha256(",".join(keys).encode()).hexdigest()[:16]
        return HealthReport(ok=bool(self.jobs), job_count=len(self.jobs), schema_hash=schema_hash)
