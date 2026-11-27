"""rank_survivors against the test database: what gets ranked, what is stored, what is skipped."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import HttpUrl
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from jobagent.core import models as core
from jobagent.core.llm import LlmLedger
from jobagent.core.models import JobDetail, NormalizedJob, RawJob
from jobagent.db import repo
from jobagent.db.models import Job, Ranking, Run
from jobagent.pipeline.normalize import normalize
from jobagent.pipeline.profile import ProfileSync
from jobagent.pipeline.profile_schema import CandidateProfile
from jobagent.pipeline.rank import RULE_MODEL, RankResult, RankRun, rank_survivors
from tests.db.conftest import needs_db
from tests.fakes import FakeSource
from tests.llm_fakes import FakeOpenAI, WordTokenizer, completion

pytestmark = needs_db

MODEL = "gpt-5.4-nano"
SOURCE = "greenhouse:stripe"
SECRET_TEXT = "PRIVATE-DESCRIPTION-TEXT"


def company() -> core.Company:
    return core.Company(
        slug="stripe",
        name="Stripe",
        tier=core.Tier.T1,
        india_presence=True,
        europe_presence=True,
        us_transfer_path=False,
        sources=[core.SourceConfig(platform=core.Platform.GREENHOUSE, tenant="stripe")],
    )


DESCRIPTION = f"Python on Kafka. {SECRET_TEXT}"


def job(job_id: str, description: str = DESCRIPTION, min_yoe: int | None = 4) -> NormalizedJob:
    raw = RawJob(
        source_job_id=job_id,
        company_slug="stripe",
        title="Senior Backend Engineer",
        location_raw="Bengaluru, India",
        url=HttpUrl(f"https://example.com/jobs/{job_id}"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={},
        has_description=bool(description),
        description_text=description,
        min_yoe=min_yoe,
    )
    return normalize(raw, source=SOURCE)


PROFILE = CandidateProfile(
    years_experience=4.4,
    seniority="mid",
    primary_stack=["Python", "Kafka"],
    secondary_skills=[],
    domains=["logistics"],
    notable_projects=[],
    target_titles=["Backend Engineer"],
    roles=[],
)


def sync(version: int = 1, effective_hash: str = "e" * 64) -> ProfileSync:
    return ProfileSync(version, False, None, PROFILE, (), effective_hash)


def answer(score: int = 82) -> object:
    return completion(RankResult(score=score, fit="High", reason="Same stack."))


def store(session: Session, jobs: list[NormalizedJob]) -> int:
    source_ids = repo.sync_registry(session, [company()])
    run_id = repo.start_run(session)
    repo.upsert_jobs(
        session,
        run_id=run_id,
        source_id=source_ids[("stripe", "greenhouse", "stripe")],
        jobs=jobs,
        complete=False,
    )
    return run_id


async def rank(
    session: Session,
    jobs: list[NormalizedJob],
    fake: FakeOpenAI,
    run_id: int,
    *,
    profile: ProfileSync | None = None,
    sources: dict | None = None,
    budget: int = 10_000_000,
) -> RankRun:
    return await rank_survivors(
        session,
        jobs,
        {"stripe": company()},
        sources=sources or {},
        client=fake.client,
        profile=profile or sync(),
        model=MODEL,
        tokenizer=WordTokenizer(),
        token_budget=budget,
        ledger=LlmLedger(),
        run_id=run_id,
    )


def rankings(session: Session) -> list[Ranking]:
    return list(session.scalars(select(Ranking).order_by(Ranking.job_id, Ranking.id)))


async def test_rows_carry_model_profile_version_and_effective_hash(session: Session) -> None:
    jobs = [job("1"), job("2")]
    run_id = store(session, jobs)
    result = await rank(session, jobs, FakeOpenAI(answer(82), answer(64)), run_id)

    rows = rankings(session)
    assert [(r.model, r.profile_version, r.effective_profile_hash) for r in rows] == [
        (MODEL, 1, "e" * 64)
    ] * 2
    assert sorted(r.score for r in rows) == [64, 82]
    assert {r.fit for r in rows} == {"High", "Medium"}
    assert result.report.llm == 2
    cost = session.scalar(select(Run.cost_usd).where(Run.id == run_id))
    assert cost is not None
    assert cost > Decimal(0)


async def test_survivor_descriptions_are_stored(session: Session) -> None:
    jobs = [job("1"), job("2", description="")]
    run_id = store(session, jobs)
    await rank(session, jobs, FakeOpenAI(answer(), answer()), run_id)
    texts = {sid: text for sid, text in session.execute(select(Job.source_job_id, Job.description_text))}
    assert texts == {"1": DESCRIPTION, "2": None}
    hashes = {sid: h for sid, h in session.execute(select(Job.source_job_id, Job.description_hash))}
    assert hashes == {"1": repo.description_hash(DESCRIPTION), "2": None}


async def test_already_ranked_jobs_make_no_llm_call(session: Session) -> None:
    jobs = [job("1")]
    run_id = store(session, jobs)
    await rank(session, jobs, FakeOpenAI(answer()), run_id)
    again = FakeOpenAI()  # no answers queued: any call would fail
    result = await rank(session, jobs, again, run_id)
    assert again.requests == []
    assert result.report.candidates == 0
    assert len(rankings(session)) == 1


async def test_rule_rows_are_replaced_by_the_llm_next_run(session: Session) -> None:
    jobs = [job("1")]
    run_id = store(session, jobs)
    first = await rank(session, jobs, FakeOpenAI(), run_id, budget=10)  # budget: rule score
    assert [r.model for r in rankings(session)] == [RULE_MODEL]
    assert first.report.fallback["budget"] == 1

    second = await rank(session, jobs, FakeOpenAI(answer(77)), run_id)
    rows = rankings(session)
    assert [(r.model, r.score) for r in rows] == [(MODEL, 77)]  # replaced, not added
    assert second.report.llm == 1


async def test_new_effective_profile_reranks(session: Session) -> None:
    jobs = [job("1")]
    run_id = store(session, jobs)
    await rank(session, jobs, FakeOpenAI(answer(60)), run_id)
    await rank(session, jobs, FakeOpenAI(answer(80)), run_id, profile=sync(1, "f" * 64))  # overrides changed
    rows = rankings(session)
    assert [(r.effective_profile_hash[0], r.score) for r in rows] == [("e", 60), ("f", 80)]


async def test_stale_jobs_are_skipped(session: Session) -> None:
    jobs = [job("1"), job("2")]
    run_id = store(session, jobs)
    session.execute(update(Job).where(Job.source_job_id == "2").values(status=repo.STALE))
    fake = FakeOpenAI(answer())
    result = await rank(session, jobs, fake, run_id)
    assert result.report.skipped_stale == 1
    assert len(fake.requests) == 1
    assert len(rankings(session)) == 1


async def test_listing_without_description_fetches_the_posting(session: Session) -> None:
    jobs = [job("1", description="", min_yoe=None), job("2", description="", min_yoe=None)]
    run_id = store(session, jobs)
    source = FakeSource(
        [],
        details={
            "1": JobDetail(source_job_id="1", description_text="Full posting: Python on Kafka.", min_yoe=4),
            "2": JobDetail(source_job_id="2", description_text="Full posting: 12+ years.", min_yoe=12),
        },
    )
    fake = FakeOpenAI(answer())
    result = await rank(session, jobs, fake, run_id, sources={SOURCE: source})
    assert result.report.details_fetched == 2
    assert result.report.refiltered_out == 1  # job 2: 12+ years is over the Tier 1 range
    assert len(fake.requests) == 1
    assert "Full posting: Python on Kafka." in str(fake.requests[0]["messages"][2]["content"])
    texts = {sid: text for sid, text in session.execute(select(Job.source_job_id, Job.description_text))}
    assert texts["1"] == "Full posting: Python on Kafka."


async def test_logs_counts_never_job_text(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    jobs = [job("1")]
    run_id = store(session, jobs)
    with caplog.at_level(logging.DEBUG):
        await rank(session, jobs, FakeOpenAI(answer()), run_id)
    assert "rank: 1 candidates, 1 by LLM, 0 rule (none), cached" in caplog.text
    assert "no prompt caching" in caplog.text  # the fake tokenizer's prefix is under 1,024
    for secret in (SECRET_TEXT, "Senior Backend Engineer", "Same stack", "logistics"):
        assert secret not in caplog.text
