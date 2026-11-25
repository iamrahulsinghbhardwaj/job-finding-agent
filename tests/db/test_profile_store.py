"""ensure_profile / get_profile against the test database: the LLM runs only when the profile key
(CV text, model, prompt version) changes; years and overrides are applied on every read."""

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobagent.core.http import BlockedError, PoliteClient
from jobagent.core.llm import LlmError, LlmLedger
from jobagent.db import repo
from jobagent.db.models import CvProfile, Ranking
from jobagent.jobs.profile import _show
from jobagent.pipeline import profile as profile_module
from jobagent.pipeline.profile import (
    OverridesError,
    ProfileError,
    ProfileSync,
    ensure_profile,
    get_profile,
    profile_key,
)
from jobagent.pipeline.profile_schema import ExtractedProfile, ProfileOverrides, Role, effective_profile_hash
from tests.llm_fakes import FakeOpenAI, completion, make_pdf

CV_URL = "https://acct.blob.core.windows.net/private/cv.pdf?sp=r&se=2027-05-01T00:00:00Z&sig=FAKESIG"
OVERRIDES_URL = "https://acct.blob.core.windows.net/private/overrides.yaml?sp=r&sig=FAKESIG"
MODEL = "gpt-5.4-mini"
TODAY = date(2026, 11, 26)
CV_V1 = ["Jane Doe (fake CV)", "Backend engineer, 5 years Python"]
CV_V2 = ["Jane Doe (fake CV)", "Backend engineer, 6 years Python and Go"]


def profile(start: str = "2021-11") -> ExtractedProfile:
    """One current full-time role from `start`, after an internship that must not count."""
    return ExtractedProfile(
        roles=[
            Role(title="Backend Engineer", company="ExampleCo", start=start, end=None, is_internship=False),
            Role(title="SWE Intern", company="ExampleCo", start="2021-05", end="2021-08", is_internship=True),
        ],
        primary_stack=["Python"],
        secondary_skills=["Postgres"],
        domains=["payments"],
        notable_projects=["Payments ledger, 1M tx/day"],
        target_titles=["Software Engineer"],
    )


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async with PoliteClient(min_delay=0) as client:
        yield client


def serve_cv(lines: Sequence[str]) -> None:
    respx.get(CV_URL).mock(return_value=httpx.Response(200, content=make_pdf(lines)))


def serve_overrides(body: bytes, status: int = 200) -> None:
    respx.get(OVERRIDES_URL).mock(return_value=httpx.Response(status, content=body))


def rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(CvProfile)) or 0


async def sync(
    session: Session,
    http: PoliteClient,
    fake: FakeOpenAI,
    ledger: LlmLedger | None = None,
    model: str = MODEL,
    overrides_url: str | None = None,
) -> ProfileSync:
    return await ensure_profile(
        session,
        http=http,
        client=fake.client,
        cv_url=CV_URL,
        model=model,
        overrides_url=overrides_url,
        ledger=ledger,
        today=TODAY,
    )


@respx.mock
async def test_first_run_builds_and_stores_the_profile(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()))
    ledger = LlmLedger()

    result = await sync(session, http, fake, ledger)

    assert result.rebuilt
    assert result.call is not None
    assert len(fake.requests) == 1
    assert ledger.calls == [result.call]
    stored = get_profile(session, today=TODAY)
    assert stored is not None
    assert stored.version == result.version
    assert stored.profile == result.profile
    assert stored.profile.years_experience == 5.1  # 2021-11 to 2026-11-26, internship excluded
    assert stored.profile.seniority == "senior"
    assert stored.profile.roles == profile().roles
    assert stored.overridden == ()
    assert stored.effective_hash == result.effective_hash == effective_profile_hash(result.version, None)
    assert stored.model == MODEL
    assert stored.key == profile_key("\n".join(CV_V1), MODEL, profile_module.PROFILE_PROMPT_VERSION)


@respx.mock
async def test_unchanged_cv_makes_no_llm_call(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()))  # one result only: a second call would fail
    first = await sync(session, http, fake)

    ledger = LlmLedger()
    second = await sync(session, http, fake, ledger)

    assert not second.rebuilt
    assert second.call is None
    assert second.version == first.version
    assert len(fake.requests) == 1
    assert ledger.calls == []
    assert rows(session) == 1


