import copy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from jobagent.adapters.ashby import AshbySource
from jobagent.core.contracts import JobSource
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, SearchSpec
from tests.adapters.conftest import load_fixture

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/openai"
JOB_ID = "621bb104-9daa-4c9e-949a-03d5730334e8"
SPEC = SearchSpec(keywords=["engineer"], countries=["IN", "GB"])
NEWEST_IDS = [
    "83b21911-d467-44ab-af3b-c0b898488ab4",
    "980bc2ea-2c35-4546-bb48-5496a4e6847f",
    "103402a5-6391-4cf9-8a87-634695303f04",
    "a3825ab9-72b7-4ce3-89f0-9def7b7b15af",
    "8ddb799f-f0c7-4df8-9665-5016a1f39da3",
]
OLDEST_ID = "29c2c171-fb63-4985-9ac5-229b2ff5ced6"


@pytest.fixture
def board() -> dict[str, Any]:
    return load_fixture("ashby_openai.json")


@pytest.fixture
def source(openai: Company, http: PoliteClient) -> AshbySource:
    return AshbySource(openai, openai.sources[0], http)


def mock_board(body: dict[str, Any]) -> respx.Route:
    return respx.get(BOARD_URL, params={"includeCompensation": "true"}).mock(
        return_value=httpx.Response(200, json=body)
    )


def with_jobs(board: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"apiVersion": board["apiVersion"], "jobs": jobs}


async def collect(source: JobSource, known: frozenset[str] = frozenset()) -> list[str]:
    return [job.source_job_id async for job in source.search(SPEC, known)]


def test_satisfies_protocol(source: AshbySource) -> None:
    as_protocol: JobSource = source
    assert isinstance(as_protocol, JobSource)


# --- search ----------------------------------------------------------------


@respx.mock
async def test_maps_fields(source: AshbySource, board: dict[str, Any]) -> None:
    route = mock_board(board)
    jobs = {job.source_job_id: job async for job in source.search(SPEC, frozenset())}
    assert len(jobs) == 20
    assert route.calls.last.request.url.params["includeCompensation"] == "true"

    job = jobs[JOB_ID]
    assert job.title == "Software Engineer, ChatGPT Infrastructure"
    assert job.company_slug == "openai"
    assert job.location_raw == "London, UK"
    assert str(job.url) == f"https://jobs.ashbyhq.com/openai/{JOB_ID}"
    assert job.posted_at == datetime(2026, 7, 28, 19, 48, 3, 864000, tzinfo=UTC)
    assert job.raw["address"]["postalAddress"]["addressCountry"] == "United Kingdom"  # kept for later

    # Trailing spaces in real titles are cleaned by the model.
    assert jobs["3fefc615-6950-4a29-9214-eefdc4e659e3"].title == "Protection Scientist Engineer, Integrity"
    assert jobs["a36322aa-829e-47b0-821d-d15a40733b83"].title == "Software Engineer, Guardian"


@respx.mock
async def test_orders_newest_first_by_published_at(source: AshbySource, board: dict[str, Any]) -> None:
    mock_board(board)
    jobs = [job async for job in source.search(SPEC, frozenset())]
    assert [j.source_job_id for j in jobs[:5]] == NEWEST_IDS
    assert jobs[-1].source_job_id == OLDEST_ID
    dates = [j.posted_at for j in jobs if j.posted_at is not None]
    assert len(dates) == 20
    assert dates == sorted(dates, reverse=True)


@respx.mock
async def test_ties_and_missing_dates(source: AshbySource, board: dict[str, Any]) -> None:
    jobs = copy.deepcopy(board["jobs"][:3])
    same = "2026-11-20T10:00:00.000+00:00"
    jobs[0] |= {"id": "aaaa", "publishedAt": same}
    jobs[1] |= {"id": "bbbb", "publishedAt": same}
    jobs[2] |= {"id": "cccc", "publishedAt": None}
    mock_board(with_jobs(board, jobs))
    assert await collect(source) == ["bbbb", "aaaa", "cccc"]  # tie -> id as text; no date -> last


@respx.mock
async def test_known_ids_are_ignored_full_board_every_time(
    source: AshbySource, board: dict[str, Any]
) -> None:
    route = mock_board(board)
    everything = await collect(source)
    assert len(everything) == 20
    assert await collect(source, frozenset(everything)) == everything  # all known: still the full board
    assert source.full_listing
    assert route.call_count == 2  # one request per search: whole board, no paging


