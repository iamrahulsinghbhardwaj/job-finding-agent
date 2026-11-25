"""CV loading and the profile model, without a database (store tests: tests/db/test_profile_store.py)."""

import logging
from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobagent.core.http import BlockedError, PoliteClient
from jobagent.pipeline.profile import (
    INSTRUCTIONS,
    PROFILE_PROMPT_VERSION,
    CvError,
    OverridesError,
    build_profile,
    extract_text,
    load_cv_text,
    load_overrides,
    profile_key,
    sas_days_left,
)
from jobagent.pipeline.profile_schema import ExtractedProfile, ProfileOverrides, Role
from tests.llm_fakes import FakeOpenAI, completion, make_pdf

CV_URL = "https://acct.blob.core.windows.net/private/cv.pdf?sp=r&se=2027-05-01T00:00:00Z&sig=FAKESIG"
OVERRIDES_URL = "https://acct.blob.core.windows.net/private/overrides.yaml?sp=r&sig=FAKESIG"
CV_LINES = ["Jane Doe (fake CV)", "Backend engineer, 5 years Python and Go", "Payments platform at ExampleCo"]


def profile() -> ExtractedProfile:
    return ExtractedProfile(
        roles=[Role(title="Backend Engineer", company="Ex", start="2021-07", end=None, is_internship=False)],
        primary_stack=["Python", "Go"],
        secondary_skills=["Kubernetes"],
        domains=["payments"],
        notable_projects=["Built a payments ledger handling 1M tx/day"],
        target_titles=["SDE II", "Senior Backend Engineer"],
    )


def test_extract_text_reads_every_line() -> None:
    assert extract_text(make_pdf(CV_LINES)) == "\n".join(CV_LINES)


@pytest.mark.parametrize("pdf", [b"", b"not a pdf at all", b"%PDF-1.4\ngarbage"])
def test_unreadable_pdf_raises_cv_error(pdf: bytes) -> None:
    with pytest.raises(CvError, match="not a readable PDF"):
        extract_text(pdf)


def test_pdf_without_text_raises_cv_error() -> None:
    with pytest.raises(CvError, match="no extractable text"):
        extract_text(make_pdf([]))


def test_profile_key_is_stable_and_hex_sha256() -> None:
    key = profile_key("cv text", "gpt-5.4-mini", 1)
    assert key == profile_key("cv text", "gpt-5.4-mini", 1)
    assert len(key) == 64
    assert int(key, 16) >= 0


@pytest.mark.parametrize(
    "changed",
    [
        ("cv text!", "gpt-5.4-mini", 1),  # CV text
        ("cv text", "gpt-5.4-nano", 1),  # model
        ("cv text", "gpt-5.4-mini", 2),  # prompt version
    ],
)
def test_profile_key_changes_with_each_input(changed: tuple[str, str, int]) -> None:
    assert profile_key(*changed) != profile_key("cv text", "gpt-5.4-mini", 1)


def test_profile_key_parts_cannot_run_together() -> None:
    # Plain concatenation would make ("ab", "c") and ("a", "bc") collide.
    assert profile_key("ab", "c", 1) != profile_key("a", "bc", 1)


@respx.mock
async def test_load_cv_text_downloads_the_pdf() -> None:
    route = respx.get(CV_URL).mock(return_value=httpx.Response(200, content=make_pdf(CV_LINES)))
    async with PoliteClient(min_delay=0) as http:
        text = await load_cv_text(http, CV_URL)
    assert text == "\n".join(CV_LINES)
    assert route.call_count == 1


@respx.mock
async def test_load_cv_text_403_is_blocked_and_hides_the_signature() -> None:
    respx.get(CV_URL).mock(return_value=httpx.Response(403))
    async with PoliteClient(min_delay=0) as http:
        with pytest.raises(BlockedError) as info:
            await load_cv_text(http, CV_URL)
    assert "FAKESIG" not in str(info.value)
    assert "acct.blob.core.windows.net" in str(info.value)


