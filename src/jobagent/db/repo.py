"""Small repository functions over the job store.

Every function takes a Session and never commits: the caller owns the transaction, so one run
can be committed (or rolled back) as a unit.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, Executable, bindparam, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from jobagent.core import models as core
from jobagent.core.models import NormalizedJob, Platform
from jobagent.db.models import Company, CvProfile, DeadLetter, Job, Ranking, Run, Source

JobKey = tuple[str, str]  # (company slug, source job id): a job's identity across runs

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
            # Years and the description hash refresh only from listings that carried a description.
            years = (
                {
                    "min_yoe": stmt.excluded.min_yoe,
                    "max_yoe": stmt.excluded.max_yoe,
                    "description_hash": stmt.excluded.description_hash,
                }
                if refresh_years
                else {}
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
        "description_hash": description_hash(job.description_text),
        "url": str(job.url),
        "posted_at": job.posted_at,
        "first_seen_run_id": run_id,
        "last_seen_run_id": run_id,
    }


def _rowcount(session: Session, stmt: Executable) -> int:
    """Rows affected by an UPDATE/DELETE (session.execute is typed as Result; it is a CursorResult)."""
    return cast(CursorResult[Any], session.execute(stmt)).rowcount


def latest_cv_profile(session: Session) -> CvProfile | None:
    """The newest stored profile (highest id), or None before the first build."""
    return session.scalars(select(CvProfile).order_by(CvProfile.id.desc()).limit(1)).first()


def add_cv_profile(session: Session, *, key: str, model: str, profile_json: Mapping[str, Any]) -> int:
    """Store a new profile version and return its id (the profile version)."""
    row = CvProfile(hash=key, model=model, profile_json=dict(profile_json))
    session.add(row)
    session.flush()
    return row.id


# --- ranking -------------------------------------------------------------------------------------


def job_ids_for(session: Session, keys: Iterable[JobKey]) -> dict[JobKey, tuple[int, str]]:
    """(company slug, source job id) -> (job id, status) for the jobs that exist."""
    wanted = list(dict.fromkeys(keys))
    found: dict[JobKey, tuple[int, str]] = {}
    for start in range(0, len(wanted), _BATCH):
        chunk = wanted[start : start + _BATCH]
        rows = session.execute(
            select(Company.slug, Job.source_job_id, Job.id, Job.status)
            .join(Company, Company.id == Job.company_id)
            .where(tuple_(Company.slug, Job.source_job_id).in_(chunk))
        )
        for slug, source_job_id, job_id, status in rows:
            found[(slug, source_job_id)] = (job_id, status)
    return found


def description_hash(text: str | None) -> str | None:
    """sha256 of a plain-text description; None without one (such jobs are never merged as duplicates)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def save_descriptions(session: Session, texts: Mapping[int, str]) -> int:
    """Store description_text (and its hash) for these job ids (filter survivors only)."""
    if not texts:
        return 0
    table = Job.metadata.tables["job"]  # Core table: executemany UPDATE with bound parameters
    stmt = (
        update(table)
        .where(table.c.id == bindparam("job_id"))
        .values(description_text=bindparam("text"), description_hash=bindparam("digest"))
    )
    rows = [
        {"job_id": job_id, "text": text, "digest": description_hash(text)} for job_id, text in texts.items()
    ]
    session.execute(stmt, rows)
    return len(rows)


def ranked_job_ids(
    session: Session, job_ids: Iterable[int], effective_profile_hash: str, *, exclude_model: str
) -> set[int]:
    """Jobs already ranked for this effective profile, by any model except `exclude_model`
    (the rule fallback, which the LLM should replace on a later run)."""
    ids = list(set(job_ids))
    ranked: set[int] = set()
    for start in range(0, len(ids), _BATCH):
        ranked.update(
            session.scalars(
                select(Ranking.job_id).where(
                    Ranking.job_id.in_(ids[start : start + _BATCH]),
                    Ranking.effective_profile_hash == effective_profile_hash,
                    Ranking.model != exclude_model,
                )
            )
        )
    return ranked


def upsert_rankings(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert rankings; an existing (job, effective profile) row is replaced (e.g. rule -> LLM).

    Each row: job_id, profile_version, effective_profile_hash, model, score, fit, reason.
    """
    for start in range(0, len(rows), _BATCH):
        stmt = pg_insert(Ranking).values([dict(r) for r in rows[start : start + _BATCH]])
        stmt = stmt.on_conflict_do_update(
            index_elements=[Ranking.job_id, Ranking.effective_profile_hash],
            set_={
                "profile_version": stmt.excluded.profile_version,
                "model": stmt.excluded.model,
                "score": stmt.excluded.score,
                "fit": stmt.excluded.fit,
                "reason": stmt.excluded.reason,
                "ranked_at": func.now(),
            },
        )
        session.execute(stmt)
    return len(rows)


def set_run_cost(session: Session, run_id: int, cost_usd: Decimal) -> None:
    if _rowcount(session, update(Run).where(Run.id == run_id).values(cost_usd=cost_usd)) != 1:
        raise LookupError(f"run {run_id} not found")


# --- digest delivery (see digest/send.py) ---------------------------------------------------------

DIGEST_SENDING = "sending"
DIGEST_SENT = "sent"
DIGEST_FAILED = "failed"


def begin_digest(session: Session, run_id: int, job_ids: Iterable[int], rfc_message_id: str) -> None:
    """Record what this run is about to email, BEFORE sending (status "sending")."""
    values = {
        "emailed_job_ids": sorted(set(job_ids)),
        "digest_status": DIGEST_SENDING,
        "digest_rfc_message_id": rfc_message_id,
        "digest_attempted_at": func.now(),
    }
    if _rowcount(session, update(Run).where(Run.id == run_id).values(**values)) != 1:
        raise LookupError(f"run {run_id} not found")


def mark_digest_sent(session: Session, run_id: int, gmail_id: str) -> None:
    session.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(digest_status=DIGEST_SENT, digest_gmail_id=gmail_id, digest_sent_at=func.now())
    )


def mark_digest_failed(session: Session, run_id: int) -> None:
    session.execute(update(Run).where(Run.id == run_id).values(digest_status=DIGEST_FAILED))


def sending_runs(session: Session) -> list[Run]:
    """Runs whose send outcome is unknown (the process stopped mid-send): reconcile with Gmail."""
    return list(session.scalars(select(Run).where(Run.digest_status == DIGEST_SENDING).order_by(Run.id)))


def held_job_ids(session: Session) -> frozenset[int]:
    """Job ids never to put in a new digest: those sent, and those whose send is still unknown
    ("sending", until reconcile settles it). Only a "failed" run's jobs go out again."""
    statuses = (DIGEST_SENT, DIGEST_SENDING)
    ids = session.scalars(
        select(func.unnest(Run.emailed_job_ids)).where(Run.digest_status.in_(statuses)).distinct()
    )
    return frozenset(int(i) for i in ids)