@respx.mock
async def test_changed_cv_builds_a_new_version(session: Session, http: PoliteClient) -> None:
    fake = FakeOpenAI(completion(profile("2021-11")), completion(profile("2020-11")))
    serve_cv(CV_V1)
    first = await sync(session, http, fake)
    serve_cv(CV_V2)
    second = await sync(session, http, fake)

    assert second.rebuilt
    assert second.version > first.version
    assert len(fake.requests) == 2
    stored = get_profile(session, today=TODAY)
    assert stored is not None
    assert (stored.version, stored.profile.years_experience) == (second.version, 6.1)


@respx.mock
async def test_changed_model_rebuilds(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()), completion(profile()))
    first = await sync(session, http, fake)
    second = await sync(session, http, fake, model="gpt-5.4-nano")
    assert second.rebuilt
    assert second.version > first.version
    assert fake.requests[1]["model"] == "gpt-5.4-nano"
    stored = get_profile(session)
    assert stored is not None
    assert stored.model == "gpt-5.4-nano"


@respx.mock
async def test_bumped_prompt_version_rebuilds(
    session: Session, http: PoliteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()), completion(profile()))
    first = await sync(session, http, fake)
    monkeypatch.setattr(profile_module, "PROFILE_PROMPT_VERSION", profile_module.PROFILE_PROMPT_VERSION + 1)
    second = await sync(session, http, fake)
    assert second.rebuilt
    assert second.version > first.version
    assert len(fake.requests) == 2


@respx.mock
async def test_reverting_to_an_older_cv_is_a_new_version(session: Session, http: PoliteClient) -> None:
    fake = FakeOpenAI(completion(profile()), completion(profile("2020-11")), completion(profile()))
    for lines in (CV_V1, CV_V2, CV_V1):
        serve_cv(lines)
        await sync(session, http, fake)
    assert len(fake.requests) == 3  # compared with the newest profile only
    assert rows(session) == 3


def test_get_profile_is_none_before_the_first_build(session: Session) -> None:
    assert get_profile(session) is None


@respx.mock
async def test_llm_failure_stores_nothing(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(None, refusal="no"))
    with pytest.raises(LlmError):
        await sync(session, http, fake)
    assert rows(session) == 0


@respx.mock
async def test_cv_download_failure_makes_no_llm_call(session: Session, http: PoliteClient) -> None:
    respx.get(CV_URL).mock(return_value=httpx.Response(403))
    fake = FakeOpenAI()
    with pytest.raises(BlockedError):
        await sync(session, http, fake)
    assert fake.requests == []
    assert rows(session) == 0


@respx.mock
async def test_logs_never_carry_cv_text_profile_or_signature(
    session: Session, http: PoliteClient, caplog: pytest.LogCaptureFixture
) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()))
    with caplog.at_level(logging.DEBUG):
        await sync(session, http, fake)
        await sync(session, http, fake)
    logs = caplog.text
    assert "profile rebuilt (v" in logs
    assert "profile unchanged (v" in logs
    for secret in ("Jane Doe", "Backend engineer", "Payments ledger", "FAKESIG"):
        assert secret not in logs


@respx.mock
async def test_show_prints_the_stored_profile(
    session: Session, http: PoliteClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _show(session) == 1
    assert "No profile stored yet" in capsys.readouterr().err

    serve_cv(CV_V1)
    result = await sync(session, http, FakeOpenAI(completion(profile())))
    assert _show(session) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"profile v{result.version}, model {MODEL}, built ")
    assert '"seniority": "senior"' in out
    assert f"effective profile hash: {effective_profile_hash(result.version, None)}" in out


# --- years up to today, overrides, older stored shapes ------------------------------------------


