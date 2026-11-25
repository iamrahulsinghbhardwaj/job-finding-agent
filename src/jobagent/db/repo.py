"""Small repository functions over the job store.

Every function takes a Session and never commits: the caller owns the transaction, so one run
can be committed (or rolled back) as a unit.
"""

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, Executable, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jobagent.core import models as core
from jobagent.core.models import NormalizedJob, Platform
from jobagent.db.models import Company, DeadLetter, Job, Run, Source

ACTIVE = "active"
STALE = "stale"
_BATCH = 500  # rows per INSERT, well under Postgres' 65,535 bind-parameter limit
_MAX_ERROR = 500
_CUSTOM_PLATFORMS = frozenset({Platform.AMAZON, Platform.CUSTOM})  # in-house sites, one company each


def upsert_jobs(
    session: Session,
    *,
    run_id: int,
    source_id: int,
    jobs: Sequence[NormalizedJob],
    complete: bool,
    raw_sample: Mapping[str, Any] | None = None,
) -> set[int]:
    """Insert or refresh jobs for one source; return the ids of jobs first seen in this run.

    Existing jobs get last_seen/last_seen_run_id refreshed, missed_runs reset and status set
    back to active. When `complete` is True the batch is the source's full listing, so active
    jobs of this source that are missing from it get missed_runs + 1 (see mark_stale).
    An empty complete batch is NOT counted: a source returning nothing is more likely broken
    than empty, and must not mark every job stale.
    `raw_sample` (one listing as the source sent it) is kept on the Source for debugging.
    """
    company_id = session.execute(select(Source.company_id).where(Source.id == source_id)).scalar_one()
    unique = list({job.source_job_id: job for job in reversed(jobs)}.values())[::-1]  # first wins
    seen: set[int] = set()
    new: set[int] = set()
    # Years are refreshed only from listings that carried a description; a listing without one
    # says nothing about them, so the stored values stay. Hence two statements (set_ differs).
    with_text = [job for job in unique if job.has_description]
    without_text = [job for job in unique if not job.has_description]
    for group, refresh_years in ((with_text, True), (without_text, False)):
        for start in range(0, len(group), _BATCH):
            rows = [_row(job, company_id, source_id, run_id) for job in group[start : start + _BATCH]]
            stmt = pg_insert(Job).values(rows)
            years = (
                {"min_yoe": stmt.excluded.min_yoe, "max_yoe": stmt.excluded.max_yoe} if refresh_years else {}
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_job_company_id_source_job_id",
                set_={
                    "title": stmt.excluded.title,
                    "location_raw": stmt.excluded.location_raw,
                    "country": stmt.excluded.country,
                    "city": stmt.excluded.city,
                    **years,
                    "url": stmt.excluded.url,
                    "posted_at": func.coalesce(stmt.excluded.posted_at, Job.posted_at),
                    "last_seen": func.now(),
                    "last_seen_run_id": run_id,
                    "missed_runs": 0,
                    "status": ACTIVE,
                },
            ).returning(Job.id, Job.first_seen_run_id)
            for job_id, first_seen_run_id in session.execute(stmt):
                seen.add(job_id)
                if first_seen_run_id == run_id:
                    new.add(job_id)
    if raw_sample is not None:
        session.execute(update(Source).where(Source.id == source_id).values(raw_sample=dict(raw_sample)))
    if complete and seen:
        session.execute(
            update(Job)
            .where(Job.source_id == source_id, Job.status == ACTIVE, Job.id.not_in(seen))
            .values(missed_runs=Job.missed_runs + 1)
        )
    return new


def known_job_ids(session: Session, source_id: int) -> frozenset[str]:
    """Every stored job id of a source (any status): paged sources stop at a page of these."""
    rows = session.execute(select(Job.source_job_id).where(Job.source_id == source_id)).scalars()
    return frozenset(rows)


def backfilled_queries(session: Session, source_id: int) -> frozenset[str]:
    """Query keys of a paged source that have completed one full pass (see QueryLedger)."""
    state = session.execute(select(Source.crawl_state).where(Source.id == source_id)).scalar_one()
    return frozenset(state.get("backfilled", []))


def save_backfilled(session: Session, source_id: int, keys: Iterable[str]) -> None:
    """Replace the set of backfilled query keys (other crawl_state fields are kept)."""
    source = session.get_one(Source, source_id)
    source.crawl_state = {**source.crawl_state, "backfilled": sorted(set(keys))}
    session.flush()


