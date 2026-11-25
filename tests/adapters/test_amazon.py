import asyncio
import copy
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from jobagent.adapters.amazon import AmazonSource
from jobagent.adapters.common import ScopeError
from jobagent.core.contracts import JobSource, QueryLedger
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, Platform, SearchSpec, SourceConfig
from tests.adapters.conftest import load_fixture

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
SPEC = SearchSpec(keywords=[], countries=["IN"])
NEWEST_IDS = ["10611406", "10610387", "10610382", "10610380", "10610616"]
OLDEST_ID = "10599103"


@pytest.fixture
def page() -> dict[str, Any]:
    """The real recorded page (20 jobs, India, software-development)."""
    return load_fixture("amazon_india_sde.json")


@pytest.fixture
def source(amazon: Company, http: PoliteClient) -> AmazonSource:
    return AmazonSource(amazon, amazon.sources[0], http)


def made_up_page(page: dict[str, Any], size: int, start_id: int) -> dict[str, Any]:
    """A page BUILT from the real one (new ids) to test paging without extra live calls."""
    template = page["jobs"][0]
    jobs = []
    for i in range(size):
        job = copy.deepcopy(template)
        job_id = str(start_id + i)
        job |= {"id_icims": job_id, "id": f"uuid-{job_id}", "job_path": f"/en/jobs/{job_id}/x"}
        jobs.append(job)
    return page | {"jobs": jobs}


def params_of(route: respx.Route, call: int) -> list[tuple[str, str]]:
    return list(route.calls[call].request.url.params.multi_items())


async def collect(
    source: JobSource,
    spec: SearchSpec = SPEC,
    known: AbstractSet[str] = frozenset(),
    ledger: QueryLedger | None = None,
) -> list[str]:
    return [job.source_job_id async for job in source.search(spec, known, ledger)]


def test_satisfies_protocol(source: AmazonSource) -> None:
    as_protocol: JobSource = source
    assert isinstance(as_protocol, JobSource)


# --- mapping & ordering ------------------------------------------------------


@respx.mock
async def test_maps_fields(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    jobs = {job.source_job_id: job async for job in source.search(SPEC, frozenset())}
    assert len(jobs) == 20

    job = jobs["10611406"]  # public job number, as in the URL
    assert job.title == "Software Dev Engineer II, Amazon, Selection Monitoring"
    assert job.company_slug == "amazon"
    assert job.location_raw == "Bengaluru, Karnataka, IND"
    assert str(job.url) == (
        "https://www.amazon.jobs/en/jobs/10611406/software-dev-engineer-ii-amazon-selection-monitoring"
    )
    assert job.posted_at == datetime(2026, 11, 23, tzinfo=UTC)  # day only -> midnight UTC
    assert job.raw["id"] == "c39878f8-c460-497a-9528-ffbfb4f19cba"  # internal uuid kept

    assert jobs[OLDEST_ID].posted_at == datetime(2026, 11, 6, tzinfo=UTC)  # "November  6" (double space)
    assert jobs["10608894"].title == "SDE II , AWS IoT Fleet Management"  # real title kept as-is


@respx.mock
async def test_orders_by_posted_date_then_job_number(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    jobs = [job async for job in source.search(SPEC, frozenset())]
    assert [j.source_job_id for j in jobs[:5]] == NEWEST_IDS
    assert jobs[-1].source_job_id == OLDEST_ID
    keys = [(j.posted_at, int(j.source_job_id)) for j in jobs if j.posted_at is not None]
    assert len(keys) == 20
    assert keys == sorted(keys, reverse=True)


# --- scoping & params --------------------------------------------------------


@respx.mock
async def test_sends_scoped_params(source: AmazonSource, page: dict[str, Any]) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    await collect(source, SearchSpec(keywords=["Software Development Engineer"], countries=["IN"]))
    assert params_of(route, 0) == [
        ("category[]", "software-development"),
        ("normalized_country_code[]", "IND"),
        ("sort", "recent"),
        ("offset", "0"),
        ("result_limit", "100"),
        ("base_query", "Software Development Engineer"),
    ]


async def test_requires_a_country(source: AmazonSource) -> None:
    with pytest.raises(ScopeError, match="at least one country"):
        await collect(source, SearchSpec(keywords=[], countries=[]))


async def test_unknown_country_raises(source: AmazonSource) -> None:
    with pytest.raises(ScopeError, match="'US' is not supported"):
        await collect(source, SearchSpec(keywords=[], countries=["US"]))


@pytest.mark.parametrize("extra", [{}, {"categories": []}, {"categories": "software-development"}])
def test_requires_categories(amazon: Company, http: PoliteClient, extra: dict[str, Any]) -> None:
    bad = SourceConfig(platform=Platform.AMAZON, tenant="amazon.jobs", extra=extra)
    with pytest.raises(ScopeError, match=r"extra\.categories"):
        AmazonSource(amazon, bad, http)


@respx.mock
async def test_one_query_per_keyword_and_country_deduped(source: AmazonSource, page: dict[str, Any]) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    spec = SearchSpec(keywords=["SDE", "Backend"], countries=["IN", "IE"])
    ids = await collect(source, spec)
    assert route.call_count == 4  # 2 keywords x 2 countries, each a single short page
    sent = [dict(params_of(route, i)) for i in range(4)]
    queries = {(p["base_query"], p["normalized_country_code[]"]) for p in sent}
    assert queries == {("SDE", "IND"), ("SDE", "IRL"), ("Backend", "IND"), ("Backend", "IRL")}
    assert len(ids) == 20  # same jobs from every query, merged


# --- paging (pages built from the real one) ------------------------------------


@respx.mock
async def test_pages_until_short_page(source: AmazonSource, page: dict[str, Any]) -> None:
    route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=made_up_page(page, 100, 20_000_000)),
            httpx.Response(200, json=made_up_page(page, 100, 20_000_100)),
            httpx.Response(200, json=made_up_page(page, 7, 20_000_200)),
        ]
    )
    assert len(await collect(source)) == 207
    assert route.call_count == 3
    assert [dict(params_of(route, i))["offset"] for i in range(3)] == ["0", "100", "200"]


