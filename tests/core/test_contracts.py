from datetime import UTC, datetime

import pytest

from jobagent.core.contracts import JobSource
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


async def collect(source: JobSource, stop_at: str | None) -> list[str]:
    return [job.source_job_id async for job in source.search(SPEC, stop_at)]


def test_fake_source_satisfies_protocol() -> None:
    source: JobSource = FakeSource(NEWEST_FIRST)  # checked statically by pyright
    assert isinstance(source, JobSource)  # and structurally at runtime


async def test_yields_all_newest_first_without_stop() -> None:
    assert await collect(FakeSource(NEWEST_FIRST), None) == ["5", "4", "3", "2", "1"]


async def test_stops_before_stop_at_job_id() -> None:
    assert await collect(FakeSource(NEWEST_FIRST), "3") == ["5", "4"]


async def test_stop_at_newest_yields_nothing() -> None:
    assert await collect(FakeSource(NEWEST_FIRST), "5") == []


async def test_unknown_stop_at_yields_everything() -> None:
    assert await collect(FakeSource(NEWEST_FIRST), "999") == ["5", "4", "3", "2", "1"]


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