NOW = datetime(2026, 11, 25, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "days"),
    [
        (CV_URL, 157),
        ("https://a.blob.core.windows.net/c/cv.pdf?se=2026-12-01T00:00:00Z&sig=x", 6),
        ("https://a.blob.core.windows.net/c/cv.pdf?se=2026-12-01&sig=x", 6),  # date only
        ("https://a.blob.core.windows.net/c/cv.pdf?se=2026-11-20T00:00:00Z&sig=x", -5),  # expired
        ("https://a.blob.core.windows.net/c/cv.pdf?sig=x", None),  # no expiry
        ("https://a.blob.core.windows.net/c/cv.pdf?se=soon&sig=x", None),  # unreadable
    ],
)
def test_sas_days_left(url: str, days: int | None) -> None:
    assert sas_days_left(url, NOW) == days


def test_prompt_version_bumped_for_dated_roles() -> None:
    # v2: roles with dates, years and seniority computed in code. Bumping it rebuilt stored profiles.
    assert PROFILE_PROMPT_VERSION == 2


async def test_build_profile_sends_instructions_and_cv_text() -> None:
    fake = FakeOpenAI(completion(profile()))
    built, call = await build_profile(fake.client, model="gpt-5.4-mini", cv_text="the cv text")
    assert built == profile()
    assert call.purpose == "profile"
    request = fake.requests[0]
    assert request["model"] == "gpt-5.4-mini"
    assert request["response_format"] is ExtractedProfile
    assert request["messages"] == [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": "the cv text"},
    ]


# --- the private overrides file ----------------------------------------------------------------

OVERRIDES_YAML = b"""\
target_titles:
  - SDE II
  - Senior Backend Engineer
years_experience: 4
primary_stack: [Python, Go]
"""


async def _load(body: bytes, status: int = 200) -> ProfileOverrides:
    with respx.mock:
        respx.get(OVERRIDES_URL).mock(return_value=httpx.Response(status, content=body))
        async with PoliteClient(min_delay=0) as http:
            return await load_overrides(http, OVERRIDES_URL)


async def test_load_overrides_reads_yaml() -> None:
    overrides = await _load(OVERRIDES_YAML)
    assert overrides == ProfileOverrides(
        target_titles=["SDE II", "Senior Backend Engineer"],
        years_experience=4,
        primary_stack=["Python", "Go"],
    )


async def test_load_overrides_optional_fields_may_be_left_out() -> None:
    overrides = await _load(b"target_titles: [SDE II]\n")
    assert overrides == ProfileOverrides(target_titles=["SDE II"])


async def test_load_overrides_accepts_json_too() -> None:
    overrides = await _load(b'{"target_titles": ["SDE II"]}')
    assert overrides.target_titles == ["SDE II"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"target_titles: [unclosed", "not valid UTF-8 YAML", id="broken yaml"),
        pytest.param(b"\xff\xfe\x00", "not valid UTF-8 YAML", id="not utf-8"),
        pytest.param(b"- just\n- a list\n", "must be a YAML mapping", id="list"),
        pytest.param(b"", "must be a YAML mapping", id="empty file"),
        pytest.param(b"years_experience: 4\n", "target_titles (missing)", id="no titles"),
        pytest.param(
            b"target_titles: [SDE II]\nsalary: PRIVATE-123\n", "salary (extra_forbidden)", id="unknown field"
        ),
        pytest.param(
            b"target_titles: [SDE II]\nyears_experience: PRIVATE-123\n",
            "years_experience (float_parsing)",
            id="bad value",
        ),
    ],
)
async def test_bad_overrides_file_names_fields_never_values(body: bytes, message: str) -> None:
    with pytest.raises(OverridesError) as info:
        await _load(body)
    assert message in str(info.value)
    assert "PRIVATE-123" not in str(info.value)
    assert info.value.__cause__ is None


async def test_overrides_403_is_blocked() -> None:
    with pytest.raises(BlockedError):
        await _load(b"", status=403)


async def test_overrides_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await _load(OVERRIDES_YAML)
    assert "Senior Backend Engineer" not in caplog.text
    assert "FAKESIG" not in caplog.text
