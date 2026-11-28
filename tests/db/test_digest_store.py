"""select_digest against the test database, and the description hash that duplicate merging uses."""

from datetime import UTC, date, datetime

from pydantic import HttpUrl
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from jobagent.core import models as core
from jobagent.core.models import NormalizedJob, RawJob
from jobagent.db import repo
from jobagent.db.models import Job
from jobagent.digest.select import EUROPE, INDIA, emailed_job_ids, select_digest
from jobagent.pipeline.normalize import normalize
from tests.db.conftest import needs_db

pytestmark = needs_db

HASH = "e" * 64
TODAY = date(2026, 11, 27)


def company(slug: str, tier: core.Tier = core.Tier.T1) -> core.Company:
    return core.Company(
        slug=slug,
        name=slug.title(),
        tier=tier,
        india_presence=True,
        europe_presence=True,
        us_transfer_path=True,
        sources=[core.SourceConfig(platform=core.Platform.GREENHOUSE, tenant=slug)],
    )


def job(slug: str, job_id: str, location: str = "Bengaluru, India", text: str | None = None) -> NormalizedJob:
    raw = RawJob(
        source_job_id=job_id,
        company_slug=slug,
        title="Senior Backend Engineer",
        location_raw=location,
        url=HttpUrl(f"https://example.com/{slug}/{job_id}"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={},
        has_description=True,
        description_text=text if text is not None else f"Posting {slug} {job_id}.",
    )
    return normalize(raw, source=f"greenhouse:{slug}")


def seed(session: Session, jobs: dict[str, list[NormalizedJob]]) -> dict[tuple[str, str], int]:
    """Store jobs per company; return (slug, source job id) -> job id."""
    companies = [company("acme"), company("other", core.Tier.OTHER)]
    sources = repo.sync_registry(session, companies)
    run_id = repo.start_run(session)
    for slug, items in jobs.items():
        repo.upsert_jobs(
            session, run_id=run_id, source_id=sources[(slug, "greenhouse", slug)], jobs=items, complete=False
        )
    keys = [(j.company_slug, j.source_job_id) for items in jobs.values() for j in items]
    return {key: job_id for key, (job_id, _) in repo.job_ids_for(session, keys).items()}


def rank(session: Session, job_id: int, score: int, fit: str, effective_hash: str = HASH) -> None:
    repo.upsert_rankings(
        session,
        [
            {
                "job_id": job_id,
                "profile_version": 1,
                "effective_profile_hash": effective_hash,
                "model": "gpt-5.4-mini",
                "score": score,
                "fit": fit,
                "reason": "Same stack.",
            }
        ],
    )


def sent_digest(session: Session, job_ids: list[int]) -> int:
    """A past run whose digest was sent with these jobs."""
    run_id = repo.start_run(session)
    repo.begin_digest(session, run_id, job_ids, f"<run-{run_id}@x>")
    repo.mark_digest_sent(session, run_id, f"gmail-{run_id}")
    return run_id


def ids_in(session: Session) -> set[int]:
    digest = select_digest(session, HASH, TODAY)
    return set(digest.job_ids) if digest else set()


def test_selects_ranked_tier1_high_and_medium(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1"), job("acme", "2"), job("acme", "3", "Dublin, Ireland")]})
    rank(session, ids[("acme", "1")], 88, "High")
    rank(session, ids[("acme", "2")], 30, "Low")
    rank(session, ids[("acme", "3")], 60, "Medium")
    digest = select_digest(session, HASH, TODAY)
    assert digest is not None
    assert {s.name: s.count for s in digest.sections} == {INDIA: 1, EUROPE: 1}
    assert digest.job_ids == {ids[("acme", "1")], ids[("acme", "3")]}
    assert digest.subject == "2 new: 1 India, 1 Europe"


def test_already_emailed_jobs_are_left_out(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1"), job("acme", "2")]})
    for key in (("acme", "1"), ("acme", "2")):
        rank(session, ids[key], 80, "High")
    sent_digest(session, [ids[("acme", "1")]])
    assert emailed_job_ids(session) == {ids[("acme", "1")]}
    assert ids_in(session) == {ids[("acme", "2")]}


def test_a_failed_runs_jobs_go_out_again(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1")]})
    rank(session, ids[("acme", "1")], 80, "High")
    run_id = repo.start_run(session)
    repo.begin_digest(session, run_id, [ids[("acme", "1")]], "<m@x>")
    repo.mark_digest_failed(session, run_id)
    assert ids_in(session) == {ids[("acme", "1")]}


def test_a_run_still_sending_holds_its_jobs_back(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1")]})
    rank(session, ids[("acme", "1")], 80, "High")
    repo.begin_digest(session, repo.start_run(session), [ids[("acme", "1")]], "<m@x>")  # outcome unknown
    assert select_digest(session, HASH, TODAY) is None


def test_everything_emailed_means_no_digest(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1")]})
    rank(session, ids[("acme", "1")], 80, "High")
    sent_digest(session, [ids[("acme", "1")]])
    assert select_digest(session, HASH, TODAY) is None


def test_only_the_current_effective_profile(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1")]})
    rank(session, ids[("acme", "1")], 80, "High", effective_hash="f" * 64)
    assert select_digest(session, HASH, TODAY) is None


def test_stale_jobs_and_other_tier_are_left_out(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1"), job("acme", "2")], "other": [job("other", "9")]})
    for key in ids:
        rank(session, ids[key], 85, "High")
    session.execute(update(Job).where(Job.id == ids[("acme", "2")]).values(status=repo.STALE))
    assert ids_in(session) == {ids[("acme", "1")]}


def test_upsert_stores_the_description_hash(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1", text="Same text"), job("acme", "2", text="")]})
    hashes = {i: h for i, h in session.execute(select(Job.id, Job.description_hash))}
    assert hashes[ids[("acme", "1")]] == repo.description_hash("Same text")
    assert hashes[ids[("acme", "2")]] is None


def test_identical_postings_merge_and_different_ones_do_not(session: Session) -> None:
    ids = seed(
        session,
        {
            "acme": [
                job("acme", "1", text="Identical posting."),
                job("acme", "2", text="Identical posting."),
                job("acme", "3", text="A different posting."),
            ]
        },
    )
    for key in ids:
        rank(session, ids[key], 80, "High")
    digest = select_digest(session, HASH, TODAY)
    assert digest is not None
    [india] = digest.sections
    assert sorted(len(line.job_ids) for line in india.lines) == [1, 2]
    assert len(digest.job_ids) == 3


def test_rows_with_text_but_no_stored_hash_still_merge(session: Session) -> None:
    ids = seed(session, {"acme": [job("acme", "1", text="Same."), job("acme", "2", text="Same.")]})
    repo.save_descriptions(session, {job_id: "Same." for job_id in ids.values()})  # survivors' text
    session.execute(update(Job).values(description_hash=None))  # rows stored before the hash existed
    for key in ids:
        rank(session, ids[key], 80, "High")
    digest = select_digest(session, HASH, TODAY)
    assert digest is not None
    assert [len(line.job_ids) for line in digest.sections[0].lines] == [2]
