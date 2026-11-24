"""Step 1 of processing (HLD §7): map every RawJob to one schema (NormalizedJob).

Titles: whitespace cleaned, plus a canonical form where "SDE II", "Software Dev Engineer 2" and
"Software Engineer - II" all read "Software Engineer II". Locations: see pipeline/locations.py;
unknown ones stay None and are counted in NormalizeStats (counts only, safe for public logs).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from jobagent.core.models import NormalizedJob, RawJob, SourceConfig
from jobagent.pipeline.locations import parse_location

CANONICAL_ROLE = "Software Engineer"
_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
_FROM_ROMAN = {roman.casefold(): n for n, roman in _ROMAN.items()}

# A role alias, optionally followed by its level. The level must sit right after the role:
# "Software Engineer - II", "SDE2", "SWE 3" — never a number elsewhere ("Web3", "Team 2").
_ROLE = re.compile(
    r"\b(?:software\s+development\s+engineer|software\s+dev\.?\s+engineer|software\s+engineer|sde|swe)"
    r"(?![a-z])"  # "SDET" is a different role
    r"(?:(?:\s*[-\u2013]\s*|\s+)(?P<roman>iv|v|i{1,3})\b|\s*[-\u2013]?\s*(?P<digit>[1-5])\b)?",
    re.IGNORECASE,
)
_SENIOR = re.compile(r"\b(?:sr|snr)\b\.?", re.IGNORECASE)


@dataclass(frozen=True)
class CanonicalTitle:
    title: str  # the original, whitespace cleaned
    canonical: str
    level: int | None


@dataclass(frozen=True)
class NormalizeStats:
    total: int = 0
    unknown_location: int = 0  # no country found at all (vague, regional or unparsed)
    multi_country: int = 0  # several countries found: known, `countries` holds them all
    remote: int = 0

    @property
    def unknown_share(self) -> float:
        """Fraction of jobs with no country (target: under 5%)."""
        return self.unknown_location / self.total if self.total else 0.0

    def __add__(self, other: "NormalizeStats") -> "NormalizeStats":
        return NormalizeStats(
            total=self.total + other.total,
            unknown_location=self.unknown_location + other.unknown_location,
            multi_country=self.multi_country + other.multi_country,
            remote=self.remote + other.remote,
        )

    def as_counts(self) -> dict[str, int]:
        """Numbers for Run.per_source_counts (counts only: safe for public logs)."""
        return {
            "normalized": self.total,
            "unknown_location": self.unknown_location,
            "multi_country": self.multi_country,
            "remote": self.remote,
        }

    def summary(self) -> str:
        return f"{self.total} jobs, {self.unknown_location} unknown location ({self.unknown_share:.1%})"


def source_key(config: SourceConfig) -> str:
    """Stable name of a listing source, e.g. "greenhouse:stripe"."""
    return f"{config.platform.value}:{config.tenant}"


def clean_title(raw: str) -> CanonicalTitle:
    title = re.sub(r"\s+,", ",", " ".join(raw.split()))
    levels: list[int] = []

    def role(match: re.Match[str]) -> str:
        level = _level(match)
        if level is None:
            return CANONICAL_ROLE
        levels.append(level)
        return f"{CANONICAL_ROLE} {_ROMAN[level]}"

    canonical = _SENIOR.sub("Senior", _ROLE.sub(role, title))
    return CanonicalTitle(title=title, canonical=canonical, level=levels[0] if levels else None)


def normalize(raw: RawJob, *, source: str) -> NormalizedJob:
    title = clean_title(raw.title)
    location = parse_location(raw.location_raw)
    return NormalizedJob(
        source=source,
        source_job_id=raw.source_job_id,
        company_slug=raw.company_slug,
        title=title.title,
        title_canonical=title.canonical,
        title_level=title.level,
        location_raw=raw.location_raw,
        country=location.country,
        countries=location.countries,
        city=location.city,
        remote=location.remote,
        url=raw.url,
        posted_at=raw.posted_at,
    )


def normalize_all(raws: Iterable[RawJob], *, source: str) -> tuple[list[NormalizedJob], NormalizeStats]:
    jobs = [normalize(raw, source=source) for raw in raws]
    stats = NormalizeStats(
        total=len(jobs),
        unknown_location=sum(not job.countries for job in jobs),
        multi_country=sum(len(job.countries) > 1 for job in jobs),
        remote=sum(job.remote for job in jobs),
    )
    return jobs, stats


def _level(match: re.Match[str]) -> int | None:
    if roman := match.group("roman"):
        return _FROM_ROMAN[roman.casefold()]
    if digit := match.group("digit"):
        return int(digit)
    return None
