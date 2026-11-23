import copy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from jobagent.adapters.amazon import AmazonSource
from jobagent.adapters.common import ScopeError
from jobagent.core.contracts import JobSource
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


async def collect(source: JobSource, spec: SearchSpec = SPEC, stop_at: str | None = None) -> list[str]:
    return [job.source_job_id async for job in source.search(spec, stop_at)]


def test_satisfies_protocol(source: AmazonSource) -> None:
    as_protocol: JobSource = source
    assert isinstance(as_protocol, JobSource)


# --- mapping & ordering ------------------------------------------------------


@respx.mock
async def test_maps_fields(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    jobs = {job.source_job_id: job async for job in source.search(SPEC, None)}
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
    jobs = [job async for job in source.search(SPEC, None)]
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


@respx.mock
async def test_stop_at_does_not_cut_paging_short(source: AmazonSource, page: dict[str, Any]) -> None:
    # sort=recent is not strictly newest first, so a stored id on page 1 must not stop paging:
    # newer jobs can sit on page 2.
    first = made_up_page(page, 100, 20_000_000)
    second = made_up_page(page, 5, 30_000_000)  # higher job numbers = newer, on the LAST page
    route = respx.get(SEARCH_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    ids = await collect(source, stop_at="20000099")
    assert route.call_count == 2  # kept paging past the page containing stop_at
    assert ids == ["30000004", "30000003", "30000002", "30000001", "30000000"]


@respx.mock
async def test_stop_at_behaviour(source: AmazonSource, page: dict[str, Any]) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=page))
    assert await collect(source, stop_at=NEWEST_IDS[3]) == NEWEST_IDS[:3]
    assert await collect(source, stop_at=NEWEST_IDS[0]) == []
    assert len(await collect(source, stop_at="999")) == 20


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
