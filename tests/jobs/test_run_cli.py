from pathlib import Path

import pytest

from jobagent.jobs.run import format_summary, main
from jobagent.pipeline.crawl import CrawlResult, SourceOutcome
from jobagent.pipeline.normalize import NormalizeStats

REGISTRY = str(Path(__file__).resolve().parents[2] / "companies.yaml")


def test_summary_table_groups_sources_by_company() -> None:
    result = CrawlResult(
        run_id=7,
        outcomes=[
            SourceOutcome(
                "amazon", "amazon:amazon.jobs", fetched=1200, new=40, stats=NormalizeStats(1200, 6, 0)
            ),
            SourceOutcome("stripe", "greenhouse:stripe", fetched=20, new=20, stats=NormalizeStats(20, 2, 1)),
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
