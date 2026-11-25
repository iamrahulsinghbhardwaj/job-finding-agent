from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from jobagent.core.models import Company, JobDetail, NormalizedJob, Platform, RawJob, SourceConfig, Tier
from jobagent.core.registry import load_search_specs
from jobagent.pipeline import filter_config as cfg
from jobagent.pipeline.filter import filter_jobs, passes, with_details, words
from jobagent.pipeline.normalize import normalize

T1, START, OTHER = Tier.T1, Tier.T1_STARTUP, Tier.OTHER


def company(tier: Tier, slug: str = "acme") -> Company:
    return Company(
        slug=slug,
        name=slug.title(),
        tier=tier,
        india_presence=True,
        europe_presence=True,
        us_transfer_path=False,
        sources=[SourceConfig(platform=Platform.GREENHOUSE, tenant=slug)],
    )


def job(
    title: str,
    location: str = "Bengaluru, India",
    min_yoe: int | None = None,
    slug: str = "acme",
    max_yoe: int | None = None,
) -> NormalizedJob:
    """Through the real normalizer, so canonical titles and countries are what production sees."""
    raw = RawJob(
        source_job_id=f"{title}|{location}",
        company_slug=slug,
        title=title,
        location_raw=location,
        url=HttpUrl("https://example.com/job"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={},
        has_description=True,
        min_yoe=min_yoe,
        max_yoe=max_yoe,
    )
    return normalize(raw, source="greenhouse:acme")


# --- titles (location and experience held fine: Bengaluru, unknown YOE, Tier 1) -----------------

TITLES: list[tuple[str, bool, str]] = [
    # the two from the brief
    ("Senior Engineering Manager", False, "excluded:manager"),
    ("Software Engineer II - Backend", True, "ok"),
    # whole-word exclusions
    ("Internal Tools Engineer", False, "title"),  # "intern" must not hit "Internal": no keyword though
    ("Senior Internal Tools Engineer", True, "ok"),
    ("Software Engineering Intern", False, "excluded:intern"),
    ("Software Engineer Internship, Summer 2027", False, "excluded:internship"),
    ("Staffing Platform Engineer", True, "ok"),
    ("Staff Engineer", False, "excluded:staff"),
    ("Staff Software Engineer, Platform", False, "excluded:staff"),
    ("Principal Engineer, Distributed Systems", False, "excluded:principal"),
    ("Director, Platform Engineering", False, "excluded:director"),
    ("VP of Engineering", False, "excluded:vp"),
    ("Vice President, Backend Engineering", False, "excluded:vice president"),
    ("Head of Platform Engineering", False, "excluded:head of"),
    ("Backend Engineer (Contract)", False, "excluded:contract"),
    ("Senior Backend Engineer - Contractor", False, "excluded:contractor"),
    ("Backend Engineer, Contract-to-hire", False, "excluded:contract"),
    ("Senior Smart Contract Engineer", True, "ok"),  # "contract" only as employment type
    ("Smart Contracts Platform Engineer", True, "ok"),
    ("Senior Manager, Software Development", False, "excluded:manager"),
    # Member of Technical Staff: "staff" there is not an exclusion, and it is an engineering title
    ("Member of Technical Staff, Backend", True, "ok"),
    ("Senior Member of Technical Staff", True, "ok"),
    ("Member of Technical Staff", False, "title_yoe_unknown"),  # plain title: needs stated years
    # engineering word required
    ("Senior Accountant", False, "not_engineering"),
    ("Senior Product Designer, Platform", False, "not_engineering"),
    ("Platform Product Lead", False, "not_engineering"),
    ("Senior Backend Developer", True, "ok"),
    # keywords, on the canonical title
    ("Sr. Software Engineer", True, "ok"),
    ("Snr Software Engineer", True, "ok"),
    ("Back-End Engineer", True, "ok"),
    ("Backend / API Engineer, Radar", True, "ok"),
    ("Software Engineer, Distributed Systems", True, "ok"),
    ("AI Engineer", True, "ok"),
    ("AI/ML Engineer", True, "ok"),
    ("Machine Learning Engineer, Ranking", True, "ok"),
    ("ML Infra Engineer", True, "ok"),
    ("Software Engineer, ML Infrastructure", True, "ok"),
    ("Software Engineer, Frontend", False, "title_yoe_unknown"),
    ("Full-Stack Software Engineer, Communication Tools", False, "title_yoe_unknown"),
    ("Integration Engineer", False, "title"),  # not a plain title either
    ("Data Engineer", False, "title"),
    # SDE2: level 2 only
    ("SDE II", True, "ok"),
    ("SDE 2, Payments", True, "ok"),
    ("Software Development Engineer II", True, "ok"),
    ("SWE-2", True, "ok"),
    ("Software Engineer I", False, "title_yoe_unknown"),  # not SDE2: falls back to the plain rule
    ("SDE III", False, "title_yoe_unknown"),
    ("Software Engineer", False, "title_yoe_unknown"),
    # founding: startups only
    ("Founding Engineer", False, "title"),
]


@pytest.mark.parametrize(("title", "ok", "reason"), TITLES)
def test_title_rules_tier1(title: str, ok: bool, reason: str) -> None:
    assert passes(job(title), company(T1)) == (ok, reason)


@pytest.mark.parametrize(
    ("title", "ok", "reason"),
    [
        ("Founding Engineer", True, "ok"),
        ("Founding Software Engineer", True, "ok"),
        ("Founding Product Designer", False, "not_engineering"),
        ("Founding Engineer (Contract)", False, "excluded:contract"),
        ("Senior Engineering Manager", False, "excluded:manager"),
        ("Software Engineer II - Backend", True, "ok"),
        ("Staff Engineer", False, "excluded:staff"),
    ],
)
def test_title_rules_startups(title: str, ok: bool, reason: str) -> None:
    assert passes(job(title, location="San Francisco, CA"), company(START)) == (ok, reason)


def test_founding_is_startup_only() -> None:
    assert passes(job("Founding Engineer"), company(OTHER)) == (False, "title")


# --- locations ------------------------------------------------------------------------------------

GOOD = "Senior Backend Engineer"

LOCATIONS: list[tuple[Tier, str, bool, str]] = [
    (T1, "Bengaluru, India", True, "ok"),
    (T1, "Remote - India", True, "ok"),
    (T1, "Dublin", True, "ok"),
    (T1, "London or Dublin", True, "ok"),
    (T1, "Munich, Germany", True, "ok"),  # Europe by country, not hub city
    (T1, "Copenhagen", True, "ok"),
    (T1, "Zürich", True, "ok"),
    (T1, "San Francisco, CA", False, "location"),
    (T1, "Dublin, CA", False, "location"),  # the US one
    (T1, "US / Canada", False, "location"),
    (T1, "New York or London", True, "ok"),  # ANY country in scope
    (T1, "Paris, France", False, "location"),
    (T1, "Remote", False, "remote_unspecified"),
    (T1, "Multiple Locations, EMEA", False, "unknown_location"),
    (T1, "", False, "unknown_location"),
    (START, "San Francisco, CA", True, "ok"),
    (START, "Remote", True, "remote_unspecified"),  # kept, labelled "Remote (unspecified)"
    (START, "Multiple Locations, EMEA", True, "ok"),
    (START, "Paris, France", True, "ok"),
    (OTHER, "Hyderabad, Telangana, IND", True, "ok"),
    (OTHER, "Dublin", False, "location"),
    (OTHER, "London or Bengaluru", True, "ok"),
    (OTHER, "Remote", False, "remote_unspecified"),
    (OTHER, "AMER", False, "unknown_location"),
]


@pytest.mark.parametrize(("tier", "location", "ok", "reason"), LOCATIONS)
def test_location_rules(tier: Tier, location: str, ok: bool, reason: str) -> None:
    assert passes(job(GOOD, location=location), company(tier)) == (ok, reason)


def test_location_is_checked_before_title() -> None:
    assert passes(job("Senior Engineering Manager", "Paris, France"), company(T1)) == (False, "location")


def test_filter_countries_match_the_crawl_scope() -> None:
    """The filter and companies.yaml's search block must agree, or we crawl what we then drop."""
    specs = load_search_specs(Path(__file__).resolve().parents[2] / "companies.yaml")
    for tier in (T1, OTHER):
        assert cfg.TIER_COUNTRIES[tier] == frozenset(specs[tier].countries)
    assert cfg.TIER_COUNTRIES[START] is None


# --- experience -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "min_yoe", "ok", "reason"),
    [
        (T1, None, True, "ok"),  # unknown passes
        (T1, 2, False, "yoe_low"),
        (T1, 3, True, "ok"),  # Amazon's SDE II minimum
        (T1, 4, True, "ok"),
        (T1, 6, True, "ok"),
        (T1, 7, False, "yoe_high"),
        (START, 2, True, "ok"),  # no YOE rule outside Tier 1
        (START, 10, True, "ok"),
        (OTHER, 2, True, "ok"),
        (OTHER, 10, True, "ok"),
    ],
)
def test_experience_rules(tier: Tier, min_yoe: int | None, ok: bool, reason: str) -> None:
    assert passes(job(GOOD, min_yoe=min_yoe), company(tier)) == (ok, reason)