@respx.mock
async def test_skips_unlisted_and_invalid(source: AshbySource, board: dict[str, Any]) -> None:
    jobs = copy.deepcopy(board["jobs"])
    jobs[0]["isListed"] = False
    jobs[1]["jobUrl"] = "not a url"
    del jobs[2]["title"]
    jobs[3]["publishedAt"] = "yesterday"
    mock_board(with_jobs(board, jobs))
    assert len(await collect(source)) == 16
    assert source.unlisted == 1
    assert source.skipped == 3


@respx.mock
async def test_search_403_raises_blocked(source: AshbySource) -> None:
    respx.get(BOARD_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        await collect(source)


# --- details ---------------------------------------------------------------


@respx.mock
async def test_get_details_reuses_board_from_search(source: AshbySource, board: dict[str, Any]) -> None:
    route = mock_board(board)
    await collect(source)
    detail = await source.get_details(JOB_ID)
    assert route.call_count == 1  # no second request: descriptions come with the board
    assert detail.source_job_id == JOB_ID
    text = detail.description_text
    assert text.startswith("About the Team\n\nOur London-based team builds the backend systems")
    assert "4+ years of professional software engineering experience" in text
    assert detail.min_yoe == 4  # "4+ years of professional software engineering experience"


@respx.mock
async def test_get_details_fetches_board_if_needed(source: AshbySource, board: dict[str, Any]) -> None:
    route = mock_board(board)
    detail = await source.get_details(JOB_ID)
    assert route.call_count == 1
    assert "4+ years" in detail.description_text


@respx.mock
async def test_get_details_falls_back_to_html(source: AshbySource, board: dict[str, Any]) -> None:
    jobs = copy.deepcopy(board["jobs"])
    job = next(j for j in jobs if j["id"] == JOB_ID)
    job["descriptionPlain"] = ""
    job["descriptionHtml"] = "<h2>About</h2><p>Build <b>reliable</b> systems.</p>"
    mock_board(with_jobs(board, jobs))
    assert (await source.get_details(JOB_ID)).description_text == "About\nBuild reliable systems."


@respx.mock
async def test_get_details_unknown_job_raises(source: AshbySource, board: dict[str, Any]) -> None:
    mock_board(board)
    with pytest.raises(FetchError, match="not on board"):
        await source.get_details("no-such-job")


# --- health ----------------------------------------------------------------


@respx.mock
async def test_health_hash_stable(source: AshbySource, board: dict[str, Any]) -> None:
    mock_board(board)
    first = await source.health_check()
    second = await source.health_check()
    assert first.ok
    assert first.job_count == 20
    assert len(first.schema_hash) == 16
    assert first == second

    reordered = dict(reversed(list(board["jobs"][0].items())))
    reordered["title"] = "Something else"
    mock_board(with_jobs(board, [reordered]))
    assert (await source.health_check()).schema_hash == first.schema_hash


@respx.mock
async def test_health_hash_changes_with_schema(source: AshbySource, board: dict[str, Any]) -> None:
    mock_board(board)
    before = await source.health_check()
    jobs = copy.deepcopy(board["jobs"])
    jobs[0]["newField"] = True
    mock_board(with_jobs(board, jobs))
    assert (await source.health_check()).schema_hash != before.schema_hash


@respx.mock
async def test_health_reports_blocked_and_empty_board(source: AshbySource, board: dict[str, Any]) -> None:
    respx.get(BOARD_URL).mock(return_value=httpx.Response(403))
    blocked = await source.health_check()
    assert not blocked.ok
    assert blocked.error is not None
    assert blocked.error.startswith("blocked:")

    # Ashby answers unknown boards with 200 and no jobs, so empty must not look healthy.
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=with_jobs(board, [])))
    empty = await source.health_check()
    assert (empty.ok, empty.job_count, empty.error) == (False, 0, None)


def test_fixture_is_trimmed() -> None:
    assert len(load_fixture("ashby_openai.json")["jobs"]) == 20


@respx.mock
async def test_search_min_yoe_matches_details(source: AshbySource, board: dict[str, Any]) -> None:
    mock_board(board)
    jobs = [job async for job in source.search(SPEC, frozenset())]
    for job in jobs:
        assert job.min_yoe == (await source.get_details(job.source_job_id)).min_yoe
    assert any(job.min_yoe is not None for job in jobs)
