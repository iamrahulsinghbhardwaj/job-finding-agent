import json
from pathlib import Path

import httpx
import pytest
import respx

from jobagent.jobs.fetch import main

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = str(REPO_ROOT / "companies.yaml")
LIST_URL = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
BOARD = json.loads((REPO_ROOT / "tests/fixtures/greenhouse_stripe.json").read_text(encoding="utf-8"))


@respx.mock
def test_prints_limited_rows(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=BOARD))
    assert main(["--company", "stripe", "--limit", "3", "--path", REGISTRY]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "Integration Engineer | London | https://stripe.com/jobs/search?gh_jid=8337955"
    assert len([line for line in lines if " | " in line]) == 3
    assert lines[-1] == "3 jobs"


@respx.mock
def test_blocked_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(LIST_URL).mock(return_value=httpx.Response(403))
    assert main(["--company", "stripe", "--path", REGISTRY]) == 1
    assert "Blocked" in capsys.readouterr().err


def test_unknown_company_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--company", "nope", "--path", REGISTRY]) == 1
    assert "Known: amazon, microsoft, stripe" in capsys.readouterr().err


def test_platform_without_adapter_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--company", "microsoft", "--path", REGISTRY]) == 1
    assert "eightfold" in capsys.readouterr().err


def test_limit_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        main(["--company", "stripe", "--limit", "0", "--path", REGISTRY])
