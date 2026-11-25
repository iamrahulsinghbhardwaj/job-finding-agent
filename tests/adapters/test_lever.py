import copy
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from jobagent.adapters.common import extract_min_yoe
from jobagent.adapters.lever import LeverSource, _description_text
from jobagent.core.contracts import JobSource
from jobagent.core.http import BlockedError, PoliteClient
from jobagent.core.models import Company, SearchSpec
from tests.adapters.conftest import load_fixture

LIST_URL = "https://api.lever.co/v0/postings/palantir"
JOB_ID = "10dfc8bc-99ad-4ca2-ab76-853cb90a92c2"
DETAIL_URL = f"{LIST_URL}/{JOB_ID}"
SPEC = SearchSpec(keywords=["engineer"], countries=["GB"])
NEWEST_IDS = [
    "98c244fc-444b-43ba-b152-30227191170b",
    "cadc0eb2-2703-43e4-8e4f-41edf5b071c6",
    "6ed76ce8-4156-4b60-b120-403538bd66cd",
    "f163223b-db5d-4c6d-b73f-0a2eaf85583f",
    "5a99fe16-1bda-4313-ace2-a11ea6e58f0c",
]
OLDEST_ID = "5168e8fd-fec1-4fea-b7a1-81bdaea65850"


@pytest.fixture
def board() -> list[dict[str, Any]]:
    return load_fixture("lever_palantir.json")


@pytest.fixture
def source(palantir: Company, http: PoliteClient) -> LeverSource:
    return LeverSource(palantir, palantir.sources[0], http)


def mock_board(jobs: list[dict[str, Any]]) -> respx.Route:
    return respx.get(LIST_URL, params={"mode": "json"}).mock(return_value=httpx.Response(200, json=jobs))


async def collect(source: JobSource, known: frozenset[str] = frozenset()) -> list[str]:
    return [job.source_job_id async for job in source.search(SPEC, known)]


def test_satisfies_protocol(source: LeverSource) -> None:
    as_protocol: JobSource = source
    assert isinstance(as_protocol, JobSource)


# --- search ----------------------------------------------------------------


@respx.mock
async def test_maps_fields(source: LeverSource, board: list[dict[str, Any]]) -> None:
    route = mock_board(board)
    jobs = {job.source_job_id: job async for job in source.search(SPEC, frozenset())}
    assert len(jobs) == 20
    assert route.calls.last.request.url.params["mode"] == "json"

    job = jobs[JOB_ID]
    assert job.title == "Backend Software Engineer - Application Development"
    assert job.company_slug == "palantir"
    assert job.location_raw == "London, United Kingdom"
    assert str(job.url) == f"https://jobs.lever.co/palantir/{JOB_ID}"
    # createdAt 1710188707256 ms -> 2024-03-11 20:25:07.256 UTC
    assert job.posted_at == datetime(2024, 3, 11, 20, 25, 7, 256000, tzinfo=UTC)
    assert job.raw["country"] == "GB"  # original payload kept for later normalization


@respx.mock
async def test_orders_newest_first_by_created_at(source: LeverSource, board: list[dict[str, Any]]) -> None:
    mock_board(board)
    jobs = [job async for job in source.search(SPEC, frozenset())]
    assert [j.source_job_id for j in jobs[:5]] == NEWEST_IDS
    assert jobs[-1].source_job_id == OLDEST_ID
    dates = [j.posted_at for j in jobs if j.posted_at is not None]
    assert len(dates) == 20
    assert dates == sorted(dates, reverse=True)


@respx.mock
async def test_ties_and_missing_dates(source: LeverSource, board: list[dict[str, Any]]) -> None:
    jobs = copy.deepcopy(board[:3])
    jobs[0] |= {"id": "aaaa", "createdAt": 1790000000000}
    jobs[1] |= {"id": "bbbb", "createdAt": 1790000000000}
    jobs[2] |= {"id": "cccc", "createdAt": None}
    mock_board(jobs)
    assert await collect(source) == ["bbbb", "aaaa", "cccc"]  # tie -> id as text; no date -> last


