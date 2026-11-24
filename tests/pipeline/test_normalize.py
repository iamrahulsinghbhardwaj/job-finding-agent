from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl, ValidationError

from jobagent.core.models import NormalizedJob, Platform, RawJob, SourceConfig
from jobagent.pipeline.normalize import NormalizeStats, clean_title, normalize, normalize_all, source_key

SOURCE = "amazon:amazon.jobs"

# (raw title, cleaned title, canonical, level). The first block is real fixture titles.
TITLE_CASES = [
    (
        "Software Dev Engineer II, Amazon, Selection Monitoring",
        "Software Dev Engineer II, Amazon, Selection Monitoring",
        "Software Engineer II, Amazon, Selection Monitoring",
        2,
    ),
    (
        "Software Development Engineer III, PaySec, StoreSec",
        "Software Development Engineer III, PaySec, StoreSec",
        "Software Engineer III, PaySec, StoreSec",
        3,
    ),
    (
        "SDE II , AWS IoT Fleet Management",
        "SDE II, AWS IoT Fleet Management",
        "Software Engineer II, AWS IoT Fleet Management",
        2,
    ),
    (
        "Software Development Engineer, Last Mile Platform Services, SDE",
        "Software Development Engineer, Last Mile Platform Services, SDE",
        "Software Engineer, Last Mile Platform Services, Software Engineer",
        None,
    ),
    # the brick's three spellings of the same level
    ("SDE II", "SDE II", "Software Engineer II", 2),
    ("Software Engineer 2", "Software Engineer 2", "Software Engineer II", 2),
    ("Software Engineer - II", "Software Engineer - II", "Software Engineer II", 2),
    # more spellings
    ("SDE2", "SDE2", "Software Engineer II", 2),
    ("SDE-3", "SDE-3", "Software Engineer III", 3),
    ("swe iv", "swe iv", "Software Engineer IV", 4),
    ("Software Engineer I", "Software Engineer I", "Software Engineer I", 1),
    ("Sr. Software Engineer", "Sr. Software Engineer", "Senior Software Engineer", None),
    ("Sr Backend SDE", "Sr Backend SDE", "Senior Backend Software Engineer", None),
    ("  Senior\tSoftware   Engineer \n", "Senior Software Engineer", "Senior Software Engineer", None),
    # things that must NOT change
    ("SDET II", "SDET II", "SDET II", None),
    (
        "Software Engineer, Infrastructure",
        "Software Engineer, Infrastructure",
        "Software Engineer, Infrastructure",
        None,
    ),
    ("Software Engineer In Test", "Software Engineer In Test", "Software Engineer In Test", None),
    ("Web3 Engineer 2", "Web3 Engineer 2", "Web3 Engineer 2", None),
    ("Software Engineer 2025 Grad", "Software Engineer 2025 Grad", "Software Engineer 2025 Grad", None),
    ("Platform Engineer", "Platform Engineer", "Platform Engineer", None),
]


@pytest.mark.parametrize(("raw", "title", "canonical", "level"), TITLE_CASES)
def test_clean_title(raw: str, title: str, canonical: str, level: int | None) -> None:
    result = clean_title(raw)
    assert (result.title, result.canonical, result.level) == (title, canonical, level)


def raw_job(
    job_id: str = "1", title: str = "SDE II , Payments", location: str = "Hyderabad, TS, IN"
) -> RawJob:
    return RawJob(
        source_job_id=job_id,
        company_slug="amazon",
        title=title,
        location_raw=location,
        url=HttpUrl(f"https://www.amazon.jobs/en/jobs/{job_id}"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={"id": job_id},
    )


def test_normalize_maps_every_field() -> None:
    job = normalize(raw_job(), source=SOURCE)
    assert job == NormalizedJob(
        source=SOURCE,
        source_job_id="1",
        company_slug="amazon",
        title="SDE II, Payments",
        title_canonical="Software Engineer II, Payments",
        title_level=2,
        location_raw="Hyderabad, TS, IN",
        country="IN",
        countries=("IN",),
        city="Hyderabad",
        remote=False,
        url=HttpUrl("https://www.amazon.jobs/en/jobs/1"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
    )


def test_normalize_keeps_unknown_location_as_none() -> None:
    job = normalize(raw_job(location="Multiple Locations, EMEA"), source=SOURCE)
    assert (job.country, job.countries, job.city) == (None, (), None)
    assert job.location_raw == "Multiple Locations, EMEA"  # kept for a later classifier


def test_normalize_all_counts_unknowns_and_remote() -> None:
    raws = [
        raw_job("1", location="Bengaluru"),
        raw_job("2", location="Remote - India"),
        raw_job("3", location="N/A"),
        raw_job("4", location="London / Dublin"),
        raw_job("5", location="Remote"),
    ]
    jobs, stats = normalize_all(raws, source=SOURCE)
    assert [job.source_job_id for job in jobs] == ["1", "2", "3", "4", "5"]
    assert stats == NormalizeStats(total=5, unknown_location=3, remote=2)
    assert stats.unknown_share == 0.6
    assert stats.as_counts() == {"normalized": 5, "unknown_location": 3, "remote": 2}
    assert stats.summary() == "5 jobs, 3 unknown location (60.0%)"


def test_stats_add_up_across_sources() -> None:
    total = NormalizeStats(10, 1, 2) + NormalizeStats(30, 1, 0)
    assert total == NormalizeStats(total=40, unknown_location=2, remote=2)
    assert total.summary() == "40 jobs, 2 unknown location (5.0%)"


def test_source_key() -> None:
    assert source_key(SourceConfig(platform=Platform.GREENHOUSE, tenant="stripe")) == "greenhouse:stripe"


def test_display_title_is_the_original() -> None:
    job = normalize(raw_job(title="Software Dev Engineer II, Payments"), source=SOURCE)
    assert job.title == "Software Dev Engineer II, Payments"  # shown to people
    assert job.title_canonical == "Software Engineer II, Payments"  # matching only


def test_normalize_all_empty() -> None:
    assert normalize_all([], source=SOURCE) == ([], NormalizeStats())
    assert NormalizeStats().unknown_share == 0.0


def test_normalized_job_rejects_bad_country() -> None:
    with pytest.raises(ValidationError):
        NormalizedJob(**{**normalize(raw_job(), source=SOURCE).model_dump(), "country": "IND"})
