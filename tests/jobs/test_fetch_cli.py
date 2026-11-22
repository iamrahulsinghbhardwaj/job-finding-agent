import json
from pathlib import Path

import httpx
import pytest
import respx

from jobagent.jobs.fetch import main

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = str(REPO_ROOT / "companies.yaml")
LIST_URL = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/palantir"
FIXTURES = REPO_ROOT / "tests/fixtures"
BOARD = json.loads((FIXTURES / "greenhouse_stripe.json").read_text(encoding="utf-8"))
LEVER_BOARD = json.loads((FIXTURES / "lever_palantir.json").read_text(encoding="utf-8"))


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
    assert "Known: amazon, microsoft, palantir, stripe" in capsys.readouterr().err


def test_platform_without_adapter_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--company", "microsoft", "--path", REGISTRY]) == 1
    assert "eightfold" in capsys.readouterr().err


def test_limit_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        main(["--company", "stripe", "--limit", "0", "--path", REGISTRY])


@respx.mock
def test_lever_company_works(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(LEVER_URL).mock(return_value=httpx.Response(200, json=LEVER_BOARD))
    assert main(["--company", "palantir", "--limit", "2", "--path", REGISTRY]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == (
        "Security Systems Engineer | Seattle, WA | "
        "https://jobs.lever.co/palantir/98c244fc-444b-43ba-b152-30227191170b"
    )
    assert lines[-1] == "2 jobs"