@respx.mock
async def test_known_ids_are_ignored_full_board_every_time(
    source: LeverSource, board: list[dict[str, Any]]
) -> None:
    route = mock_board(board)
    everything = await collect(source)
    assert len(everything) == 20
    assert await collect(source, frozenset(everything)) == everything  # all known: still the full board
    assert source.full_listing
    assert route.call_count == 2  # one request per search: whole board, no paging


@respx.mock
async def test_skips_invalid_listings(source: LeverSource, board: list[dict[str, Any]]) -> None:
    jobs = copy.deepcopy(board)
    jobs[0]["hostedUrl"] = "not a url"
    del jobs[1]["text"]
    jobs[2]["createdAt"] = 10**20  # absurd timestamp
    mock_board(jobs)
    assert len(await collect(source)) == 17
    assert source.skipped == 3


@respx.mock
async def test_unexpected_shape_yields_nothing(source: LeverSource) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"ok": False}))
    assert await collect(source) == []


@respx.mock
async def test_search_403_raises_blocked(source: LeverSource) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        await collect(source)


# --- details ---------------------------------------------------------------


@respx.mock
async def test_get_details_builds_text(source: LeverSource) -> None:
    respx.get(DETAIL_URL, params={"mode": "json"}).mock(
        return_value=httpx.Response(200, json=load_fixture("lever_palantir_job.json"))
    )
    detail = await source.get_details(JOB_ID)
    assert detail.source_job_id == JOB_ID
    text = detail.description_text
    assert text.startswith("A World-Changing Company\nPalantir builds")
    # each list section: heading line, then its bullets
    assert "What We Require\nExperience in designing and developing features" in text
    assert "Technologies We Use\nDifferent backend languages, including Java, Rust, Python and Go" in text
    assert "Life at Palantir" in text  # closing text included
    assert "<" not in text
    assert "\xa0" not in text
    assert "\n\n" not in text
    assert detail.min_yoe is None  # this posting states no number of years


# --- health ----------------------------------------------------------------


@respx.mock
async def test_health_hash_stable(source: LeverSource, board: list[dict[str, Any]]) -> None:
    mock_board(board)
    first = await source.health_check()
    second = await source.health_check()
    assert first.ok
    assert first.job_count == 20
    assert len(first.schema_hash) == 16
    assert first == second

    reordered = dict(reversed(list(board[0].items())))
    reordered["text"] = "Something else"
    mock_board([reordered])
    assert (await source.health_check()).schema_hash == first.schema_hash


@respx.mock
async def test_health_hash_changes_with_schema(source: LeverSource, board: list[dict[str, Any]]) -> None:
    mock_board(board)
    before = await source.health_check()
    jobs = copy.deepcopy(board)
    del jobs[0]["hostedUrl"]
    mock_board(jobs)
    assert (await source.health_check()).schema_hash != before.schema_hash


@respx.mock
async def test_health_reports_blocked_empty_and_bad_shape(source: LeverSource) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(403))
    blocked = await source.health_check()
    assert not blocked.ok
    assert blocked.error is not None
    assert blocked.error.startswith("blocked:")

    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=[]))
    empty = await source.health_check()
    assert (empty.ok, empty.job_count, empty.error) == (False, 0, None)

    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
    odd = await source.health_check()
    assert (odd.ok, odd.error) == (False, "unexpected response shape")


def test_fixtures_are_trimmed() -> None:
    assert len(load_fixture("lever_palantir.json")) == 20
    assert len(json.dumps(load_fixture("lever_palantir_job.json"))) < 8000


@respx.mock
async def test_search_min_yoe_matches_details(source: LeverSource, board: list[dict[str, Any]]) -> None:
    # The list already carries full descriptions, so min_yoe is known without get_details.
    mock_board(board)
    jobs = [job async for job in source.search(SPEC, frozenset())]
    for job in jobs:
        assert job.min_yoe == extract_min_yoe(_description_text(job.raw))
    assert any(job.min_yoe is not None for job in jobs)
