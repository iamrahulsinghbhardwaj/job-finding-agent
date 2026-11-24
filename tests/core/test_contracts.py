from datetime import UTC, datetime

import pytest

from jobagent.core.contracts import JobSource, QueryLedger
from jobagent.core.models import JobDetail, RawJob, SearchSpec
from tests.fakes import FakeSource

SPEC = SearchSpec(keywords=["backend"], countries=["IN"])


def make_job(job_id: str) -> RawJob:
    return RawJob(
        source_job_id=job_id,
        company_slug="stripe",
        title=f"Engineer {job_id}",
        location_raw="Bengaluru, India",
        url=f"https://example.com/jobs/{job_id}",  # pyright: ignore[reportArgumentType]
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={"id": job_id},
    )


NEWEST_FIRST = [make_job(i) for i in ("5", "4", "3", "2", "1")]


async def collect(
    source: JobSource, known: frozenset[str] = frozenset(), spec: SearchSpec = SPEC
) -> list[str]:
    return [job.source_job_id async for job in source.search(spec, known)]


def test_fake_source_satisfies_protocol() -> None:
    source: JobSource = FakeSource(NEWEST_FIRST)  # checked statically by pyright
    assert isinstance(source, JobSource)  # and structurally at runtime


async def test_full_listing_yields_everything_even_when_known() -> None:
    source = FakeSource(NEWEST_FIRST)
    assert await collect(source, frozenset({"5", "4", "3", "2", "1"})) == ["5", "4", "3", "2", "1"]
    assert source.pages_fetched == 1


async def test_paged_source_stops_at_first_fully_known_page_once_backfilled() -> None:
    source = FakeSource(NEWEST_FIRST, full_listing=False, page_size=2)
    ledger = QueryLedger(backfilled=frozenset({"fake"}))
    # page 1 = [5, 4] has a new job, page 2 = [3, 2] is all known -> stop, page 3 never fetched
    ids = [job.source_job_id async for job in source.search(SPEC, frozenset({"3", "2", "1"}), ledger)]
    assert ids == ["5", "4", "3", "2"]
    assert source.pages_fetched == 2
    assert ledger.updated_backfilled() == {"fake"}  # an early stop keeps it backfilled


async def test_paged_source_not_backfilled_pages_to_the_end() -> None:
    source = FakeSource(NEWEST_FIRST, full_listing=False, page_size=2)
    ledger = QueryLedger()
    ids = [
        job.source_job_id async for job in source.search(SPEC, frozenset({"5", "4", "3", "2", "1"}), ledger)
    ]
    assert len(ids) == 5
    assert source.pages_fetched == 3  # all known, but no early stop before a full pass
    assert ledger.updated_backfilled() == {"fake"}


async def test_paged_source_respects_max_pages() -> None:
    source = FakeSource(NEWEST_FIRST, full_listing=False, page_size=2)
    spec = SearchSpec(keywords=[], countries=["IN"], max_pages=2)
    assert await collect(source, spec=spec) == ["5", "4", "3", "2"]
    assert source.pages_fetched == 2


async def test_fail_after_raises_mid_search() -> None:
    source = FakeSource(NEWEST_FIRST, fail_after=2, error=ValueError("boom"))
    seen: list[str] = []

    async def drain() -> None:
        async for job in source.search(SPEC, frozenset()):
            seen.append(job.source_job_id)

    with pytest.raises(ValueError, match="boom"):
        await drain()
    assert seen == ["5", "4"]
    assert source.known_seen == [frozenset()]


async def test_get_details() -> None:
    detail = JobDetail(source_job_id="3", description_text="Build things.", min_yoe=4)
    source = FakeSource(NEWEST_FIRST, details={"3": detail})
    assert await source.get_details("3") == detail
    with pytest.raises(KeyError):
        await source.get_details("404")


async def test_health_check() -> None:
    report = await FakeSource(NEWEST_FIRST).health_check()
    assert report.ok
    assert report.job_count == 5
    assert report.schema_hash == (await FakeSource([make_job("x")]).health_check()).schema_hash
    assert not (await FakeSource([]).health_check()).ok


# --- QueryLedger ------------------------------------------------------------------


def test_ledger_full_pass_backfills() -> None:
    ledger = QueryLedger()
    ledger.started.add("q")
    ledger.full_pass.add("q")
    ledger.finished.add("q")
    assert ledger.updated_backfilled() == {"q"}


def test_ledger_interrupted_pass_is_not_backfilled_and_un_backfills() -> None:
    ledger = QueryLedger(backfilled=frozenset({"old", "untouched"}))
    ledger.started |= {"old", "new"}  # both cut short (timeout): never finished
    assert ledger.updated_backfilled() == {"untouched"}


def test_ledger_early_stop_keeps_backfilled() -> None:
    ledger = QueryLedger(backfilled=frozenset({"q"}))
    ledger.started.add("q")
    ledger.finished.add("q")  # stopped early at a known page: not a full pass, but clean
    assert ledger.updated_backfilled() == {"q"}
