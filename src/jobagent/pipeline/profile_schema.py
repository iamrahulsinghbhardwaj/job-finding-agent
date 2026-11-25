"""The CV profile's shapes and the arithmetic on them. Pure functions, no I/O.

- `ExtractedProfile` is what the LLM fills (Structured Outputs): roles with their dates, skills,
  domains, projects, target titles. It holds no "years" and no "seniority": the model does not
  know today's date, so it cannot count time up to now.
- `years_of_experience()` counts that time IN CODE: full-time roles only (internships excluded),
  overlapping roles counted once, a current role (end None) up to today.
- `ProfileOverrides` is your private overrides file; `resolve()` merges it on top (override wins).
- `CandidateProfile` is the result, what the ranker compares each job against.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Seniority = Literal["junior", "mid", "senior", "staff_plus"]

# Seniority from years of experience: below 2 junior, below 5 mid, below 9 senior, else staff_plus.
SENIORITY_BANDS: tuple[tuple[float, Seniority], ...] = ((2, "junior"), (5, "mid"), (9, "senior"))
_DAYS_PER_YEAR = 365.25
_MONTH = re.compile(r"^(\d{4})(?:-(\d{2}))?$")
MONTH_PATTERN = _MONTH.pattern


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Job title as written in the CV.")
    company: str = Field(description="Employer as written in the CV.")
    start: str = Field(
        pattern=MONTH_PATTERN, description="Start month as YYYY-MM (YYYY alone if the CV gives only a year)."
    )
    end: str | None = Field(
        pattern=MONTH_PATTERN,
        description="End month as YYYY-MM (or YYYY); null when the role is current (Present, Now, ...).",
    )
    is_internship: bool = Field(
        description="True for internships, co-ops, apprenticeships and student positions."
    )


class ExtractedProfile(BaseModel):
    """What the LLM returns. No defaults: Structured Outputs fills every field."""

    model_config = ConfigDict(extra="forbid")

    roles: list[Role] = Field(description="Every position in the CV's work experience, newest first.")
    primary_stack: list[str] = Field(
        description="Languages, frameworks and platforms the candidate used most, most-used first."
    )
    secondary_skills: list[str] = Field(description="Other tools and skills that appear in the CV.")
    domains: list[str] = Field(
        description="Problem areas worked in, e.g. payments, distributed systems, developer tooling."
    )
    notable_projects: list[str] = Field(
        description="The strongest projects or achievements, one short line each, with scale or impact."
    )
    target_titles: list[str] = Field(
        description="Job titles the candidate fits next, e.g. 'SDE II', 'Senior Backend Engineer'."
    )


class ProfileOverrides(BaseModel):
    """Your private overrides file (YAML), merged on top of the extracted profile; override wins.

    target_titles: [SDE II, Senior Backend Engineer]   # required
    years_experience: 4                                 # optional
    primary_stack: [Python, Go]                         # optional
    """

    model_config = ConfigDict(extra="forbid")

    target_titles: list[str] = Field(min_length=1)
    years_experience: float | None = Field(default=None, ge=0, le=50)
    primary_stack: list[str] | None = Field(default=None, min_length=1)


class CandidateProfile(BaseModel):
    """What the ranker compares each job against."""

    model_config = ConfigDict(extra="forbid")

    years_experience: float  # computed from roles up to today, unless overridden
    seniority: Seniority  # from years_experience (SENIORITY_BANDS)
    primary_stack: list[str]
    secondary_skills: list[str]
    domains: list[str]
    notable_projects: list[str]
    target_titles: list[str]
    roles: list[Role]


def years_of_experience(roles: Iterable[Role], today: date) -> float:
    """Full-time years up to `today`, one decimal: internships out, overlaps counted once.

    Months are whole: a role from 2022-01 to 2022-12 is 12 months. A role with no end runs to
    today. Unreadable dates, a start in the future and an end before the start are skipped.
    """
    spans = sorted(span for role in roles if not role.is_internship and (span := _span(role, today)))
    days = 0
    covered_until: date | None = None
    for start, end in spans:
        if covered_until is not None and start < covered_until:
            start = covered_until  # overlap with an earlier role: count the rest only
        if end > start:
            days += (end - start).days
            covered_until = end
    return round(days / _DAYS_PER_YEAR, 1)


def seniority_for(years: float) -> Seniority:
    for below, level in SENIORITY_BANDS:
        if years < below:
            return level
    return "staff_plus"


def resolve(
    extracted: ExtractedProfile, overrides: ProfileOverrides | None, today: date
) -> tuple[CandidateProfile, tuple[str, ...]]:
    """The profile the ranker uses, and the names of the fields an override replaced."""
    values = extracted.model_dump()
    values["years_experience"] = years_of_experience(extracted.roles, today)
    overridden: list[str] = []
    if overrides is not None:
        for name, value in overrides.model_dump(exclude_none=True).items():
            values[name] = value
            overridden.append(name)
    values["seniority"] = seniority_for(values["years_experience"])
    return CandidateProfile.model_validate(values), tuple(overridden)


def _span(role: Role, today: date) -> tuple[date, date] | None:
    """[first day of the start month, first day after the end month), capped at today."""
    start = _month(role.start, first=True)
    end = today if role.end is None else _month(role.end, first=False)
    if start is None or end is None:
        return None
    end = min(end, today)
    return (start, end) if start < end else None


def _month(value: str, *, first: bool) -> date | None:
    """`first`: the month's first day; else the first day AFTER the month (an exclusive end).
    A bare year is January (start) or the end of December (end)."""
    match = _MONTH.match(value)
    if match is None:
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else (1 if first else 12)
    if not 1 <= month <= 12:
        return None
    if first:
        return date(year, month, 1)
    return (date(year, month, 28) + timedelta(days=4)).replace(day=1)


def effective_profile_hash(profile_version: int, overrides: ProfileOverrides | None) -> str:
    """sha256 of what the ranker actually used: the stored profile version + the overrides content.

    Stored on every Ranking row. The overrides are hashed as parsed values (key order, YAML layout
    and comments do not matter); no overrides hashes as null, so it is stable.
    """
    content = None if overrides is None else overrides.model_dump(exclude_none=True)
    material = json.dumps({"profile_version": profile_version, "overrides": content}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def overridden_text(fields: Sequence[str]) -> str:
    return ", ".join(fields) if fields else "none"
