from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from jobagent.core.models import (
    Company,
    HealthReport,
    JobDetail,
    Platform,
    RawJob,
    SearchSpec,
    SourceConfig,
    SourceStatus,
    Tier,
)


def company_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "slug": "stripe",
        "name": "Stripe",
        "tier": "T1",
        "india_presence": True,
        "europe_presence": True,
        "us_transfer_path": True,
        "sources": [{"platform": "greenhouse", "tenant": "stripe"}],
    }
    return data | overrides


def raw_job_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "source_job_id": "123",
        "company_slug": "stripe",
        "title": "Senior Backend Engineer",
        "location_raw": "Bengaluru, India",
        "url": "https://boards.greenhouse.io/stripe/jobs/123",
        "posted_at": datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
        "raw": {"id": 123},
    }
    return data | overrides


# --- valid data -------------------------------------------------------------


def test_company_parses_from_yaml_like_dict() -> None:
    company = Company.model_validate(company_data(name="  Stripe  "))
    assert company.tier is Tier.T1
    assert company.name == "Stripe"  # whitespace stripped
    assert company.sources[0].platform is Platform.GREENHOUSE
    assert company.sources[0].extra == {}


def test_company_sources_default_empty() -> None:
    data = company_data(tier="T1_STARTUP")
    del data["sources"]
    assert Company.model_validate(data).sources == []


def test_search_spec_defaults_and_uppercases_countries() -> None:
    spec = SearchSpec(keywords=["backend"], countries=["in", " ie "])
    assert spec.countries == ["IN", "IE"]
    assert spec.max_pages == 5


def test_raw_job_valid() -> None:
    job = RawJob.model_validate(raw_job_data(posted_at=None))
    assert str(job.url).startswith("https://")
    assert job.posted_at is None


def test_job_detail_and_health_report_valid() -> None:
    assert JobDetail(source_job_id="1", description_text="...", min_yoe=4).min_yoe == 4
    assert JobDetail(source_job_id="1", description_text="...").min_yoe is None
    report = HealthReport(ok=False, job_count=0, schema_hash="abc", error="HTTP 403")
    assert report.error == "HTTP 403"


# --- bad data is rejected ---------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "Stripe"},  # upper case
        {"slug": "jp morgan"},  # space
        {"slug": "-stripe"},  # leading dash
        {"name": "   "},  # empty after strip
        {"tier": "TIER_1"},  # unknown tier
        {"sources": [{"platform": "workday", "tenant": "x"}]},  # unknown platform
        {"sources": [{"platform": "lever", "tenant": ""}]},  # empty tenant
        {"sources": [{"platform": "lever", "tennant": "x"}]},  # typo -> extra field
        {"india_presence": "maybe"},
        {"careers_url": "https://x.com"},  # unknown field
    ],
)
def test_company_rejects_bad_data(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Company.model_validate(company_data(**overrides))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"countries": ["India"]},
        {"countries": ["I"]},
        {"countries": ["1N"]},
        {"countries": ["IN"], "max_pages": 0},
        {"countries": ["IN"], "max_pages": 51},
    ],
)
def test_search_spec_rejects_bad_data(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SearchSpec.model_validate({"keywords": ["backend"]} | kwargs)


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "not a url"},
        {"url": "ftp://example.com/job"},
        {"posted_at": datetime(2026, 11, 20, 9, 0)},  # naive datetime
        {"source_job_id": ""},
        {"title": "  "},
    ],
)
def test_raw_job_rejects_bad_data(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RawJob.model_validate(raw_job_data(**overrides))


@pytest.mark.parametrize("min_yoe", [-1, 51])
def test_job_detail_rejects_bad_yoe(min_yoe: int) -> None:
    with pytest.raises(ValidationError):
        JobDetail(source_job_id="1", description_text="...", min_yoe=min_yoe)


def test_health_report_rejects_negative_count_and_ok_with_error() -> None:
    with pytest.raises(ValidationError):
        HealthReport(ok=True, job_count=-1, schema_hash="abc")
    with pytest.raises(ValidationError, match="cannot be ok"):
        HealthReport(ok=True, job_count=3, schema_hash="abc", error="boom")


def test_models_are_frozen() -> None:
    job = RawJob.model_validate(raw_job_data())
    with pytest.raises(ValidationError):
        job.title = "changed"  # pyright: ignore[reportAttributeAccessIssue]
    source = SourceConfig(platform=Platform.LEVER, tenant="x")
    with pytest.raises(ValidationError):
        source.tenant = "y"  # pyright: ignore[reportAttributeAccessIssue]


# --- source status ----------------------------------------------------------


def test_source_defaults_to_active() -> None:
    source = SourceConfig(platform=Platform.LEVER, tenant="x")
    assert source.status is SourceStatus.ACTIVE
    assert source.blocked_reason is None


def test_blocked_source_needs_reason() -> None:
    blocked = SourceConfig.model_validate(
        {"platform": "eightfold", "tenant": "x", "status": "blocked", "blocked_reason": "403 on 2026-11-23"}
    )
    assert blocked.status is SourceStatus.BLOCKED
    with pytest.raises(ValidationError, match="needs a blocked_reason"):
        SourceConfig.model_validate({"platform": "eightfold", "tenant": "x", "status": "blocked"})
    with pytest.raises(ValidationError, match="only allowed when status is blocked"):
        SourceConfig.model_validate({"platform": "lever", "tenant": "x", "blocked_reason": "why?"})
    with pytest.raises(ValidationError):
        SourceConfig.model_validate({"platform": "lever", "tenant": "x", "status": "paused"})


def test_active_sources_skips_blocked() -> None:
    company = Company.model_validate(
        company_data(
            sources=[
                {"platform": "eightfold", "tenant": "a", "status": "blocked", "blocked_reason": "429"},
                {"platform": "greenhouse", "tenant": "b"},
            ]
        )
    )
    assert [s.tenant for s in company.active_sources] == ["b"]


@pytest.mark.parametrize("seconds", [0, -5, 3601])
def test_source_timeout_must_be_sane(seconds: float) -> None:
    with pytest.raises(ValidationError):
        SourceConfig(platform=Platform.AMAZON, tenant="amazon.jobs", timeout_seconds=seconds)


def test_source_timeout_defaults_to_none() -> None:
    assert SourceConfig(platform=Platform.LEVER, tenant="x").timeout_seconds is None
