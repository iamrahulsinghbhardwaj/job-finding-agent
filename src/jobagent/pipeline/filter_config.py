# Principle for the filter: drop only when confident.

"""Every list the rule filter uses (HLD §4, §7 step 3). Edit here; pipeline/filter.py holds the logic.

How phrases match: the title (its canonical form, so "Sr." reads "Senior" and "SDE 2" reads
"Software Engineer II") is lower-cased and every run of non-letters/digits becomes one space,
so "Back-End" reads "back end" and "AI/ML Engineer" reads "ai ml engineer". A phrase matches
only as whole words: "intern" does not hit "Internal Tools", "staff" does not hit "Staffing".
Write phrases in that same form: lower case, words separated by single spaces.
"""

from jobagent.core.models import Tier

# --- location (by country, ISO-2; "in scope" = ANY of the job's countries is listed) -----------

EUROPE_HUB_COUNTRIES = frozenset({"IE", "GB", "NL", "DE", "CH", "PL", "SE", "DK"})

# None = no location filter for that tier.
TIER_COUNTRIES: dict[Tier, frozenset[str] | None] = {
    Tier.T1: frozenset({"IN"}) | EUROPE_HUB_COUNTRIES,
    Tier.T1_STARTUP: None,
    Tier.OTHER: frozenset({"IN"}),
}

REMOTE_UNSPECIFIED_LABEL = "Remote (unspecified)"  # digest label for startup jobs with bare "Remote"

# --- title -------------------------------------------------------------------------------------

# A title must name an engineering role...
ENGINEERING_WORDS: tuple[str, ...] = (
    "engineer",
    "developer",
    "member of technical staff",  # OpenAI's standard title
)

# ...and match at least one of these...
TITLE_KEYWORDS: tuple[str, ...] = (
    "senior",
    "backend",
    "back end",
    "platform",
    "distributed systems",
    "ai engineer",
    "ml engineer",
    "machine learning engineer",
    "ml infra",
    "ml infrastructure",
    "machine learning infrastructure",
)

# ...or be SDE2: canonical "Software Engineer" at one of these levels ("SDE II", "SWE 2", ...)...
SDE2_LEVELS: frozenset[int] = frozenset({2})

# ...or be a plain engineering title (no level, no keyword: "Software Engineer, Payments") AND
# state min_yoe within the tier's YOE_RANGE (any stated value for tiers without one). The years
# stand in for the missing level, so an unknown min_yoe fails ("title_yoe_unknown").
PLAIN_TITLES: tuple[str, ...] = (
    "software engineer",  # canonical form: also SDE, SWE, Software Development Engineer
    "member of technical staff",
)

# Extra keywords for Tier 1 Startups only.
STARTUP_TITLE_KEYWORDS: tuple[str, ...] = ("founding",)

# A title containing any of these is dropped (all tiers).
EXCLUDED_WORDS: tuple[str, ...] = (
    "staff",
    "principal",
    "manager",
    "director",
    "vp",
    "vice president",
    "head of",
    "intern",
    "internship",
    # employment type only; "smart contract" is allowed below
    "contract",
    "contractor",
    "contract to hire",
)

# Phrases removed from the title BEFORE the exclusion check, so their words never exclude it.
EXCLUSION_ALLOWED: tuple[str, ...] = (
    "member of technical staff",
    "smart contract",
    "smart contracts",
)

# --- experience (min_yoe from the posting; unknown passes, except for PLAIN_TITLES) -----------

# Inclusive (low, high) bounds per tier. Missing tier = no rule. A posting asking "N+ years" is the
# point N; one giving a range "N-M years" (min_yoe..max_yoe) is in scope if it overlaps the bounds.
YOE_RANGE: dict[Tier, tuple[int, int]] = {
    Tier.T1: (3, 6),  # 3, not 4: Amazon's standard SDE II minimum is "3+ years"
}