@respx.mock
async def test_years_are_counted_up_to_the_day_it_is_read(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    await sync(session, http, FakeOpenAI(completion(profile())))
    a_year_later = get_profile(session, today=date(2027, 11, 26))
    assert a_year_later is not None
    assert a_year_later.profile.years_experience == 6.1  # no rebuild needed for time to pass


@respx.mock
async def test_overrides_file_wins(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    serve_overrides(b"target_titles: [SDE II, Senior Backend Engineer]\nyears_experience: 4\n")
    result = await sync(session, http, FakeOpenAI(completion(profile())), overrides_url=OVERRIDES_URL)
    assert result.profile.target_titles == ["SDE II", "Senior Backend Engineer"]
    assert result.profile.years_experience == 4
    assert result.profile.seniority == "mid"
    assert result.profile.primary_stack == ["Python"]  # not in the file: extracted value kept
    assert result.overridden == ("target_titles", "years_experience")


@respx.mock
async def test_editing_overrides_needs_no_llm_call(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()))  # one result only: a second call would fail
    serve_overrides(b"target_titles: [SDE II]\n")
    first = await sync(session, http, fake, overrides_url=OVERRIDES_URL)
    serve_overrides(b"target_titles: [Senior Backend Engineer]\nprimary_stack: [Go]\n")
    second = await sync(session, http, fake, overrides_url=OVERRIDES_URL)
    assert not second.rebuilt
    assert second.version == first.version
    assert len(fake.requests) == 1
    assert second.profile.target_titles == ["Senior Backend Engineer"]
    assert second.profile.primary_stack == ["Go"]


@respx.mock
async def test_get_profile_applies_given_overrides(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    await sync(session, http, FakeOpenAI(completion(profile())))
    stored = get_profile(session, overrides=ProfileOverrides(target_titles=["SDE II"]), today=TODAY)
    assert stored is not None
    assert stored.profile.target_titles == ["SDE II"]
    assert stored.overridden == ("target_titles",)


@respx.mock
async def test_bad_overrides_file_stops_before_the_llm(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    serve_overrides(b"years_experience: 4\n")  # target_titles missing
    fake = FakeOpenAI()
    with pytest.raises(OverridesError):
        await sync(session, http, fake, overrides_url=OVERRIDES_URL)
    assert fake.requests == []
    assert rows(session) == 0


@respx.mock
async def test_profile_stored_in_an_older_shape_is_rebuilt(session: Session, http: PoliteClient) -> None:
    # v1 rows (prompt version 1) held years_experience and seniority from the model, no roles.
    old = {"years_experience": 3.0, "seniority": "mid", "primary_stack": ["Python"]}
    old_version = repo.add_cv_profile(session, key="old-key", model=MODEL, profile_json=old)
    with pytest.raises(ProfileError, match=f"v{old_version} has an older shape"):
        get_profile(session)

    serve_cv(CV_V1)
    result = await sync(session, http, FakeOpenAI(completion(profile())))
    assert result.rebuilt
    assert result.version > old_version
    stored = get_profile(session, today=TODAY)
    assert stored is not None
    assert stored.profile.years_experience == 5.1


@respx.mock
async def test_logs_never_carry_overrides(
    session: Session, http: PoliteClient, caplog: pytest.LogCaptureFixture
) -> None:
    serve_cv(CV_V1)
    serve_overrides(b"target_titles: [PRIVATE-TITLE]\nprimary_stack: [PRIVATE-STACK]\n")
    with caplog.at_level(logging.DEBUG):
        await sync(session, http, FakeOpenAI(completion(profile())), overrides_url=OVERRIDES_URL)
    assert "profile overrides: target_titles, primary_stack" in caplog.text
    for secret in ("PRIVATE-TITLE", "PRIVATE-STACK", "FAKESIG"):
        assert secret not in caplog.text


# --- effective profile hash ----------------------------------------------------------------------


@respx.mock
async def test_effective_hash_follows_the_overrides(session: Session, http: PoliteClient) -> None:
    serve_cv(CV_V1)
    fake = FakeOpenAI(completion(profile()))  # one LLM call for the whole test
    plain = await sync(session, http, fake)
    plain_again = await sync(session, http, fake)
    assert plain_again.effective_hash == plain.effective_hash  # no overrides: stable

    serve_overrides(b"target_titles: [SDE II]\n")
    titles = await sync(session, http, fake, overrides_url=OVERRIDES_URL)
    serve_overrides(b"# comment\ntarget_titles:\n  - SDE II\n")  # same content, other layout
    titles_again = await sync(session, http, fake, overrides_url=OVERRIDES_URL)
    serve_overrides(b"target_titles: [SDE II]\nyears_experience: 4\n")
    with_years = await sync(session, http, fake, overrides_url=OVERRIDES_URL)

    assert {r.version for r in (plain, titles, with_years)} == {plain.version}  # same stored profile
    assert titles.effective_hash != plain.effective_hash
    assert titles_again.effective_hash == titles.effective_hash
    assert with_years.effective_hash not in (plain.effective_hash, titles.effective_hash)
    stored = get_profile(session, overrides=ProfileOverrides(target_titles=["SDE II"], years_experience=4))
    assert stored is not None
    assert stored.effective_hash == with_years.effective_hash


def test_ranking_rows_require_the_effective_hash() -> None:
    # The migration matches the models (test_repo.test_migration_matches_models), nullability included.
    columns = {c.name: c for c in Ranking.__table__.columns}
    assert columns["effective_profile_hash"].nullable is False
