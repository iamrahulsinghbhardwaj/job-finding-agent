"""Core data models shared by adapters, pipeline and storage.

All models are strict (unknown fields rejected), frozen (immutable once built) and strip
whitespace from strings, so bad registry entries or adapter output fail loudly and early.
"""

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Tier(StrEnum):
    """Brand and pay bar of a company. Set by hand in companies.yaml, never by code."""

    T1 = "T1"
    T1_STARTUP = "T1_STARTUP"
    OTHER = "OTHER"


class Platform(StrEnum):
    """Hiring platform a source is fetched from; one adapter per platform."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    EIGHTFOLD = "eightfold"
    AMAZON = "amazon"
    CUSTOM = "custom"


class SourceConfig(_Model):
    """Where to fetch one company's jobs: a platform plus its tenant (board token or domain)."""

    platform: Platform
    tenant: NonEmptyStr
    extra: dict[str, Any] = Field(default_factory=dict)


class Company(_Model):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: NonEmptyStr
    tier: Tier
    india_presence: bool
    europe_presence: bool
    us_transfer_path: bool
    # Empty is valid: startups are tracked even with no openings.
    sources: list[SourceConfig] = Field(default_factory=list)


class SearchSpec(_Model):
    keywords: list[str]
    countries: list[str]  # ISO 3166-1 alpha-2, upper case
    max_pages: int = Field(default=5, ge=1, le=50)

    @field_validator("countries", mode="before")
    @classmethod
    def _upper_countries(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [c.strip().upper() if isinstance(c, str) else c for c in value]
        return value

    @field_validator("countries")
    @classmethod
    def _check_iso2(cls, value: list[str]) -> list[str]:
        for code in value:
            if len(code) != 2 or not code.isascii() or not code.isalpha():
                raise ValueError(f"not an ISO-2 country code: {code!r}")
        return value


class RawJob(_Model):
    """A job as listed by a source, before normalization."""

    source_job_id: NonEmptyStr
    company_slug: NonEmptyStr
    title: NonEmptyStr
    location_raw: str
    url: HttpUrl
    posted_at: AwareDatetime | None
    raw: dict[str, Any]


class JobDetail(_Model):
    source_job_id: NonEmptyStr
    description_text: str
    min_yoe: int | None = Field(default=None, ge=0, le=50)


class HealthReport(_Model):
    ok: bool
    job_count: int = Field(ge=0)
    schema_hash: str
    error: str | None = None

    @model_validator(mode="after")
    def _error_means_not_ok(self) -> Self:
        if self.error is not None and self.ok:
            raise ValueError("a report with an error cannot be ok")
        return self