@respx.mock
async def test_stops_at_max_pages(source: AmazonSource, page: dict[str, Any]) -> None:
    full_pages = [made_up_page(page, 100, 20_000_000 + i * 100) for i in range(3)]
    route = respx.get(SEARCH_URL).mock(side_effect=[httpx.Response(200, json=p) for p in full_pages])
    assert len(await collect(source, SearchSpec(keywords=[], countries=["IN"], max_pages=2))) == 200
    assert route.call_count == 2


# --- incremental paging: stop at the first fully known page, per query ---------------


def page_with_ids(page: dict[str, Any], ids: list[int]) -> dict[str, Any]:
    template = page["jobs"][0]
    jobs = []
    for number in ids:
        job = copy.deepcopy(template)
        job |= {"id_icims": str(number), "id": f"uuid-{number}", "job_path": f"/en/jobs/{number}/x"}
        jobs.append(job)
    return page | {"jobs": jobs, "hits": len(ids)}


class FakeAmazon:
    """Serves one catalogue (newest first) per (country, category) query, paged like Amazon."""

    def __init__(self, page: dict[str, Any], catalogues: dict[tuple[str, str], list[int]]) -> None:
        self.page = page
        self.catalogues = catalogues
        self.pages: dict[tuple[str, str], int] = dict.fromkeys(catalogues, 0)  # requests per query
        self.hang_at: tuple[tuple[str, str], int] | None = None  # (query, request number): hang there

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = (params["normalized_country_code[]"], params["category[]"])
        self.pages[query] += 1
        if self.hang_at == (query, self.pages[query]):
            await asyncio.sleep(60)  # a slow site: the caller's timeout fires first
        offset, limit = int(params["offset"]), int(params["result_limit"])
        ids = self.catalogues[query][offset : offset + limit]
        return httpx.Response(200, json=page_with_ids(self.page, ids))


@pytest.fixture
def two_category_source(amazon: Company, http: PoliteClient) -> AmazonSource:
    config = SourceConfig(
        platform=Platform.AMAZON,
        tenant="amazon.jobs",
        extra={"categories": ["software-development", "machine-learning-science"]},
    )
    return AmazonSource(amazon, config, http)


def catalogues() -> dict[tuple[str, str], list[int]]:
    return {
        ("IND", "software-development"): list(range(10_350, 10_000, -1)),  # 350 jobs
        ("IND", "machine-learning-science"): list(range(20_150, 20_000, -1)),  # 150
        ("IRL", "software-development"): list(range(30_250, 30_000, -1)),  # 250
        ("IRL", "machine-learning-science"): list(range(40_050, 40_000, -1)),  # 50
    }


