"""Years of experience in code, seniority bands and the overrides merge. Fixed dates only."""

from datetime import date
from typing import Any

import pytest
from openai.lib._parsing._completions import type_to_response_format_param
from pydantic import ValidationError

from jobagent.pipeline.profile_schema import (
    ExtractedProfile,
    ProfileOverrides,
    Role,
    effective_profile_hash,
    resolve,
    seniority_for,
    years_of_experience,
)

TODAY = date(2026, 11, 26)


def role(start: str, end: str | None, *, intern: bool = False, title: str = "Software Engineer") -> Role:
    return Role(title=title, company="ExampleCo", start=start, end=end, is_internship=intern)


def extracted(*roles: Role) -> ExtractedProfile:
    return ExtractedProfile(
        roles=list(roles),
        primary_stack=["Python", "Go"],
        secondary_skills=["Kubernetes"],
        domains=["payments"],
        notable_projects=["Payments ledger, 1M tx/day"],
        target_titles=["Software Engineer"],
    )


# --- years of experience -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "years"),
    [
        pytest.param([], 0.0, id="no roles"),
        pytest.param([role("2022-01", "2022-12")], 1.0, id="Jan..Dec is 12 whole months"),
        pytest.param([role("2022-11", None)], 4.1, id="current role runs to today"),
        pytest.param(
            [role("2022-11", None), role("2021-07", "2022-10")], 5.4, id="two back-to-back roles add up"
        ),
        pytest.param(
            [role("2022-11", None), role("2022-06", "2022-10", intern=True)], 4.1, id="internship excluded"
        ),
        pytest.param(
            [role("2020-01", "2021-12"), role("2021-01", "2022-12")], 3.0, id="overlap counted once"
        ),
        pytest.param([role("2020-01", "2023-12"), role("2021-01", "2021-06")], 4.0, id="role inside a role"),
        pytest.param([role("2019-01", "2019-12"), role("2022-01", "2022-12")], 2.0, id="gap not counted"),
        pytest.param([role("2021", "2021")], 1.0, id="bare years: Jan..Dec"),
        pytest.param([role("2027-01", None)], 0.0, id="start in the future skipped"),
        pytest.param([role("2025-11", "2027-06")], 1.1, id="end in the future capped at today"),
        pytest.param([role("2023-05", "2022-01")], 0.0, id="end before start skipped"),
        pytest.param([role("2022-13", None)], 0.0, id="impossible month skipped"),
    ],
)
def test_years_of_experience(roles: list[Role], years: float) -> None:
    assert years_of_experience(roles, TODAY) == years


def test_years_grow_with_today_without_a_rebuild() -> None:
    roles = [role("2022-11", None)]
    assert years_of_experience(roles, date(2026, 11, 26)) == 4.1
    assert years_of_experience(roles, date(2027, 11, 26)) == 5.1


def test_role_date_must_be_year_month_or_year() -> None:
    for bad in ("Nov 2022", "2022/11", "11-2022", "2022-1"):
        with pytest.raises(ValidationError):
            role(bad, None)


# --- seniority ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("years", "level"),
    [
        (0, "junior"),
        (1.9, "junior"),
        (2, "mid"),
        (4.1, "mid"),
        (5, "senior"),
        (8.9, "senior"),
        (9, "staff_plus"),
    ],
)
def test_seniority_bands(years: float, level: str) -> None:
    assert seniority_for(years) == level


# --- resolve: computed years, then overrides ----------------------------------------------------


def test_resolve_without_overrides_computes_years_and_seniority() -> None:
    profile, overridden = resolve(extracted(role("2022-11", None)), None, TODAY)
    assert (profile.years_experience, profile.seniority) == (4.1, "mid")
    assert profile.target_titles == ["Software Engineer"]
    assert profile.roles == [role("2022-11", None)]
    assert overridden == ()


def test_overrides_win() -> None:
    overrides = ProfileOverrides(
        target_titles=["SDE II", "Senior Backend Engineer"], years_experience=5.5, primary_stack=["Java"]
    )
    profile, overridden = resolve(extracted(role("2022-11", None)), overrides, TODAY)
    assert profile.target_titles == ["SDE II", "Senior Backend Engineer"]
    assert profile.years_experience == 5.5
    assert profile.seniority == "senior"  # from the overridden years
    assert profile.primary_stack == ["Java"]
    assert profile.secondary_skills == ["Kubernetes"]  # not overridable: kept
    assert overridden == ("target_titles", "years_experience", "primary_stack")


def test_optional_overrides_left_out_keep_the_extracted_values() -> None:
    profile, overridden = resolve(
        extracted(role("2022-11", None)), ProfileOverrides(target_titles=["SDE II"]), TODAY
    )
    assert profile.years_experience == 4.1
    assert profile.primary_stack == ["Python", "Go"]
    assert overridden == ("target_titles",)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({}, id="target_titles required"),
        pytest.param({"target_titles": []}, id="target_titles empty"),
        pytest.param({"target_titles": ["SDE II"], "years_experience": -1}, id="negative years"),
        pytest.param({"target_titles": ["SDE II"], "primary_stack": []}, id="empty stack"),
        pytest.param({"target_titles": ["SDE II"], "domains": ["x"]}, id="field not overridable"),
    ],
)
def test_invalid_overrides_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProfileOverrides.model_validate(data)


# --- the LLM schema ----------------------------------------------------------------------------


def test_extracted_profile_is_valid_for_structured_outputs() -> None:
    # The SDK raises here if the model breaks strict-mode rules (defaults, open objects, ...).
    response_format: Any = type_to_response_format_param(ExtractedProfile)
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert set(schema["required"]) == set(ExtractedProfile.model_fields)
    assert "years_experience" not in schema["properties"]  # computed in code, never by the model
    assert "seniority" not in schema["properties"]
    role_schema = schema["$defs"]["Role"]
    assert set(role_schema["required"]) == {"title", "company", "start", "end", "is_internship"}


# --- effective profile hash (stored on every Ranking row) ---------------------------------------


def test_no_overrides_hash_is_stable() -> None:
    assert effective_profile_hash(3, None) == effective_profile_hash(3, None)
    assert len(effective_profile_hash(3, None)) == 64


def test_changing_overrides_changes_the_hash() -> None:
    titles = effective_profile_hash(3, ProfileOverrides(target_titles=["SDE II"]))
    other_titles = effective_profile_hash(3, ProfileOverrides(target_titles=["Senior Backend Engineer"]))
    with_years = effective_profile_hash(3, ProfileOverrides(target_titles=["SDE II"], years_experience=4))
    assert len({effective_profile_hash(3, None), titles, other_titles, with_years}) == 4


def test_profile_version_changes_the_hash() -> None:
    overrides = ProfileOverrides(target_titles=["SDE II"])
    assert effective_profile_hash(3, overrides) != effective_profile_hash(4, overrides)
    assert effective_profile_hash(3, None) != effective_profile_hash(4, None)


def test_same_overrides_content_same_hash() -> None:
    # Parsed values are hashed: field order (and so YAML layout and comments) does not matter.
    a = ProfileOverrides.model_validate({"target_titles": ["SDE II"], "years_experience": 4})
    b = ProfileOverrides.model_validate({"years_experience": 4.0, "target_titles": ["SDE II"]})
    assert effective_profile_hash(3, a) == effective_profile_hash(3, b)
