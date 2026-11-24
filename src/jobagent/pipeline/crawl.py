"""One crawl run (HLD §3, §5, §11): every active source fetched concurrently, isolated, then stored.

1. Prepare (DB): start a Run, sync the registry, read each source's known job ids. Commit, so the
   Run row exists even if the process dies.
2. Fetch (network): one asyncio task per source. Every exception (BlockedError, exhausted
   retries, timeout, a bug in one adapter) is caught inside its task, so one source never stops
   the others. Jobs yielded before a failure are kept.
3. Store (DB, one source at a time, each in its own savepoint): normalize, upsert, collect NEW
   job ids; failures go to dead_letter. Finally the per-source counts are saved on the Run row.

Staleness: a batch is `complete` (missing jobs get a missed run) only when the source returns
its whole listing in one search (full_listing) AND the fetch finished without error.

Paged sources: each source's QueryLedger (which queries are backfilled) is loaded from the DB,
updated by the adapter during the search (even one cut short by a timeout) and saved in the
same savepoint as the jobs, so the ledger never claims pages that were not stored.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from jobagent.adapters import build_source
from jobagent.core.contracts import JobSource, QueryLedger
from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, RawJob, SearchSpec, SourceConfig, SourceStatus, Tier
from jobagent.db import repo
from jobagent.pipeline.normalize import NormalizeStats, normalize_all, source_key

SOURCE_TIMEOUT = 120.0  # default seconds per source; SourceConfig.timeout_seconds overrides it
_MAX_ERROR = 300

log = logging.getLogger(__name__)

BuildSource = Callable[[Company, SourceConfig, PoliteClient], JobSource]


@dataclass
class SourceOutcome:
    """What happened to one source in this run (counts only: safe for public logs)."""

    company_slug: str
    source: str  # source_key, e.g. "greenhouse:stripe"
    blocked: bool = False
    fetched: int = 0
    new: int = 0
    errors: int = 0
    capped_queries: int = 0  # queries that stopped at spec.max_pages (more jobs exist than we read)
    complete: bool = False
    stats: NormalizeStats = field(default_factory=NormalizeStats)

    def counts(self) -> dict[str, int]:
        if self.blocked:
            return {"blocked": 1}
        return {
            "fetched": self.fetched,
            "new": self.new,
            "errors": self.errors,
            "capped_queries": self.capped_queries,
            **self.stats.as_counts(),
        }


@dataclass(frozen=True)
class CrawlResult:
    run_id: int
    outcomes: list[SourceOutcome]
    new_job_ids: frozenset[int]


@dataclass(frozen=True)
class _Plan:
    company: Company
    config: SourceConfig
    source_id: int
    known: frozenset[str]
    ledger: QueryLedger


@dataclass(frozen=True)
class _Fetched:
    raws: list[RawJob]
    full_listing: bool
    error: str | None


async def crawl(
    session: Session,
    companies: Sequence[Company],
    specs: Mapping[Tier, SearchSpec],
    *,
    http: PoliteClient,
    build: BuildSource = build_source,
    source_timeout: float = SOURCE_TIMEOUT,
) -> CrawlResult:
    """Run one crawl. Commits as it goes (the Run row, then each source, then the counts)."""
    run_id = repo.start_run(session)
    source_ids = repo.sync_registry(session, companies)
    outcomes: list[SourceOutcome] = []
    plans: list[_Plan] = []
    for company in companies:
        for config in company.sources:
            if config.status is SourceStatus.BLOCKED:  # never fetched, no workarounds
                outcomes.append(SourceOutcome(company.slug, source_key(config), blocked=True))
                continue
            source_id = source_ids[(company.slug, config.platform.value, config.tenant)]
            known = repo.known_job_ids(session, source_id)
            ledger = QueryLedger(backfilled=repo.backfilled_queries(session, source_id))
            plans.append(_Plan(company, config, source_id, known, ledger))
    session.commit()

    fetched = await asyncio.gather(
        *(
            _fetch(plan, specs[plan.company.tier], http, build, plan.config.timeout_seconds or source_timeout)
            for plan in plans
        )
    )

    new_ids: set[int] = set()
    for plan, result in zip(plans, fetched, strict=True):
        outcome = _store(session, run_id, plan, result)
        session.commit()
        new_ids |= outcome.new_job_ids
        outcomes.append(outcome.outcome)

    repo.finish_run(session, run_id, {o.source: o.counts() for o in outcomes})
    session.commit()
    return CrawlResult(run_id=run_id, outcomes=outcomes, new_job_ids=frozenset(new_ids))


async def _fetch(
    plan: _Plan, spec: SearchSpec, http: PoliteClient, build: BuildSource, seconds: float
) -> _Fetched:
    raws: list[RawJob] = []
    try:
        source = build(plan.company, plan.config, http)
        async with asyncio.timeout(seconds):
            async for job in source.search(spec, plan.known, plan.ledger):
                raws.append(job)
    except Exception as exc:  # isolation: one source must never stop the run
        return _Fetched(raws, full_listing=False, error=_describe(exc))
    return _Fetched(raws, full_listing=source.full_listing, error=None)


@dataclass(frozen=True)
class _Stored:
    outcome: SourceOutcome
    new_job_ids: AbstractSet[int]


def _store(session: Session, run_id: int, plan: _Plan, result: _Fetched) -> _Stored:
    key = source_key(plan.config)
    complete = result.error is None and result.full_listing
    ledger = plan.ledger
    outcome = SourceOutcome(
        plan.company.slug, key, fetched=len(result.raws), complete=complete, capped_queries=len(ledger.capped)
    )
    if ledger.capped:
        log.warning("%s: %d queries hit max_pages", key, len(ledger.capped))  # counts only
    new: set[int] = set()
    errors = [result.error] if result.error else []
    try:
        with session.begin_nested():
            jobs, outcome.stats = normalize_all(result.raws, source=key)
            sample: dict[str, Any] | None = result.raws[0].raw if result.raws else None
            new = repo.upsert_jobs(
                session,
                run_id=run_id,
                source_id=plan.source_id,
                jobs=jobs,
                complete=complete,
                raw_sample=sample,
            )
            if ledger.started:  # paged source: save which queries are now backfilled
                repo.save_backfilled(session, plan.source_id, ledger.updated_backfilled())
    except Exception as exc:  # a storage failure is one source's error, not the run's
        new = set()
        errors.append(f"store: {_describe(exc)}")
    for error in errors:
        with session.begin_nested():
            repo.record_dead_letter(session, run_id=run_id, source_id=plan.source_id, error=error)
    outcome.new = len(new)
    outcome.errors = len(errors)
    return _Stored(outcome, new)


def _describe(exc: BaseException) -> str:
    """Short, body-free error text for dead_letter (FetchError messages carry host and status only)."""
    if isinstance(exc, TimeoutError):
        return "TimeoutError: source took too long"
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]
