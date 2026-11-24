import copy
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobagent.adapters.greenhouse import GreenhouseSource
from jobagent.core.contracts import JobSource
from jobagent.core.http import BlockedError, PoliteClient
from jobagent.core.models import Company, SearchSpec
from tests.adapters.conftest import load_fixture

LIST_URL = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
DETAIL_URL = f"{LIST_URL}/8315954"
SPEC = SearchSpec(keywords=["engineer"], countries=["IN", "IE"])
NEWEST_IDS = ["8337955", "8386610", "8352489", "8352150", "8338849", "8315954"]


@pytest.fixture
def board() -> dict[str, object]:
    return load_fixture("greenhouse_stripe.json")


@pytest.fixture
def source(stripe: Company, http: PoliteClient) -> GreenhouseSource:
    return GreenhouseSource(stripe, stripe.sources[0], http)


async def collect(source: JobSource, known: frozenset[str] = frozenset()) -> list[str]:
    return [job.source_job_id async for job in source.search(SPEC, known)]


def test_satisfies_protocol(source: GreenhouseSource) -> None:
    as_protocol: JobSource = source
    assert isinstance(as_protocol, JobSource)


# --- search ----------------------------------------------------------------


@respx.mock
async def test_maps_fields(source: GreenhouseSource, board: dict[str, object]) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=board))
    jobs = {job.source_job_id: job async for job in source.search(SPEC, frozenset())}
    assert len(jobs) == 20

    job = jobs["8315954"]
    assert job.title == "Full-Stack Software Engineer, Communication Tools"
    assert job.company_slug == "stripe"
    assert job.location_raw == "Dublin"
    assert str(job.url) == "https://stripe.com/jobs/search?gh_jid=8315954"
    assert job.posted_at == datetime(2026, 11, 3, 15, 12, tzinfo=UTC)  # first_published, not updated_at
    assert job.raw["internal_job_id"]  # original payload kept

    # Messy real-world values are cleaned by the model.
    assert jobs["8337955"].title == "Integration Engineer"  # leading space
    assert jobs["8301379"].title == "Backend / API Engineer, Radar"  # leading tab
    assert jobs["8319459"].location_raw == "Dublin"  # trailing space


@respx.mock
async def test_orders_newest_first_by_first_published(
    source: GreenhouseSource, board: dict[str, object]
) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=board))
    jobs = [job async for job in source.search(SPEC, frozenset())]
    assert [j.source_job_id for j in jobs[:6]] == NEWEST_IDS
    dates = [j.posted_at for j in jobs if j.posted_at is not None]
    assert len(dates) == 20
    assert dates == sorted(dates, reverse=True)
    assert jobs[-1].source_job_id == "5601879"  # oldest: published 2024


@respx.mock
async def test_ties_and_missing_dates(source: GreenhouseSource, board: dict[str, object]) -> None:
    jobs = copy.deepcopy(board["jobs"])
    assert isinstance(jobs, list)
    same = "2026-11-20T10:00:00-05:00"
    jobs[0] |= {"id": 100, "first_published": same}
    jobs[1] |= {"id": 200, "first_published": same}
    jobs[2] |= {"id": 300, "first_published": None, "updated_at": None}
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": jobs[:3]}))
    assert await collect(source) == ["200", "100", "300"]  # tie -> higher id first; no date -> last


@respx.mock
async def test_falls_back_to_updated_at(source: GreenhouseSource, board: dict[str, object]) -> None:
    jobs = copy.deepcopy(board["jobs"])
    assert isinstance(jobs, list)
    del jobs[0]["first_published"]
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": jobs[:1]}))
    (job,) = [j async for j in source.search(SPEC, frozenset())]
    assert job.posted_at == datetime.fromisoformat(jobs[0]["updated_at"])


@respx.mock
async def test_known_ids_are_ignored_full_board_every_time(
    source: GreenhouseSource, board: dict[str, object]
) -> None:
    route = respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=board))
    everything = await collect(source)
    assert len(everything) == 20
    assert await collect(source, frozenset(everything)) == everything  # all known: still the full board
    assert source.full_listing
    assert route.call_count == 2  # one request per search: whole board, no paging


@respx.mock
async def test_skips_invalid_listings(source: GreenhouseSource, board: dict[str, object]) -> None:
    jobs = copy.deepcopy(board["jobs"])
    assert isinstance(jobs, list)
    jobs[0]["absolute_url"] = "not a url"
    del jobs[1]["title"]
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": jobs}))
    assert len(await collect(source)) == 18
    assert source.skipped == 2


@respx.mock
async def test_search_403_raises_blocked(source: GreenhouseSource) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        await collect(source)


# --- details ---------------------------------------------------------------


@respx.mock
async def test_get_details_extracts_text(source: GreenhouseSource) -> None:
    detail_json = load_fixture("greenhouse_stripe_job.json")
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=detail_json))
    detail = await source.get_details("8315954")
    assert detail.source_job_id == "8315954"
    text = detail.description_text
    assert text.startswith("Who we are\nAbout Stripe\nStripe is a financial infrastructure platform")
    assert "You have 6+ years of professional software engineering experience" in text
    assert "<" not in text
    assert "&lt;" not in text
    assert "&amp;" not in text
    assert detail.min_yoe is None  # parsed later by the pipeline


# --- health ----------------------------------------------------------------


@respx.mock
async def test_health_hash_stable(source: GreenhouseSource, board: dict[str, object]) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=board))
    first = await source.health_check()
    second = await source.health_check()
    assert first.ok
    assert first.job_count == 20
    assert len(first.schema_hash) == 16
    assert first == second

    jobs = board["jobs"]
    assert isinstance(jobs, list)
    reordered = dict(reversed(list(jobs[0].items())))  # same keys, different order and values
    reordered["title"] = "Something else"
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": [reordered]}))
    assert (await source.health_check()).schema_hash == first.schema_hash


@respx.mock
async def test_health_hash_changes_with_schema(source: GreenhouseSource, board: dict[str, object]) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=board))
    before = await source.health_check()
    jobs = copy.deepcopy(board["jobs"])
    assert isinstance(jobs, list)
    jobs[0]["new_field"] = 1
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": jobs}))
    assert (await source.health_check()).schema_hash != before.schema_hash


@respx.mock
async def test_health_reports_blocked_and_empty(source: GreenhouseSource) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(403))
    blocked = await source.health_check()
    assert not blocked.ok
    assert blocked.error is not None
    assert blocked.error.startswith("blocked:")

    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}))
    empty = await source.health_check()
    assert (empty.ok, empty.job_count, empty.error) == (False, 0, None)


def test_fixture_is_trimmed() -> None:
    board = load_fixture("greenhouse_stripe.json")
    assert len(board["jobs"]) == 20
    assert board["meta"]["total"] == 20
    assert len(json.dumps(load_fixture("greenhouse_stripe_job.json"))) < 5000
