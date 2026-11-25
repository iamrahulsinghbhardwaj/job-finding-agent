"""Step 3 of processing (HLD §7): the per-tier rule filter. Pure functions, no I/O.

The rules run in a fixed order and the first one that fails is the reason:

1. location: ANY of the job's countries is in the tier's list (TIER_COUNTRIES). A bare "Remote"
   with no country fails for Tier 1 and Other (reason "remote_unspecified") and passes for Tier 1
   Startups, which have no location filter (reason "remote_unspecified": label it in the digest).
2. excluded words (Staff, Principal, Manager, Director, VP, Head of, Intern, Contract, ...).
3. an engineering word (engineer, developer, member of technical staff).
4. a title keyword, or SDE2 (canonical "Software Engineer" at a level in SDE2_LEVELS);
   Tier 1 Startups also accept "founding". Failing those, a plain title (PLAIN_TITLES:
   "Software Engineer, Payments", "Member of Technical Staff") passes only with a stated min_yoe,
   which rule 5 then checks against the tier's range.
5. experience: the posting's years within the tier's YOE_RANGE; a range ("2-12+ years") passes if
   it overlaps. Unknown min_yoe passes.

Titles are matched on `title_canonical` (never displayed); the lists live in filter_config.
Reasons are short codes, safe for public logs ("excluded:manager", "yoe_high", ...).
"""

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from jobagent.core.models import Company, JobDetail, NormalizedJob, Tier
from jobagent.pipeline import filter_config as cfg

OK = "ok"
REMOTE_UNSPECIFIED = "remote_unspecified"
UNKNOWN_LOCATION = "unknown_location"
LOCATION = "location"
NOT_ENGINEERING = "not_engineering"
TITLE = "title"
TITLE_YOE_UNKNOWN = "title_yoe_unknown"  # plain title, and no stated years to stand in for a level
YOE_LOW = "yoe_low"
YOE_HIGH = "yoe_high"

_NON_WORD = re.compile(r"[^0-9a-z]+")


def passes(job: NormalizedJob, company: Company) -> tuple[bool, str]:
    """(passed, reason). A passing job's reason is "ok", or "remote_unspecified" for a startup job."""
    tier = company.tier
    in_scope, location = _location(job, tier)
    if not in_scope:
        return False, location

    title = words(job.title_canonical)
    allowed = title
    for phrase in cfg.EXCLUSION_ALLOWED:
        allowed = allowed.replace(f" {phrase} ", " ")
    for word in cfg.EXCLUDED_WORDS:
        if _has(allowed, word):
            return False, f"excluded:{word}"
    if not any(_has(title, word) for word in cfg.ENGINEERING_WORDS):
        return False, NOT_ENGINEERING
    keywords = cfg.TITLE_KEYWORDS + (cfg.STARTUP_TITLE_KEYWORDS if tier is Tier.T1_STARTUP else ())
    is_sde2 = job.title_level is not None and job.title_level in sde2_levels(company)
    if not is_sde2 and not any(_has(title, word) for word in keywords):
        if not any(_has(title, word) for word in cfg.PLAIN_TITLES):
            return False, TITLE
        if job.min_yoe is None:
            return False, TITLE_YOE_UNKNOWN

    experience = _experience(job.min_yoe, job.max_yoe, tier)
    if experience != OK:
        return False, experience
    return True, location


def sde2_levels(company: Company) -> frozenset[int]:
    """Levels that count as SDE2. Hook for a per-company level map later (Google's SWE III is
    the 4-6 YOE band, Amazon's SDE III is not); today every company uses SDE2_LEVELS."""
    return cfg.SDE2_LEVELS


def with_details(job: NormalizedJob, detail: JobDetail) -> NormalizedJob:
    """The job with min_yoe from its full posting, when the posting states one.

    Re-run passes() on filter survivors after get_details and before ranking: a listing may not
    carry the years, the full posting can.
    """
    if detail.min_yoe is None or (detail.min_yoe, detail.max_yoe) == (job.min_yoe, job.max_yoe):
        return job
    return job.model_copy(update={"min_yoe": detail.min_yoe, "max_yoe": detail.max_yoe})


@dataclass
class FilterReport:
    """Counts only (safe for public logs)."""

    before: Counter[str] = field(default_factory=Counter)  # company slug -> jobs in
    after: Counter[str] = field(default_factory=Counter)  # company slug -> jobs passed
    reasons: Counter[str] = field(default_factory=Counter)  # rejection reason -> jobs
    remote_unspecified: int = 0  # passed startup jobs to label "Remote (unspecified)"

    @property
    def total_before(self) -> int:
        return sum(self.before.values())

    @property
    def total_after(self) -> int:
        return sum(self.after.values())


def filter_jobs(
    jobs: Iterable[NormalizedJob], companies: Mapping[str, Company]
) -> tuple[list[NormalizedJob], FilterReport]:
    """Survivors in input order, plus counts per company and per rejection reason."""
    report = FilterReport()
    kept: list[NormalizedJob] = []
    for job in jobs:
        report.before[job.company_slug] += 1
        ok, reason = passes(job, companies[job.company_slug])
        if not ok:
            report.reasons[reason] += 1
            continue
        report.after[job.company_slug] += 1
        report.remote_unspecified += reason == REMOTE_UNSPECIFIED
        kept.append(job)
    return kept, report


def words(text: str) -> str:
    """Lower-cased, every run of non-letters/digits one space, padded: " senior backend engineer "."""
    return f" {_NON_WORD.sub(' ', text.casefold()).strip()} "


def _has(title_words: str, phrase: str) -> bool:
    return f" {phrase} " in title_words


def _location(job: NormalizedJob, tier: Tier) -> tuple[bool, str]:
    allowed = cfg.TIER_COUNTRIES[tier]
    if not job.countries:
        # Bare "Remote" (no country): dropped and counted for T1/Other, a labelled pass for startups.
        if job.remote:
            return allowed is None, REMOTE_UNSPECIFIED
        return (True, OK) if allowed is None else (False, UNKNOWN_LOCATION)
    if allowed is None or any(country in allowed for country in job.countries):
        return True, OK
    return False, LOCATION


def _experience(min_yoe: int | None, max_yoe: int | None, tier: Tier) -> str:
    bounds = cfg.YOE_RANGE.get(tier)
    if min_yoe is None or bounds is None:
        return OK
    low, high = bounds
    top = max_yoe if max_yoe is not None else min_yoe  # "3+ years" is the point 3, not 3..infinity
    if top < low:
        return YOE_LOW
    if min_yoe > high:
        return YOE_HIGH
    return OK
