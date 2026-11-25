from decimal import Decimal

import pytest

from jobagent.core.llm import LlmCall, LlmLedger
from jobagent.jobs.profile import expiry_warning, format_sync, main
from jobagent.pipeline.profile import ProfileSync
from jobagent.pipeline.profile_schema import CandidateProfile

SETTINGS = {
    "DATABASE_URL": "postgresql://user:hunter2@localhost/none",
    "OPENAI_API_KEY": "sk-secret",
    "PROFILE_MODEL": "gpt-5.4-mini",
    "CV_BLOB_SAS_URL": "https://a.blob.core.windows.net/c/cv.pdf?sig=FAKESIG",
}


def _call(cost: Decimal | None) -> LlmCall:
    return LlmCall("profile", "gpt-5.4-mini", 3120, 0, 410, 900, cost)


PROFILE = CandidateProfile(
    years_experience=4.1,
    seniority="mid",
    primary_stack=["Python"],
    secondary_skills=[],
    domains=[],
    notable_projects=[],
    target_titles=["SDE II"],
    roles=[],
)


def _sync(version: int, call: LlmCall | None, overridden: tuple[str, ...] = ()) -> ProfileSync:
    return ProfileSync(
        version,
        rebuilt=call is not None,
        call=call,
        profile=PROFILE,
        overridden=overridden,
        effective_hash="h",
    )


@pytest.mark.parametrize(
    ("unset", "missing"),
    [
        ("OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("PROFILE_MODEL", "PROFILE_MODEL"),
        ("CV_BLOB_SAS_URL", "CV_BLOB_SAS_URL"),
        ("DATABASE_URL", "DATABASE_URL"),
    ],
)
def test_sync_needs_all_its_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], unset: str, missing: str
) -> None:
    for name, value in SETTINGS.items():
        if name != unset:
            monkeypatch.setenv(name, value)
    assert main([]) == 2
    err = capsys.readouterr().err
    assert err.strip() == f"Missing in .env: {missing}"


def test_empty_setting_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PROFILE_MODEL", "")
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "PROFILE_MODEL" in err
    for secret in ("hunter2", "sk-secret", "FAKESIG"):
        assert secret not in err


def test_show_needs_only_the_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--show"]) == 2
    assert capsys.readouterr().err.strip() == "Missing in .env: DATABASE_URL"


def test_format_sync_unchanged() -> None:
    assert format_sync(_sync(3, None), LlmLedger()) == "profile: unchanged (v3), no LLM call; overrides: none"


def test_format_sync_rebuilt_shows_tokens_cost_and_overridden_fields() -> None:
    call = _call(Decimal("0.0042"))
    assert format_sync(_sync(4, call, ("target_titles", "years_experience")), LlmLedger([call])) == (
        "profile: rebuilt (v4), 1 LLM call, 3,120 in / 410 out tokens, $0.0042; "
        "overrides: target_titles, years_experience"
    )


def test_format_sync_unknown_price() -> None:
    call = _call(None)
    assert "unknown cost" in format_sync(_sync(4, call), LlmLedger([call]))


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (None, None),
        (167, None),
        (14, None),
        (13, "expires in 13 days"),
        (0, "expires in 0 days"),
        (-1, "has expired"),
    ],
)
def test_expiry_warning(days: int | None, expected: str | None) -> None:
    warning = expiry_warning(days)
    if expected is None:
        assert warning is None
    else:
        assert warning is not None
        assert expected in warning


def test_overrides_link_warning_names_its_setting() -> None:
    warning = expiry_warning(3, "profile_overrides_sas_url", "overrides link")
    assert warning == (
        "warning: the overrides link (PROFILE_OVERRIDES_SAS_URL) expires in 3 days: generate a new SAS URL"
    )


def test_overrides_link_is_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Everything but DATABASE_URL set, no PROFILE_OVERRIDES_SAS_URL: only DATABASE_URL is reported.
    for name, value in SETTINGS.items():
        if name != "DATABASE_URL":
            monkeypatch.setenv(name, value)
    assert main([]) == 2
    assert capsys.readouterr().err.strip() == "Missing in .env: DATABASE_URL"
