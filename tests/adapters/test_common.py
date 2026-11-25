import pytest

from jobagent.adapters.common import (
    ScopeError,
    YearsRequired,
    extract_min_yoe,
    extract_yoe,
    html_to_text,
    schema_hash,
    to_iso3,
)

Y = YearsRequired
UNKNOWN = YearsRequired()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the four from the brief
        ("5+ years of professional experience and 2+ years with Ruby", Y(5)),  # general beats skill
        ("3+ years non-internship professional software development experience", Y(3)),
        ("5+ years Java, 2+ years Kubernetes", Y(5)),  # skill only: the largest
        ("Minimum requirements\n2-12+ years", Y(2, 12)),
        # general experience
        ("- 3+ years of non-internship professional software development experience", Y(3)),  # Amazon
        ("You have 6+ years of professional software engineering experience", Y(6)),  # Stripe
        ("2\u201312+ years of industry software engineering experience (no internships)", Y(2, 12)),  # Stripe
        ("4+ years of relevant work experience", Y(4)),
        ("10+ years\u2019 experience in an AML/CFT role", Y(10)),  # possessive (Stripe)
        ("3+ years' professional experience", Y(3)),
        ("5 or more years of experience building distributed systems", Y(5)),  # plain "of experience"
        ("10+ YEARS OF EXPERIENCE leading teams", Y(10)),
        ("2 to 4 years of engineering experience", Y(2, 4)),
        ("Bachelor's AND 4+ years of experience OR Master's AND 3+ years of experience", Y(3)),  # paths
        # skill-specific
        ("1+ year of experience with Kubernetes", Y(1)),
        ("6+ yrs experience in backend services", Y(6)),
        ("5+ years of Java experience and 3+ years using Kafka", Y(5)),
        ("at least 4 years in backend roles", Y(4)),
        ("3+ years of C++", Y(3)),
        ("3+ years of programming using a modern programming language such as Java", Y(3)),  # Amazon
        # a part of a bigger figure is not a minimum
        (
            "You have 6+ years of professional software engineering experience, including 2+ years of "
            "industry experience building rich web experiences",
            Y(6),
        ),
        ("5+ years of experience, of which at least 2 years of management experience", Y(5)),
        # ambiguous or not a requirement -> unknown
        ("2-12+ years", UNKNOWN),  # bare figure outside a required section
        ("founded 10 years ago", UNKNOWN),
        ("for over 20 years", UNKNOWN),
        ("For over 20 years, our experience has shaped payments", UNKNOWN),
        ("We have 30 years of experience in banking", UNKNOWN),
        ("10+ years leading teams", UNKNOWN),
        ("5+ years of " + "x" * 70 + " experience", UNKNOWN),  # beyond the window
        ("Over 15 years. Experience with Go is a plus", UNKNOWN),
        ("Experience programming with at least one software programming language", UNKNOWN),
        ("Founded in 2015, we have grown to 500 people", UNKNOWN),
        ("", UNKNOWN),
        ("Celebrating 75 years of experience in innovation", UNKNOWN),  # above MAX_YOE
        # preferred never counts
        ("3+ years of experience in Java. 5+ years of experience preferred", Y(3)),
        ("5+ years of experience preferred, 3+ years of experience required", Y(3)),
        ("7+ years of experience is a plus", UNKNOWN),
        # bad ranges keep the minimum only
        ("4-2 years of professional experience", Y(4)),
    ],
)
def test_extract_yoe(text: str, expected: YearsRequired) -> None:
    assert extract_yoe(text) == expected
    assert extract_min_yoe(text) == expected.min


STRIPE_LIKE = """About the team
For over 20 years, we have led payments.
Minimum requirements
You have 6+ years of professional software engineering experience
Preferred qualifications
You have 10+ years of experience leading teams"""

AMAZON_LIKE = """Basic qualifications
- 3+ years of non-internship professional software development experience
- 2+ years of experience with Java
Preferred qualifications
- 1+ years of experience with AWS"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STRIPE_LIKE, Y(6)),
        (AMAZON_LIKE, Y(3)),  # general (3) beats the skill-specific 2
        ("Basic qualifications\n- 5+ years of Go\n- 2+ years with Postgres", Y(5)),  # skill only: max
        ("Intro\n4+ years of experience building APIs\nRequirements\n5+ years of backend experience", Y(5)),
        ("About you\n4+ years of experience building APIs", Y(4)),  # no sections: anywhere but preferred
        ("Preferred qualifications\n- 8+ years of experience\nAbout us\nWe ship.", UNKNOWN),
        ("Nice to have:\n5+ years of experience with Rust", UNKNOWN),
        ("Minimum requirements\n- 5+ years\n- 2+ years with Ruby", Y(5)),  # bare bullet = general here
    ],
)
def test_extract_yoe_sections(text: str, expected: YearsRequired) -> None:
    assert extract_yoe(text) == expected


def test_to_iso3() -> None:
    assert to_iso3("IN") == "IND"
    assert to_iso3("GB") == "GBR"
    assert to_iso3("DK") == "DNK"  # Copenhagen hub
    with pytest.raises(ScopeError, match="'US' is not supported"):
        to_iso3("US")


def test_html_to_text_keeps_inline_and_breaks_blocks() -> None:
    assert html_to_text("<p>a <b>bold</b> word</p><script>x()</script>") == "a bold word"
    assert html_to_text("- one<br/>- two<br>- three") == "- one\n- two\n- three"
    assert html_to_text("") == ""


def test_schema_hash_ignores_order_and_values() -> None:
    assert schema_hash({"a": 1, "b": 2}) == schema_hash({"b": "x", "a": None})
    assert schema_hash({"a": 1}) != schema_hash({"a": 1, "c": 3})