PLAIN = [
    "Software Engineer",
    "Software Engineer, Payments",
    "Software Development Engineer, Alexa for Shopping",
    "Member of Technical Staff",
]


@pytest.mark.parametrize("title", PLAIN)
@pytest.mark.parametrize(
    ("tier", "min_yoe", "ok", "reason"),
    [
        (T1, None, False, "title_yoe_unknown"),  # unknown years: fail
        (T1, 2, False, "yoe_low"),
        (T1, 3, True, "ok"),
        (T1, 6, True, "ok"),
        (T1, 7, False, "yoe_high"),
        (OTHER, None, False, "title_yoe_unknown"),
        (OTHER, 10, True, "ok"),  # no range for Other: any stated value
        (START, None, False, "title_yoe_unknown"),
        (START, 1, True, "ok"),
    ],
)
def test_plain_titles_need_stated_years_in_range(
    title: str, tier: Tier, min_yoe: int | None, ok: bool, reason: str
) -> None:
    location = "San Francisco, CA" if tier is START else "Bengaluru, India"
    assert passes(job(title, location, min_yoe=min_yoe), company(tier)) == (ok, reason)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "SDE II",
        "Software Engineer, Backend",
        "Member of Technical Staff, Backend",
    ],
)
def test_keyword_titles_do_not_need_years(title: str) -> None:
    assert passes(job(title), company(T1)) == (True, "ok")


