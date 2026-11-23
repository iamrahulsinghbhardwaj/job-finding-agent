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

from jobagent.core.models import RawJob
from jobagent.db.models import DeadLetter, Job, Run, Source

ACTIVE = "active"
STALE = "stale"
_BATCH = 500  # rows per INSERT, well under Postgres' 65,535 bind-parameter limit
_MAX_ERROR = 500


def upsert_jobs(
    session: Session, *, run_id: int, source_id: int, jobs: Sequence[RawJob], complete: bool
) -> set[int]:
    """Insert or refresh jobs for one source; return the ids of jobs first seen in this run.

    Existing jobs get last_seen/last_seen_run_id refreshed, missed_runs reset and status set
    back to active. When `complete` is True the batch is the source's full listing, so active
    jobs of this source that are missing from it get missed_runs + 1 (see mark_stale).
    An empty complete batch is NOT counted: a source returning nothing is more likely broken
    than empty, and must not mark every job stale.
    """
    company_id = session.execute(select(Source.company_id).where(Source.id == source_id)).scalar_one()
    unique = list({job.source_job_id: job for job in reversed(jobs)}.values())[::-1]  # first wins
    seen: set[int] = set()
    new: set[int] = set()
    for start in range(0, len(unique), _BATCH):
        rows = [_row(job, company_id, source_id, run_id) for job in unique[start : start + _BATCH]]
        stmt = pg_insert(Job).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_job_company_id_source_job_id",
            set_={
                "title": stmt.excluded.title,
                "location_raw": stmt.excluded.location_raw,
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
    if unique:
        session.execute(update(Source).where(Source.id == source_id).values(raw_sample=unique[0].raw))
    if complete and seen:
        session.execute(
            update(Job)
            .where(Job.source_id == source_id, Job.status == ACTIVE, Job.id.not_in(seen))
            .values(missed_runs=Job.missed_runs + 1)
        )
    return new


def latest_seen_job_id(session: Session, source_id: int) -> str | None:
    """The newest stored job for a source: the stop_at_job_id for its next incremental crawl."""
    stmt = (
        select(Job.source_job_id)
        .where(Job.source_id == source_id)
        .order_by(Job.posted_at.desc().nulls_last(), Job.first_seen.desc(), Job.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


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


def _row(job: RawJob, company_id: int, source_id: int, run_id: int) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "source_id": source_id,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "location_raw": job.location_raw,
        "url": str(job.url),
        "posted_at": job.posted_at,
        "first_seen_run_id": run_id,
        "last_seen_run_id": run_id,
    }


def _rowcount(session: Session, stmt: Executable) -> int:
    """Rows affected by an UPDATE/DELETE (session.execute is typed as Result; it is a CursorResult)."""
    return cast(CursorResult[Any], session.execute(stmt)).rowcount
