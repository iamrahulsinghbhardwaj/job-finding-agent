"""The contract every job source (platform or custom adapter) implements."""

from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from jobagent.core.models import HealthReport, JobDetail, RawJob, SearchSpec


@dataclass
class QueryLedger:
    """Per-query crawl state of a paged source, loaded before a search and saved after it.

    A query may stop early at a page of already-known ids only once it is BACKFILLED: it has
    finished one full pass (reached its last page or spec.max_pages). Otherwise a pass that was
    cut short (timeout, error) would leave older pages unfetched forever, because the next run's
    page 1 is already known.

    The adapter records, per query key, when it starts, when it finishes cleanly (end, max_pages
    or an early stop) and whether that was a full pass. The ledger is mutated in place, so the
    record survives a timeout that cancels the search midway.
    """

    backfilled: frozenset[str] = frozenset()  # from the store
    started: set[str] = field(default_factory=set[str])
    finished: set[str] = field(default_factory=set[str])
    full_pass: set[str] = field(default_factory=set[str])
    capped: set[str] = field(default_factory=set[str])  # full passes that stopped at max_pages

    def can_stop_early(self, key: str) -> bool:
        return key in self.backfilled

    def updated_backfilled(self) -> frozenset[str]:
        """What to store: old + new full passes, minus any query whose pass was cut short.

        An interrupted pass un-backfills its query even if it was backfilled before: it may have
        stored new jobs from page 1 while newer jobs on page 2 were never reached.
        """
        interrupted = self.started - self.finished
        return frozenset((self.backfilled | self.full_pass) - interrupted)


@runtime_checkable
class JobSource(Protocol):
    # True when one search always returns the source's ENTIRE listing (Greenhouse, Lever, Ashby):
    # then jobs missing from a successful search have really gone (see upsert_jobs `complete`).
    full_listing: bool

    def search(
        self, spec: SearchSpec, known_job_ids: AbstractSet[str], ledger: QueryLedger | None = None
    ) -> AsyncIterator[RawJob]:
        """Yield jobs matching `spec`, newest first.

        `known_job_ids` are this source's jobs already in the store. Paged sources stop paging a
        query once an entire page holds only known ids, but only if `ledger` says that query is
        backfilled (spec.max_pages stays a hard cap); they record their progress in `ledger`
        (None: no early stop, nothing recorded). Full-listing sources ignore both.
        Jobs on fetched pages are yielded even when known, so their last_seen is refreshed.

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
