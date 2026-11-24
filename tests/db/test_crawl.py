"""crawl() against a real Postgres with FakeSource (skipped unless TEST_DATABASE_URL is set)."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobagent.core.contracts import JobSource, QueryLedger
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import (
    Company,
    HealthReport,
    JobDetail,
    Platform,
    RawJob,
    SearchSpec,
    SourceConfig,
    Tier,
)
from jobagent.db import repo
from jobagent.db.models import DeadLetter, Job, Run, Source
from jobagent.pipeline import crawl as crawl_module
from jobagent.pipeline.crawl import CrawlResult, crawl
from tests.adapters.conftest import load_fixture
from tests.adapters.test_amazon import ALL_QUERIES, IND_SW, SEARCH_URL, FakeAmazon, catalogues
from tests.db.conftest import needs_db
from tests.fakes import FakeSource

pytestmark = needs_db

SPEC = SearchSpec(keywords=[], countries=["IN", "IE"], max_pages=3)
SPECS = dict.fromkeys(Tier, SPEC)


def raw(job_id: str, slug: str) -> RawJob:
    return RawJob(
        source_job_id=job_id,
        company_slug=slug,
        title="SDE II , Payments",
        location_raw="Hyderabad, TS, IN",
        url=f"https://example.com/{slug}/{job_id}",  # pyright: ignore[reportArgumentType]
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={"id": job_id},
    )


def jobs_for(slug: str, *ids: str) -> list[RawJob]:
    return [raw(i, slug) for i in ids]


def company(slug: str, *, platform: str = "greenhouse", **source: Any) -> Company:
    return Company.model_validate(
        {
            "slug": slug,
            "name": slug.title(),
            "tier": "T1",
            "india_presence": True,
            "europe_presence": True,
            "us_transfer_path": False,
            "sources": [{"platform": platform, "tenant": slug, **source}],
        }
    )


class Builder:
    """build_source stand-in: hands out FakeSources by company slug and records who was built."""

    def __init__(self, sources: dict[str, JobSource]) -> None:
        self.sources = sources
        self.built: list[str] = []

    def __call__(self, company: Company, config: SourceConfig, http: PoliteClient) -> JobSource:
        self.built.append(company.slug)
        return self.sources[company.slug]


class HangingSource:
    full_listing = True

    async def search(
        self, spec: SearchSpec, known_job_ids: AbstractSet[str], ledger: QueryLedger | None = None
    ) -> AsyncIterator[RawJob]:
        await asyncio.sleep(60)
        yield raw("never", "slow")

    async def get_details(self, job_id: str) -> JobDetail:
        raise NotImplementedError

    async def health_check(self) -> HealthReport:
        raise NotImplementedError


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async def no_sleep(_: float) -> None:
        return None

    async with PoliteClient(min_delay=0, sleep=no_sleep) as client:
        yield client


async def run(
    session: Session, companies: Sequence[Company], builder: Builder, http: PoliteClient, **kwargs: Any
) -> CrawlResult:
    return await crawl(session, companies, SPECS, http=http, build=builder, **kwargs)


def stored(session: Session, slug: str) -> dict[str, Job]:
    rows = session.execute(select(Job).join(Source).where(Source.tenant == slug)).scalars()
    return {job.source_job_id: job for job in rows}


def backfilled(session: Session, slug: str) -> set[str]:
    source = session.execute(select(Source).where(Source.tenant == slug)).scalar_one()
    session.refresh(source)
    return set(source.crawl_state.get("backfilled", []))


def amazon_company(**source: Any) -> Company:
    return company(
        "amazon",
        platform=Platform.AMAZON.value,
        extra={"categories": ["software-development", "machine-learning-science"]},
        **source,
    )


def outcome(result: CrawlResult, slug: str) -> dict[str, int]:
    (match,) = [o for o in result.outcomes if o.company_slug == slug]
    return match.counts()


# --- isolation -----------------------------------------------------------------


async def test_one_failing_source_never_stops_the_others(session: Session, http: PoliteClient) -> None:
    companies = [company(s) for s in ("good", "blocked403", "flaky", "buggy", "slow")]
    builder = Builder(
        {
            "good": FakeSource(jobs_for("good", "1", "2", "3")),
            "blocked403": FakeSource(
                [], fail_after=0, error=BlockedError("403 from x", host="x", status=403)
            ),
            "flaky": FakeSource(
                jobs_for("flaky", "a", "b"), fail_after=1, error=FetchError("retries exhausted", host="x")
            ),
            "buggy": FakeSource([], fail_after=0, error=RuntimeError("adapter bug")),
            "slow": HangingSource(),
        }
    )
    result = await run(session, companies, builder, http, source_timeout=0.05)

    assert set(stored(session, "good")) == {"1", "2", "3"}
    assert set(stored(session, "flaky")) == {"a"}  # jobs yielded before the failure are kept
    assert outcome(result, "good") == {
        "fetched": 3,
        "new": 3,
        "errors": 0,
        "capped_queries": 0,
        "normalized": 3,
        "unknown_location": 0,
        "remote": 0,
    }
    for slug in ("blocked403", "flaky", "buggy", "slow"):
        assert outcome(result, slug)["errors"] == 1

    letters = session.execute(select(DeadLetter).where(DeadLetter.run_id == result.run_id)).scalars().all()
    messages = sorted(letter.error for letter in letters)
    assert messages == [
        "BlockedError: 403 from x",
        "FetchError: retries exhausted",
        "RuntimeError: adapter bug",
        "TimeoutError: source took too long",
    ]
    assert len(result.new_job_ids) == 4  # 3 good + 1 flaky


async def test_counts_are_saved_on_the_run_row(session: Session, http: PoliteClient) -> None:
    companies = [company("good"), company("bad")]
    builder = Builder(
        {
            "good": FakeSource(jobs_for("good", "1", "2")),
            "bad": FakeSource([], fail_after=0, error=RuntimeError("x")),
        }
    )
    result = await run(session, companies, builder, http)
    row = session.get(Run, result.run_id)
    assert row is not None
    assert row.finished_at is not None
    assert row.per_source_counts["greenhouse:good"]["new"] == 2
    assert row.per_source_counts["greenhouse:bad"]["errors"] == 1


async def test_a_storage_failure_is_isolated_too(
    session: Session, http: PoliteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_upsert = repo.upsert_jobs

    def upsert(session: Session, **kwargs: Any) -> set[int]:
        if kwargs["jobs"] and kwargs["jobs"][0].company_slug == "broken":
            raise ValueError("db says no")
        return real_upsert(session, **kwargs)

    monkeypatch.setattr(crawl_module.repo, "upsert_jobs", upsert)
    companies = [company("broken"), company("good")]
    builder = Builder(
        {"broken": FakeSource(jobs_for("broken", "1")), "good": FakeSource(jobs_for("good", "1"))}
    )
    result = await run(session, companies, builder, http)
    assert outcome(result, "broken")["errors"] == 1
    assert stored(session, "broken") == {}
    assert set(stored(session, "good")) == {"1"}


async def test_blocked_sources_are_never_built(session: Session, http: PoliteClient) -> None:
    blocked = company("walled", status="blocked", blocked_reason="429 + HTML, 2026-11-23")
    builder = Builder({"good": FakeSource(jobs_for("good", "1"))})
    result = await run(session, [blocked, company("good")], builder, http)
    assert builder.built == ["good"]
    assert outcome(result, "walled") == {"blocked": 1}


# --- new jobs across runs ----------------------------------------------------------


async def test_second_run_with_no_changes_yields_zero_new(session: Session, http: PoliteClient) -> None:
    fake = FakeSource(jobs_for("stripe", "1", "2", "3"))
    builder = Builder({"stripe": fake})
    first = await run(session, [company("stripe")], builder, http)
    second = await run(session, [company("stripe")], builder, http)
    assert len(first.new_job_ids) == 3
    assert second.new_job_ids == frozenset()
    assert outcome(second, "stripe")["fetched"] == 3
    assert fake.known_seen == [frozenset(), frozenset({"1", "2", "3"})]  # known ids come from the DB

    fake.jobs.insert(0, raw("4", "stripe"))
    third = await run(session, [company("stripe")], builder, http)
    assert third.new_job_ids == {stored(session, "stripe")["4"].id}


async def test_normalized_fields_are_stored(session: Session, http: PoliteClient) -> None:
    await run(session, [company("stripe")], Builder({"stripe": FakeSource(jobs_for("stripe", "1"))}), http)
    job = stored(session, "stripe")["1"]
    assert (job.title, job.country, job.city) == ("SDE II, Payments", "IN", "Hyderabad")


# --- staleness: only complete listings count a missed run -------------------------


async def _missed_after_second_run(
    session: Session, http: PoliteClient, second: FakeSource
) -> dict[str, int]:
    await run(
        session, [company("stripe")], Builder({"stripe": FakeSource(jobs_for("stripe", "1", "2", "3"))}), http
    )
    await run(session, [company("stripe")], Builder({"stripe": second}), http)
    session.expire_all()
    return {job_id: job.missed_runs for job_id, job in stored(session, "stripe").items()}


async def test_full_listing_success_counts_missing_jobs(session: Session, http: PoliteClient) -> None:
    missed = await _missed_after_second_run(session, http, FakeSource(jobs_for("stripe", "1")))
    assert missed == {"1": 0, "2": 1, "3": 1}


async def test_error_mid_fetch_is_never_complete(session: Session, http: PoliteClient) -> None:
    failing = FakeSource(
        jobs_for("stripe", "1", "2", "3"), fail_after=1, error=FetchError("timeout", host="x")
    )
    missed = await _missed_after_second_run(session, http, failing)
    assert missed == {"1": 0, "2": 0, "3": 0}  # 2 and 3 were never reached, not gone


async def test_blocked_mid_fetch_is_never_complete(session: Session, http: PoliteClient) -> None:
    failing = FakeSource(
        jobs_for("stripe", "1", "2", "3"), fail_after=1, error=BlockedError("403", host="x", status=403)
    )
    missed = await _missed_after_second_run(session, http, failing)
    assert missed == {"1": 0, "2": 0, "3": 0}


async def test_paged_source_is_never_complete(session: Session, http: PoliteClient) -> None:
    paged = FakeSource(jobs_for("stripe", "1"), full_listing=False)
    missed = await _missed_after_second_run(session, http, paged)
    assert missed == {"1": 0, "2": 0, "3": 0}


# --- paged source end to end: real AmazonSource, DB-provided known ids --------------


@respx.mock
async def test_amazon_first_run_hits_cap_second_run_one_page_per_query(
    session: Session, http: PoliteClient
) -> None:
    server = FakeAmazon(load_fixture("amazon_india_sde.json"), catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    amazon = company(
        "amazon",
        platform=Platform.AMAZON.value,
        extra={"categories": ["software-development", "machine-learning-science"]},
    )
    first = await crawl(session, [amazon], SPECS, http=http)
    assert server.pages[("IND", "software-development")] == 3  # 350 jobs, max_pages=3: the cap
    assert len(first.new_job_ids) == 300 + 150 + 250 + 50
    assert outcome(first, "amazon")["capped_queries"] == 1
    assert backfilled(session, "amazon") == ALL_QUERIES

    server.pages = dict.fromkeys(server.pages, 0)
    second = await crawl(session, [amazon], SPECS, http=http)
    assert server.pages == dict.fromkeys(server.pages, 1)  # one page per query
    assert second.new_job_ids == frozenset()
    assert outcome(second, "amazon")["errors"] == 0


@respx.mock
async def test_amazon_timed_out_first_run_is_repaired_by_the_next(
    session: Session, http: PoliteClient, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeAmazon(load_fixture("amazon_india_sde.json"), catalogues())
    server.hang_at = (("IND", "software-development"), 2)  # first run: page 2 never answers
    respx.get(SEARCH_URL).mock(side_effect=server)
    amazon = amazon_company(timeout_seconds=0.3)  # per-source timeout from the registry

    first = await crawl(session, [amazon], SPECS, http=http)
    assert outcome(first, "amazon")["errors"] == 1  # TimeoutError -> dead letter
    assert outcome(first, "amazon")["fetched"] == 0  # cancelled mid-query: nothing yielded yet
    assert backfilled(session, "amazon") == set()  # the cut-short pass is not a full pass

    server.hang_at = None
    server.pages = dict.fromkeys(server.pages, 0)
    with caplog.at_level("WARNING"):
        second = await crawl(session, [amazon], SPECS, http=http)
    assert server.pages[("IND", "software-development")] == 3  # pages fully again (to the cap)
    assert outcome(second, "amazon")["errors"] == 0
    assert backfilled(session, "amazon") == ALL_QUERIES
    assert "amazon:amazon: 1 queries hit max_pages" in caplog.text

    server.pages = dict.fromkeys(server.pages, 0)
    await crawl(session, [amazon], SPECS, http=http)
    assert server.pages == dict.fromkeys(server.pages, 1)  # now one page per query


@respx.mock
async def test_amazon_backfilled_query_cut_short_is_un_backfilled(
    session: Session, http: PoliteClient
) -> None:
    server = FakeAmazon(load_fixture("amazon_india_sde.json"), catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    amazon = amazon_company(timeout_seconds=0.3)
    await crawl(session, [amazon], SPECS, http=http)
    assert IND_SW in backfilled(session, "amazon")

    server.catalogues[("IND", "software-development")][:0] = list(range(10_600, 10_500, -1))  # 100 new
    server.pages = dict.fromkeys(server.pages, 0)
    server.hang_at = (("IND", "software-development"), 2)  # page 1 all new, page 2 hangs
    await crawl(session, [amazon], SPECS, http=http)
    assert IND_SW not in backfilled(session, "amazon")  # next run must page fully again