MULTI_SPEC = SearchSpec(keywords=[], countries=["IN", "IE"], max_pages=3)


@respx.mock
async def test_one_query_per_country_and_category(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    await collect(two_category_source, MULTI_SPEC)
    assert set(server.pages) == {
        ("IND", "software-development"),
        ("IND", "machine-learning-science"),
        ("IRL", "software-development"),
        ("IRL", "machine-learning-science"),
    }
    assert all(count >= 1 for count in server.pages.values())


@respx.mock
async def test_first_run_pages_to_the_cap_or_the_end(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    ids = await collect(two_category_source, MULTI_SPEC)  # nothing known yet
    assert server.pages == {
        ("IND", "software-development"): 3,  # 350 jobs: stopped by max_pages=3 (hard cap)
        ("IND", "machine-learning-science"): 2,  # 100 + a short page of 50
        ("IRL", "software-development"): 3,  # 100 + 100 + a short page of 50
        ("IRL", "machine-learning-science"): 1,  # one short page
    }
    assert len(ids) == 300 + 150 + 250 + 50


IND_SW = "|IND|software-development"
ALL_QUERIES = {
    IND_SW,
    "|IND|machine-learning-science",
    "|IRL|software-development",
    "|IRL|machine-learning-science",
}


async def first_pass(source: AmazonSource) -> tuple[frozenset[str], QueryLedger]:
    ledger = QueryLedger()
    known = frozenset(await collect(source, MULTI_SPEC, ledger=ledger))
    return known, QueryLedger(backfilled=ledger.updated_backfilled())


@respx.mock
async def test_completed_pass_backfills_every_query_and_counts_caps(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    respx.get(SEARCH_URL).mock(side_effect=FakeAmazon(page, catalogues()))
    ledger = QueryLedger()
    await collect(two_category_source, MULTI_SPEC, ledger=ledger)
    assert ledger.updated_backfilled() == ALL_QUERIES  # end of results or max_pages: a full pass
    assert ledger.capped == {IND_SW}  # 350 jobs, max_pages=3: hit the cap


@respx.mock
async def test_not_backfilled_query_pages_fully_even_when_all_known(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    known, _ = await first_pass(two_category_source)
    server.pages = dict.fromkeys(server.pages, 0)
    await collect(two_category_source, MULTI_SPEC, known, QueryLedger())  # nothing backfilled
    assert server.pages[("IND", "software-development")] == 3  # no early stop before a full pass


@respx.mock
async def test_second_run_costs_one_page_per_query(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    known, ledger = await first_pass(two_category_source)
    server.pages = dict.fromkeys(server.pages, 0)
    ids = await collect(two_category_source, MULTI_SPEC, known, ledger)
    assert server.pages == dict.fromkeys(server.pages, 1)  # every query: page 1 all known -> stop
    assert set(ids) <= known  # jobs on fetched pages are still yielded (refreshes last_seen)
    assert ledger.updated_backfilled() == ALL_QUERIES  # early stops keep queries backfilled


@respx.mock
async def test_new_jobs_on_one_query_page_further_only_there(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    known, ledger = await first_pass(two_category_source)
    server.catalogues[("IND", "software-development")][:0] = [10_402, 10_401]  # two new, newest first
    server.pages = dict.fromkeys(server.pages, 0)
    ids = await collect(two_category_source, MULTI_SPEC, known, ledger)
    assert set(ids) - known == {"10402", "10401"}
    assert server.pages[("IND", "software-development")] == 2  # page 1 had new jobs, page 2 all known
    assert server.pages[("IND", "machine-learning-science")] == 1
    assert server.pages[("IRL", "software-development")] == 1


@respx.mock
async def test_timed_out_pass_is_not_backfilled(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    server.hang_at = (("IND", "software-development"), 2)  # page 2 of the first query never answers
    respx.get(SEARCH_URL).mock(side_effect=server)
    ledger = QueryLedger()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await collect(two_category_source, MULTI_SPEC, ledger=ledger)
    assert ledger.started == {IND_SW}
    assert ledger.updated_backfilled() == frozenset()  # cut short: not a full pass


@respx.mock
async def test_error_mid_pass_un_backfills_the_query(
    two_category_source: AmazonSource, page: dict[str, Any]
) -> None:
    server = FakeAmazon(page, catalogues())
    respx.get(SEARCH_URL).mock(side_effect=server)
    known, ledger = await first_pass(two_category_source)
    server.catalogues[("IND", "software-development")][:0] = list(range(10_600, 10_500, -1))  # 100 new
    server.hang_at = (("IND", "software-development"), 2 + 3)  # page 2 of this run (after 3 earlier)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await collect(two_category_source, MULTI_SPEC, known, ledger)
    # page 1 (all new) was read, page 2 was not: newer jobs may hide there, so page fully next time
    assert IND_SW not in ledger.updated_backfilled()


@respx.mock
async def test_known_ids_never_drop_jobs_from_a_fetched_page(
    source: AmazonSource, page: dict[str, Any]
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    assert await collect(source, known=frozenset(NEWEST_IDS)) == await collect(source)
    assert not source.full_listing


@respx.mock
async def test_flags_result_cap(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page | {"hits": 12_000}))
    await collect(source)
    assert source.capped


@respx.mock
async def test_skips_invalid_listings(source: AmazonSource, page: dict[str, Any]) -> None:
    jobs = copy.deepcopy(page["jobs"])
    jobs[0]["posted_date"] = "23/11/2026"
    del jobs[1]["job_path"]
    jobs[2]["title"] = ""
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page | {"jobs": jobs}))
    assert len(await collect(source)) == 17
    assert source.skipped == 3


@respx.mock
async def test_error_field_raises(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page | {"error": "bad request"}))
    with pytest.raises(FetchError, match="search error"):
        await collect(source)


@respx.mock
async def test_search_403_raises_blocked(source: AmazonSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        await collect(source)


# --- details -----------------------------------------------------------------


@respx.mock
async def test_get_details_from_search_results(source: AmazonSource, page: dict[str, Any]) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    await collect(source)
    detail = await source.get_details("10611406")
    assert route.call_count == 1  # no extra request: qualifications came with the search
    assert detail.min_yoe == 3
    text = detail.description_text
    assert text.startswith("Amazon\u2019s Selection Monitoring team")  # real curly apostrophe
    assert "Basic qualifications\n- 3+ years of non-internship professional software development" in text
    assert "Preferred qualifications\n" in text
    assert "<br" not in text


@respx.mock
async def test_min_yoe_values_from_real_postings(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    await collect(source)
    assert (await source.get_details("10610387")).min_yoe == 5
    no_figure = await source.get_details("10609500")
    # Basic (required) quals state no figure; the "8+ years" in PREFERRED quals is not a minimum.
    assert no_figure.min_yoe is None
    assert "8+ years of full software development life cycle" in no_figure.description_text
    assert not no_figure.description_text.endswith("null")  # Amazon's stray literal is dropped


async def test_get_details_requires_search_first(source: AmazonSource) -> None:
    with pytest.raises(FetchError, match="search first"):
        await source.get_details("10611406")


# --- health ------------------------------------------------------------------


@respx.mock
async def test_health(source: AmazonSource, page: dict[str, Any]) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    report = await source.health_check()
    assert (report.ok, report.job_count) == (True, 362)  # total hits, not jobs on the page
    assert len(report.schema_hash) == 16
    assert report == await source.health_check()
    assert dict(params_of(route, 0))["result_limit"] == "1"

    jobs = copy.deepcopy(page["jobs"])
    jobs[0]["new_field"] = 1
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page | {"jobs": jobs}))
    assert (await source.health_check()).schema_hash != report.schema_hash


@respx.mock
async def test_health_reports_blocked_and_empty(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(403))
    blocked = await source.health_check()
    assert not blocked.ok
    assert blocked.error is not None
    assert blocked.error.startswith("blocked:")

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page | {"hits": 0, "jobs": []}))
    empty = await source.health_check()
    assert (empty.ok, empty.job_count) == (False, 0)


def test_fixture_is_trimmed() -> None:
    assert len(load_fixture("amazon_india_sde.json")["jobs"]) == 20


@respx.mock
async def test_search_min_yoe_matches_details(source: AmazonSource, page: dict[str, Any]) -> None:
    # min_yoe is read from the search results (basic quals), so the filter knows it before details.
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    jobs = [job async for job in source.search(SPEC, frozenset())]
    assert all(job.has_description for job in jobs)
    by_id = {job.source_job_id: job.min_yoe for job in jobs}
    assert (by_id["10611406"], by_id["10610387"]) == (3, 5)
    for job in jobs:
        assert job.min_yoe == (await source.get_details(job.source_job_id)).min_yoe
