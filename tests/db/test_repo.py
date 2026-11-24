"""Repository tests against a real Postgres (skipped unless TEST_DATABASE_URL is set)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, select, text
from sqlalchemy.orm import Session

from jobagent.core import models as core
from jobagent.core.models import RawJob
from jobagent.db import repo
from jobagent.db.models import Base, Company, DeadLetter, Job, Run, Source
from jobagent.pipeline.normalize import normalize
from tests.db.conftest import DbHandle, needs_db

pytestmark = needs_db


def make_company(session: Session, slug: str = "stripe") -> Source:
    company = Company(slug=slug, name=slug.title(), tier="T1")
    session.add(company)
    session.flush()
    source = Source(company_id=company.id, kind="platform", platform="greenhouse", tenant=slug)
    session.add(source)
    session.flush()
    return source


def raw(job_id: str, *, title: str = "Backend Engineer", day: int | None = 1, slug: str = "stripe") -> RawJob:
    return RawJob(
        source_job_id=job_id,
        company_slug=slug,
        title=title,
        location_raw="Bengaluru",
        url=f"https://example.com/jobs/{job_id}",  # pyright: ignore[reportArgumentType]
        posted_at=datetime(2026, 11, day, tzinfo=UTC) if day else None,
        raw={"id": job_id},
    )


def jobs_of(session: Session, source: Source) -> dict[str, Job]:
    rows = session.execute(select(Job).where(Job.source_id == source.id)).scalars()
    return {job.source_job_id: job for job in rows}


def upsert(
    session: Session, run_id: int, source: Source, jobs: list[RawJob], complete: bool = True
) -> set[int]:
    normalized = [normalize(job, source="test") for job in jobs]
    sample = jobs[0].raw if jobs else None
    return repo.upsert_jobs(
        session, run_id=run_id, source_id=source.id, jobs=normalized, complete=complete, raw_sample=sample
    )


# --- upsert_jobs -------------------------------------------------------------


def test_new_jobs_are_returned(session: Session) -> None:
    source = make_company(session)
    run = repo.start_run(session)
    new = upsert(session, run, source, [raw("1"), raw("2"), raw("3")])
    stored = jobs_of(session, source)
    assert new == {job.id for job in stored.values()}
    assert len(new) == 3
    assert all(job.first_seen_run_id == run for job in stored.values())


def test_second_run_returns_nothing_new_and_refreshes(session: Session) -> None:
    source = make_company(session)
    run1 = repo.start_run(session)
    upsert(session, run1, source, [raw("1"), raw("2")])
    run2 = repo.start_run(session)
    new = upsert(session, run2, source, [raw("1", title="Senior Backend Engineer"), raw("2"), raw("3")])
    session.expire_all()
    stored = jobs_of(session, source)
    assert new == {stored["3"].id}  # only the genuinely new job
    assert stored["1"].title == "Senior Backend Engineer"  # refreshed
    assert stored["1"].first_seen_run_id == run1  # first seen is never overwritten
    assert stored["1"].last_seen_run_id == run2


def test_duplicate_ids_in_one_batch(session: Session) -> None:
    source = make_company(session)
    run = repo.start_run(session)
    new = upsert(session, run, source, [raw("1", title="First"), raw("1", title="Second")])
    assert len(new) == 1
    assert jobs_of(session, source)["1"].title == "First"


def test_same_job_id_at_two_companies(session: Session) -> None:
    stripe = make_company(session, "stripe")
    plaid = make_company(session, "plaid")
    run = repo.start_run(session)
    assert len(upsert(session, run, stripe, [raw("42")])) == 1
    assert len(upsert(session, run, plaid, [raw("42", slug="plaid")])) == 1


def test_missing_posted_at_keeps_previous_value(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("1", day=5)])
    upsert(session, repo.start_run(session), source, [raw("1", day=None)])
    session.expire_all()
    assert jobs_of(session, source)["1"].posted_at == datetime(2026, 11, 5, tzinfo=UTC)


def test_raw_sample_stored_on_source(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("7"), raw("8")])
    session.refresh(source)
    assert source.raw_sample == {"id": "7"}


def test_normalized_location_is_stored(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("1")])  # location "Bengaluru"
    stored = jobs_of(session, source)["1"]
    assert (stored.country, stored.city) == ("IN", "Bengaluru")


def test_large_batch_is_chunked(session: Session) -> None:
    source = make_company(session)
    jobs = [raw(str(i)) for i in range(1, 1201)]
    assert len(upsert(session, repo.start_run(session), source, jobs)) == 1200


# --- known_job_ids ---------------------------------------------------------------


def test_known_job_ids(session: Session) -> None:
    source = make_company(session)
    other = make_company(session, "plaid")
    assert repo.known_job_ids(session, source.id) == frozenset()
    upsert(session, repo.start_run(session), source, [raw("1"), raw("2")])
    upsert(session, repo.start_run(session), other, [raw("9", slug="plaid")])
    assert repo.known_job_ids(session, source.id) == {"1", "2"}  # only this source's jobs


# --- sync_registry --------------------------------------------------------------


def registry_company(**changes: Any) -> core.Company:
    data: dict[str, Any] = {
        "slug": "amazon",
        "name": "Amazon",
        "tier": "T1",
        "india_presence": True,
        "europe_presence": True,
        "us_transfer_path": True,
        "sources": [
            {
                "platform": "amazon",
                "tenant": "amazon.jobs",
                "extra": {"categories": ["software-development"]},
            },
            {"platform": "greenhouse", "tenant": "amazon-gh"},
        ],
    }
    return core.Company.model_validate(data | changes)


def test_sync_registry_inserts_companies_and_sources(session: Session) -> None:
    ids = repo.sync_registry(session, [registry_company()])
    assert set(ids) == {("amazon", "amazon", "amazon.jobs"), ("amazon", "greenhouse", "amazon-gh")}
    amazon = session.get(Source, ids[("amazon", "amazon", "amazon.jobs")])
    assert amazon is not None
    assert (amazon.kind, amazon.status, amazon.tenant_config) == (
        "custom",
        "active",
        {"categories": ["software-development"]},
    )
    greenhouse = session.get(Source, ids[("amazon", "greenhouse", "amazon-gh")])
    assert greenhouse is not None
    assert greenhouse.kind == "platform"
    company = session.execute(select(Company).where(Company.slug == "amazon")).scalar_one()
    assert (company.tier, company.source_list, company.india_presence) == ("T1", "registry", True)


def test_sync_registry_is_idempotent_and_registry_wins(session: Session) -> None:
    first = repo.sync_registry(session, [registry_company()])
    blocked = registry_company(
        name="Amazon.com",
        tier="OTHER",
        sources=[
            {
                "platform": "amazon",
                "tenant": "amazon.jobs",
                "extra": {"categories": ["software-development"]},
                "status": "blocked",
                "blocked_reason": "403 on first request",
            }
        ],
    )
    second = repo.sync_registry(session, [blocked])
    key = ("amazon", "amazon", "amazon.jobs")
    assert second[key] == first[key]  # same row, updated in place
    session.expire_all()
    source = session.get(Source, second[key])
    assert source is not None
    assert (source.status, source.blocked_reason) == ("blocked", "403 on first request")
    company = session.execute(select(Company).where(Company.slug == "amazon")).scalar_one()
    assert (company.name, company.tier) == ("Amazon.com", "OTHER")
    # the source dropped from the YAML is kept (history), not deleted
    assert session.get(Source, first[("amazon", "greenhouse", "amazon-gh")]) is not None


# --- staleness -----------------------------------------------------------------


def test_job_goes_stale_after_three_complete_misses(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("keep"), raw("gone")])
    for _ in range(3):
        upsert(session, repo.start_run(session), source, [raw("keep")])
    assert repo.mark_stale(session, missed_runs=3) == 1
    session.expire_all()
    stored = jobs_of(session, source)
    assert (stored["gone"].status, stored["gone"].missed_runs) == ("stale", 3)
    assert (stored["keep"].status, stored["keep"].missed_runs) == ("active", 0)


def test_two_misses_are_not_stale(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("keep"), raw("gone")])
    for _ in range(2):
        upsert(session, repo.start_run(session), source, [raw("keep")])
    assert repo.mark_stale(session) == 0


def test_incremental_crawls_do_not_count_misses(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("old"), raw("older")])
    for _ in range(5):
        upsert(session, repo.start_run(session), source, [raw("new")], complete=False)
    assert repo.mark_stale(session) == 0
    assert jobs_of(session, source)["old"].missed_runs == 0


def test_empty_complete_batch_is_not_counted(session: Session) -> None:
    # A source that suddenly returns nothing is more likely broken than empty.
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("1")])
    for _ in range(5):
        upsert(session, repo.start_run(session), source, [])
    assert repo.mark_stale(session) == 0


def test_stale_job_is_revived_when_seen_again(session: Session) -> None:
    source = make_company(session)
    upsert(session, repo.start_run(session), source, [raw("a"), raw("b")])
    for _ in range(3):
        upsert(session, repo.start_run(session), source, [raw("a")])
    repo.mark_stale(session)
    run = repo.start_run(session)
    assert upsert(session, run, source, [raw("a"), raw("b")]) == set()  # not new: seen before
    session.expire_all()
    assert (jobs_of(session, source)["b"].status, jobs_of(session, source)["b"].missed_runs) == ("active", 0)


def test_misses_only_count_for_that_source(session: Session) -> None:
    stripe = make_company(session, "stripe")
    plaid = make_company(session, "plaid")
    upsert(session, repo.start_run(session), plaid, [raw("p1", slug="plaid")])
    for _ in range(3):
        upsert(session, repo.start_run(session), stripe, [raw("s1")])
    assert repo.mark_stale(session) == 0


# --- runs & dead letters ---------------------------------------------------------


def test_run_lifecycle(session: Session) -> None:
    run_id = repo.start_run(session)
    counts: dict[str, dict[str, int]] = {"greenhouse:stripe": {"fetched": 20, "new": 3}}
    repo.finish_run(session, run_id, counts, emailed_job_ids=[5, 3, 5])
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert run.finished_at is not None
    assert run.per_source_counts == counts
    assert run.emailed_job_ids == [3, 5]
    with pytest.raises(LookupError):
        repo.finish_run(session, 999_999, {})


def test_dead_letter(session: Session) -> None:
    source = make_company(session)
    run_id = repo.start_run(session)
    letter_id = repo.record_dead_letter(session, run_id=run_id, source_id=source.id, error="HTTP 503 " * 200)
    letter = session.get(DeadLetter, letter_id)
    assert letter is not None
    assert letter.error.startswith("HTTP 503")
    assert len(letter.error) == 500  # truncated


# --- migration ---------------------------------------------------------------------


def test_migration_matches_models(test_db: DbHandle) -> None:
    schema = test_db.schema
    expected = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(expected, schema=schema)

    def include(name: str | None, type_: str, parent: Any) -> bool:
        if type_ == "schema":
            return name == schema
        return not (type_ == "table" and name == "alembic_version")

    with test_db.engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"include_schemas": True, "include_name": include, "compare_type": True}
        )
        diff = compare_metadata(context, expected)
    assert diff == []


def test_migration_created_version_row(test_db: DbHandle) -> None:
    with test_db.engine.connect() as conn:
        query = text(f'SELECT version_num FROM "{test_db.schema}".alembic_version')
        version = conn.execute(query).scalar_one()
    assert version == "0002"