def test_plain_title_rule_never_rescues_non_plain_titles() -> None:
    assert passes(job("Integration Engineer", min_yoe=4), company(T1)) == (False, "title")


@pytest.mark.parametrize(
    ("low", "high", "ok", "reason"),
    [
        (2, 12, True, "ok"),  # Stripe's "2-12+ years": overlaps 3..6
        (2, 3, True, "ok"),  # touches the floor
        (6, 9, True, "ok"),  # touches the cap
        (1, 2, False, "yoe_low"),
        (7, 10, False, "yoe_high"),
        (2, None, False, "yoe_low"),  # "2+ years" is the point 2, not 2..infinity
    ],
)
def test_ranges_pass_when_they_overlap_the_tier(low: int, high: int | None, ok: bool, reason: str) -> None:
    for title in (GOOD, "Software Engineer, Payments and Risk"):  # keyword and plain titles alike
        assert passes(job(title, min_yoe=low, max_yoe=high), company(T1)) == (ok, reason)


def test_details_bring_the_range_too() -> None:
    detail = JobDetail(source_job_id="x", description_text="...", min_yoe=2, max_yoe=12)
    detailed = with_details(job("Software Engineer", min_yoe=8), detail)
    assert (detailed.min_yoe, detailed.max_yoe) == (2, 12)
    assert passes(detailed, company(T1)) == (True, "ok")


def test_title_is_checked_before_experience() -> None:
    assert passes(job("Staff Engineer", min_yoe=10), company(T1)) == (False, "excluded:staff")


def test_details_reapply_the_yoe_rule() -> None:
    """Listing says nothing (passes); the full posting says 8+ years: dropped before ranking."""
    survivor = job(GOOD)
    assert passes(survivor, company(T1)) == (True, "ok")
    detailed = with_details(survivor, JobDetail(source_job_id="x", description_text="...", min_yoe=8))
    assert detailed.min_yoe == 8
    assert passes(detailed, company(T1)) == (False, "yoe_high")


def test_details_without_years_keep_the_listing_value() -> None:
    survivor = job(GOOD, min_yoe=5)
    assert with_details(survivor, JobDetail(source_job_id="x", description_text="...")) is survivor


# --- report ---------------------------------------------------------------------------------------


def test_filter_jobs_counts_per_company_and_reason() -> None:
    companies = {"big": company(T1, "big"), "tiny": company(START, "tiny")}
    jobs = [
        job("Senior Backend Engineer", slug="big"),
        job("Senior Engineering Manager", slug="big"),
        job("Staff Engineer", slug="big"),
        job("Senior Backend Engineer", "Remote", slug="big"),
        job("Founding Engineer", "Remote", slug="tiny"),
        job("Account Executive", slug="tiny"),
    ]
    kept, report = filter_jobs(jobs, companies)
    assert [j.title for j in kept] == ["Senior Backend Engineer", "Founding Engineer"]
    assert report.before == {"big": 4, "tiny": 2}
    assert report.after == {"big": 1, "tiny": 1}
    assert report.total_before == 6
    assert report.total_after == 2
    assert report.reasons == {
        "excluded:manager": 1,
        "excluded:staff": 1,
        "remote_unspecified": 1,
        "not_engineering": 1,
    }
    assert report.remote_unspecified == 1


def test_words_form() -> None:
    assert words("AI/ML Engineer (Back-End)") == " ai ml engineer back end "


def test_config_phrases_are_in_words_form() -> None:
    """A phrase written "Back-End" or "head  of" could never match: catch typos in the config."""
    phrases = (
        cfg.ENGINEERING_WORDS
        + cfg.TITLE_KEYWORDS
        + cfg.PLAIN_TITLES
        + cfg.STARTUP_TITLE_KEYWORDS
        + cfg.EXCLUDED_WORDS
        + cfg.EXCLUSION_ALLOWED
    )
    for phrase in phrases:
        assert words(phrase).strip() == phrase, phrase