SourceRef = tuple[str, str, str]  # (company slug, platform, tenant)


def sync_registry(session: Session, companies: Iterable[core.Company]) -> dict[SourceRef, int]:
    """Upsert registry companies and their sources; return source ids by (slug, platform, tenant).

    Registry fields (name, tier, flags, source config, status) always win over the DB copy.
    Nothing is deleted: a company or source removed from the YAML keeps its rows and history.
    """
    ids: dict[SourceRef, int] = {}
    for company in companies:
        stmt = pg_insert(Company).values(
            slug=company.slug,
            name=company.name,
            tier=company.tier.value,
            source_list="registry",
            india_presence=company.india_presence,
            europe_presence=company.europe_presence,
            us_transfer_path=company.us_transfer_path,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_company_slug",
            set_={
                "name": stmt.excluded.name,
                "tier": stmt.excluded.tier,
                "india_presence": stmt.excluded.india_presence,
                "europe_presence": stmt.excluded.europe_presence,
                "us_transfer_path": stmt.excluded.us_transfer_path,
            },
        ).returning(Company.id)
        company_id = session.execute(stmt).scalar_one()
        for config in company.sources:
            source_stmt = pg_insert(Source).values(
                company_id=company_id,
                kind="custom" if config.platform in _CUSTOM_PLATFORMS else "platform",
                platform=config.platform.value,
                tenant=config.tenant,
                tenant_config=dict(config.extra),
                status=config.status.value,
                blocked_reason=config.blocked_reason,
            )
            source_stmt = source_stmt.on_conflict_do_update(
                constraint="uq_source_company_id_platform_tenant",
                set_={
                    "kind": source_stmt.excluded.kind,
                    "tenant_config": source_stmt.excluded.tenant_config,
                    "status": source_stmt.excluded.status,
                    "blocked_reason": source_stmt.excluded.blocked_reason,
                },
            ).returning(Source.id)
            ids[(company.slug, config.platform.value, config.tenant)] = session.execute(
                source_stmt
            ).scalar_one()
    return ids


def start_run(session: Session) -> int:
    run = Run()
    session.add(run)
    session.flush()
    return run.id


def finish_run(
    session: Session,
    run_id: int,
    counts: Mapping[str, Mapping[str, int]],
    *,
    emailed_job_ids: Iterable[int] = (),
    cost_usd: Decimal | None = None,
) -> None:
    """Close a run with per-source counts (numbers only: logs and this table hold no job text)."""
    values: dict[str, Any] = {
        "finished_at": func.now(),
        "per_source_counts": {source: dict(c) for source, c in counts.items()},
        "emailed_job_ids": sorted(set(emailed_job_ids)),
    }
    if cost_usd is not None:
        values["cost_usd"] = cost_usd
    if _rowcount(session, update(Run).where(Run.id == run_id).values(**values)) != 1:
        raise LookupError(f"run {run_id} not found")


def record_dead_letter(session: Session, *, run_id: int, source_id: int, error: str) -> int:
    """Store a failed fetch for later inspection. `error` must be a short message, never a body."""
    letter = DeadLetter(run_id=run_id, source_id=source_id, error=error[:_MAX_ERROR])
    session.add(letter)
    session.flush()
    return letter.id


def mark_stale(session: Session, missed_runs: int = 3) -> int:
    """Mark active jobs missing from `missed_runs` complete crawls in a row as stale; return count."""
    return _rowcount(
        session, update(Job).where(Job.status == ACTIVE, Job.missed_runs >= missed_runs).values(status=STALE)
    )


def _row(job: NormalizedJob, company_id: int, source_id: int, run_id: int) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "source_id": source_id,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "location_raw": job.location_raw,
        "country": job.country,
        "city": job.city,
        "min_yoe": job.min_yoe,
        "max_yoe": job.max_yoe,
        "url": str(job.url),
        "posted_at": job.posted_at,
        "first_seen_run_id": run_id,
        "last_seen_run_id": run_id,
    }


def _rowcount(session: Session, stmt: Executable) -> int:
    """Rows affected by an UPDATE/DELETE (session.execute is typed as Result; it is a CursorResult)."""
    return cast(CursorResult[Any], session.execute(stmt)).rowcount
