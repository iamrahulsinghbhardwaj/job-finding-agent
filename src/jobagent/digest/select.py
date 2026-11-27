"""Which ranked jobs go into the digest, grouped into sections (HLD §8). Reads the DB, no I/O else.

A job qualifies when it is active, belongs to a Tier 1 company, is ranked High or Medium against
the CURRENT effective profile hash, and is in no past Run.emailed_job_ids. Sections (MVP):
Tier 1 India (country IN) and Tier 1 Europe (a hub country; star for us_transfer_path).

Every section follows one rule: all High jobs in full; the top MEDIUM_FULL Medium jobs in full;
every other Medium job as a compact row (score, company, title, city, link). Nothing is held back:
everything in the digest counts as emailed once it is sent.

Exact duplicates (same company, title, city and description hash) are one line: the best score,
"+N identical posting(s)", and every job id. Jobs without a description hash are never merged.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobagent.db import repo
from jobagent.db.models import Company, Job, Ranking, Run
from jobagent.pipeline import filter_config as cfg
from jobagent.pipeline.locations import parse_location
from jobagent.pipeline.rank import RULE_MODEL

MEDIUM_FULL = 10
FITS = ("High", "Medium")
INDIA = "Tier 1 — India"
EUROPE = "Tier 1 — Europe"
SUBJECT_NAMES = {INDIA: "India", EUROPE: "Europe"}


@dataclass(frozen=True)
class DigestLine:
    job_ids: tuple[int, ...]  # every posting this line stands for (identical duplicates merged)
    score: int
    fit: str
    company: str
    title: str
    location: str
    yoe: str
    days_posted: int | None
    reason: str
    url: str  # from source data, never from the LLM
    star: bool = False  # Europe: the company has a US transfer path
    rule: bool = False  # scored by the rule fallback, not the LLM

    @property
    def duplicates(self) -> int:
        return len(self.job_ids) - 1


@dataclass(frozen=True)
class Section:
    name: str
    full: list[DigestLine] = field(default_factory=list)
    compact: list[DigestLine] = field(default_factory=list)
    stars: bool = False  # show the star legend

    @property
    def count(self) -> int:
        return len(self.full) + len(self.compact)

    @property
    def lines(self) -> list[DigestLine]:
        return self.full + self.compact


@dataclass(frozen=True)
class Digest:
    sections: list[Section]
    subject: str
    today: date

    @property
    def job_ids(self) -> frozenset[int]:
        """Everything rendered: the ids to record as emailed once the digest is sent."""
        return frozenset(i for s in self.sections for line in s.lines for i in line.job_ids)

    @property
    def count(self) -> int:
        return sum(s.count for s in self.sections)


@dataclass(frozen=True)
class DigestRow:
    job_id: int
    score: int
    fit: str
    reason: str
    model: str
    title: str
    company: str
    company_slug: str
    us_transfer_path: bool
    country: str | None
    city: str | None
    location_raw: str
    min_yoe: int | None
    max_yoe: int | None
    posted_at: datetime | None
    url: str
    description_hash: str | None


def emailed_job_ids(session: Session) -> frozenset[int]:
    """Every job id recorded as emailed by any past run."""
    ids = session.scalars(select(func.unnest(Run.emailed_job_ids)).distinct())
    return frozenset(int(i) for i in ids)


def select_digest(session: Session, effective_profile_hash: str, today: date) -> Digest | None:
    """The next digest, or None when there is nothing new to send."""
    emailed = emailed_job_ids(session)
    rows = [r for r in _rows(session, effective_profile_hash) if r.job_id not in emailed]
    return build_digest(rows, today)


def build_digest(rows: Iterable[DigestRow], today: date) -> Digest | None:
    """Pure: qualified rows -> sections, merged and ordered; None when empty."""
    by_section: dict[str, list[DigestRow]] = {INDIA: [], EUROPE: []}
    for row in rows:
        section = _section_of(row)
        if section is not None and row.fit in FITS:  # Low never goes out, whatever the caller passed
            by_section[section].append(row)
    sections = [
        _section(name, items, today, stars=name == EUROPE) for name, items in by_section.items() if items
    ]
    sections = [s for s in sections if s.count]
    if not sections:
        return None
    return Digest(sections, subject_for(sections), today)


def subject_for(sections: Sequence[Section]) -> str:
    """E.g. "9 new: 6 India, 3 Europe" (lines after merging; empty sections left out)."""
    parts = [f"{s.count} {SUBJECT_NAMES.get(s.name, s.name)}" for s in sections if s.count]
    return f"{sum(s.count for s in sections)} new: {', '.join(parts)}"


def yoe_text(min_yoe: int | None, max_yoe: int | None) -> str:
    if min_yoe is None:
        return "YOE n/a"
    if max_yoe is not None and max_yoe != min_yoe:
        return f"{min_yoe}\u2013{max_yoe} yrs"
    return f"{min_yoe}+ yrs"


def days_since(posted_at: datetime | None, today: date) -> int | None:
    return None if posted_at is None else max((today - posted_at.date()).days, 0)


# --- internals -----------------------------------------------------------------------------------


def _rows(session: Session, effective_profile_hash: str) -> list[DigestRow]:
    stmt = (
        select(
            Job.id,
            Ranking.score,
            Ranking.fit,
            Ranking.reason,
            Ranking.model,
            Job.title,
            Company.name,
            Company.slug,
            Company.us_transfer_path,
            Job.country,
            Job.city,
            Job.location_raw,
            Job.min_yoe,
            Job.max_yoe,
            Job.posted_at,
            Job.url,
            Job.description_hash,
            Job.description_text,
        )
        .join(Job, Job.id == Ranking.job_id)
        .join(Company, Company.id == Job.company_id)
        .where(
            Ranking.effective_profile_hash == effective_profile_hash,
            Ranking.fit.in_(FITS),
            Job.status == repo.ACTIVE,
            Company.tier == "T1",
        )
    )
    rows = []
    for r in session.execute(stmt):
        rows.append(
            DigestRow(
                job_id=r.id,
                score=r.score,
                fit=r.fit,
                reason=r.reason,
                model=r.model,
                title=r.title,
                company=r.name,
                company_slug=r.slug,
                us_transfer_path=r.us_transfer_path,
                country=r.country,
                city=r.city,
                location_raw=r.location_raw,
                min_yoe=r.min_yoe,
                max_yoe=r.max_yoe,
                posted_at=r.posted_at,
                url=r.url,
                # older rows have the text but no stored hash: hash the text
                description_hash=r.description_hash or repo.description_hash(r.description_text),
            )
        )
    return rows


def _section_of(row: DigestRow) -> str | None:
    countries = (row.country,) if row.country else parse_location(row.location_raw).countries
    if "IN" in countries:
        return INDIA
    if any(c in cfg.EUROPE_HUB_COUNTRIES for c in countries):
        return EUROPE
    return None


def _section(name: str, rows: list[DigestRow], today: date, *, stars: bool) -> Section:
    lines = sorted(
        (_line(group, today, stars) for group in _merge(rows)),
        key=lambda line: (-line.score, _age(line), line.title),
    )
    high = [line for line in lines if line.fit == "High"]
    medium = [line for line in lines if line.fit == "Medium"]
    return Section(
        name,
        full=high + medium[:MEDIUM_FULL],
        compact=medium[MEDIUM_FULL:],
        stars=stars and any(line.star for line in lines),
    )


def _age(line: DigestLine) -> int:
    """Newer first among equal scores; unknown posting date last."""
    return line.days_posted if line.days_posted is not None else 10**6


def _merge(rows: list[DigestRow]) -> list[list[DigestRow]]:
    """Exact duplicates only: same company, title, city and description hash."""
    groups: dict[object, list[DigestRow]] = {}
    for row in rows:
        key: object = (
            (row.company_slug, row.title.casefold(), row.city, row.description_hash)
            if row.description_hash
            else ("job", row.job_id)
        )
        groups.setdefault(key, []).append(row)
    return list(groups.values())


def _line(group: list[DigestRow], today: date, stars: bool) -> DigestLine:
    best = max(group, key=lambda r: (r.score, -(r.job_id)))
    place = ", ".join(p for p in (best.city, best.country) if p)
    location = place or best.location_raw[:60] or "Location n/a"
    return DigestLine(
        job_ids=tuple(sorted(r.job_id for r in group)),
        score=best.score,
        fit=best.fit,
        company=best.company,
        title=best.title,
        location=location,
        yoe=yoe_text(best.min_yoe, best.max_yoe),
        days_posted=days_since(best.posted_at, today),
        reason=best.reason,
        url=best.url,
        star=stars and best.us_transfer_path,
        rule=best.model == RULE_MODEL,
    )
