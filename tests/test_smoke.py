import importlib
import subprocess
import sys

import pytest

import jobagent
from jobagent.jobs.run import main


@pytest.mark.parametrize("module", ["core", "adapters", "pipeline", "agents", "digest", "db", "jobs"])
def test_package_imports(module: str) -> None:
    assert jobagent.__version__
    importlib.import_module(f"jobagent.{module}")


def test_dry_run_reports_names_not_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY: present" in out
    assert "DATABASE_URL: missing" in out
    assert "1/10 settings present" in out
    assert "sk-super-secret-value" not in out


def test_full_run_without_database_url_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err


def test_module_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jobagent.jobs.run", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "settings present" in result.stdout
