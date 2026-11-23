from pathlib import Path

import pytest

from jobagent.jobs.registry import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prints_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--path", str(REPO_ROOT / "companies.yaml")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["slug", "tier", "platform", "tenant"]
    assert lines[2].split() == ["microsoft", "T1", "eightfold", "microsoft.com"]
    assert lines[3].split() == ["amazon", "T1", "amazon", "amazon.jobs"]
    assert lines[4].split() == ["stripe", "T1", "greenhouse", "stripe"]
    assert lines[5].split() == ["palantir", "OTHER", "lever", "palantir"]
    assert lines[6].split() == ["openai", "OTHER", "ashby", "openai"]
    assert lines[-1] == "5 companies"


def test_company_without_sources_shows_dash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n  - {slug: acme, name: Acme, tier: T1_STARTUP, india_presence: false,"
        " europe_presence: false, us_transfer_path: false}\n"
    )
    assert main(["--path", str(path)]) == 0
    assert capsys.readouterr().out.splitlines()[2].split() == ["acme", "T1_STARTUP", "-", "-"]


def test_bad_file_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--path", str(tmp_path / "missing.yaml")]) == 1
    assert "Registry error" in capsys.readouterr().err
