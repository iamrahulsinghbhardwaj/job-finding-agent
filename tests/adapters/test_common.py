import pytest

from jobagent.adapters.common import ScopeError, extract_min_yoe, html_to_text, schema_hash, to_iso3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- 3+ years of non-internship professional software development experience", 3),
        ("You have 6+ years of professional software engineering experience", 6),
        ("5 or more years of experience building distributed systems", 5),
        ("1+ year of experience with Kubernetes", 1),
        ("at least 4 years in backend roles", 4),
        ("10+ YEARS leading teams", 10),
        ("Bachelor's AND 4+ years of experience OR Master's AND 3+ years of experience", 4),  # first path
        ("Experience programming with at least one software programming language", None),
        ("Founded in 2015, we have grown to 500 people", None),
        ("", None),
    ],
)
def test_extract_min_yoe(text: str, expected: int | None) -> None:
    assert extract_min_yoe(text) == expected


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
