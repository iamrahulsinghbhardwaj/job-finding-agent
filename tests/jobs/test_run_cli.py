from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from jobagent.core.models import Company, Platform, RawJob, SourceConfig, Tier
from jobagent.jobs.run import format_filter, format_summary, main, run_filter
from jobagent.pipeline.crawl import CrawledJob, CrawlResult, SourceOutcome
from jobagent.pipeline.normalize import NormalizeStats, normalize

REGISTRY = str(Path(__file__).resolve().parents[2] / "companies.yaml")


def test_summary_table_groups_sources_by_company() -> None:
    result = CrawlResult(
        run_id=7,
        outcomes=[
            SourceOutcome(
                "amazon",
                "amazon:amazon.jobs",
                fetched=1200,
                new=40,
                stats=NormalizeStats(total=1200, unknown_location=6),
            ),
            SourceOutcome(
                "stripe",
                "greenhouse:stripe",
                fetched=20,
                new=20,
                stats=NormalizeStats(total=20, unknown_location=2, remote=1),
            ),
            SourceOutcome("stripe", "lever:stripe", fetched=0, errors=1),
            SourceOutcome("microsoft", "eightfold:microsoft.com", blocked=True),
        ],
        new_job_ids=frozenset(range(60)),
    )
    assert format_summary(result).splitlines() == [
        "company    sources         fetched  new  errors  unknown_loc",
        "---------  --------------  -------  ---  ------  -----------",
        "amazon     1               1,200    40   0       6",
        "stripe     2               20       20   1       2",
        "microsoft  0 (+1 blocked)  0        0    0       0",
        "",
        "run 7: 3 sources fetched, 1 blocked, 1,220 jobs, 60 new, 1 errors, 0.7% unknown location",
    ]


def test_unknown_company_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/none")
    assert main(["--path", REGISTRY, "--company", "nope"]) == 1
    assert "Unknown company: nope" in capsys.readouterr().err


def test_bad_registry_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/none")
    path = tmp_path / "companies.yaml"
    path.write_text("companies: []\n", encoding="utf-8")
    assert main(["--path", str(path)]) == 1
    assert "top-level 'search:' mapping" in capsys.readouterr().err


def _company(slug: str, tier: Tier) -> Company:
    return Company(
        slug=slug,
        name=slug.title(),
        tier=tier,
        india_presence=True,
        europe_presence=True,
        us_transfer_path=False,
        sources=[SourceConfig(platform=Platform.GREENHOUSE, tenant=slug)],
    )


def _crawled(slug: str, job_id: str, title: str, location: str, *, new: bool) -> CrawledJob:
    raw = RawJob(
        source_job_id=job_id,
        company_slug=slug,
        title=title,
        location_raw=location,
        url=HttpUrl("https://example.com/job"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={},
    )
    return CrawledJob(normalize(raw, source=f"greenhouse:{slug}"), is_new=new)


def test_filter_summary_counts_per_company_and_top_reasons() -> None:
    companies = [_company("stripe", Tier.T1), _company("tiny", Tier.T1_STARTUP)]
    result = CrawlResult(
        run_id=8,
        outcomes=[],
        new_job_ids=frozenset(),
        jobs=[
            _crawled("stripe", "1", "Senior Backend Engineer", "Dublin", new=True),
            _crawled("stripe", "2", "Senior Backend Engineer", "London", new=False),
            _crawled("stripe", "3", "Staff Engineer", "Dublin", new=True),
            _crawled("stripe", "4", "Senior Backend Engineer", "Paris, France", new=True),
            _crawled("stripe", "5", "Senior Engineering Manager", "Dublin", new=False),
            _crawled("stripe", "6", "Engineering Manager", "Dublin", new=False),
            _crawled("tiny", "1", "Founding Engineer", "Remote", new=True),
        ],
    )
    assert format_filter(*run_filter(result, companies)).splitlines() == [
        "",
        "rule filter",
        "company  jobs  passed  passed_new",
        "-------  ----  ------  ----------",
        "stripe   6     2       1",
        "tiny     1     1       1",
        "",
        "filter: 7 -> 3 passed (2 new, 1 labelled Remote (unspecified))",
        "",
        "top rejection reasons (of 4 rejected):",
        "  excluded:manager  2",
        "  excluded:staff    1",
        "  location          1",
    ]
