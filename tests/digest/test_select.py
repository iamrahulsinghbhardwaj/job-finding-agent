"""build_digest without a database: sections, the High/Medium rule, merging, stars, subject."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from jobagent.digest.select import (
    EUROPE,
    INDIA,
    MEDIUM_FULL,
    DigestRow,
    build_digest,
    days_since,
    yoe_text,
)
from jobagent.pipeline.rank import RULE_MODEL

TODAY = date(2026, 11, 27)


def row(job_id: int, score: int = 80, **changes: object) -> DigestRow:
    base = DigestRow(
        job_id=job_id,
        score=score,
        fit="High" if score >= 75 else "Medium" if score >= 50 else "Low",
        reason=f"Reason {job_id}.",
        model="gpt-5.4-mini",
        title=f"Software Engineer {job_id}",
        company="Acme",
        company_slug="acme",
        us_transfer_path=False,
        country="IN",
        city="Bengaluru",
        location_raw="Bengaluru, India",
        min_yoe=3,
        max_yoe=None,
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        url=f"https://example.com/jobs/{job_id}",
        description_hash=f"hash-{job_id}",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def section(digest: object, name: str):
    assert digest is not None
    return next(s for s in digest.sections if s.name == name)  # type: ignore[attr-defined]


# --- the High / Medium rule (same for every section) ------------------------------------------


def test_all_high_full_top_10_medium_full_rest_compact() -> None:
    rows = [row(i, 90) for i in range(1, 4)] + [row(100 + i, 74 - i) for i in range(14)]
    india = section(build_digest(rows, TODAY), INDIA)
    assert [line.fit for line in india.full] == ["High"] * 3 + ["Medium"] * MEDIUM_FULL
    assert [line.score for line in india.full[3:]] == list(range(74, 64, -1))  # best Medium first
    assert [line.score for line in india.compact] == [64, 63, 62, 61]  # nothing held back
    assert india.count == 17


def test_more_than_ten_high_are_all_in_full() -> None:
    india = section(build_digest([row(i, 80 + i % 10) for i in range(25)], TODAY), INDIA)
    assert len(india.full) == 25
    assert india.compact == []


def test_low_is_never_in_the_digest() -> None:
    assert build_digest([row(1, 40)], TODAY) is None


def test_europe_follows_the_same_rule() -> None:
    rows = [row(i, 60, country="IE", city="Dublin") for i in range(13)]
    europe = section(build_digest(rows, TODAY), EUROPE)
    assert (len(europe.full), len(europe.compact)) == (MEDIUM_FULL, 3)


# --- sections ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "location_raw", "expected"),
    [
        ("IN", "Hyderabad, India", INDIA),
        ("GB", "London, UK", EUROPE),
        ("CH", "Zurich", EUROPE),
        (None, "London, UK / Dublin, Ireland", EUROPE),  # multi-country: re-parsed
        (None, "Bengaluru, India; Dublin, Ireland", INDIA),  # India first
        ("US", "Seattle, WA", None),  # neither section
    ],
)
def test_section_by_country(country: str | None, location_raw: str, expected: str | None) -> None:
    digest = build_digest([row(1, country=country, location_raw=location_raw)], TODAY)
    names = [s.name for s in digest.sections] if digest else []
    assert names == ([expected] if expected else [])


def test_star_only_in_europe_for_us_transfer_path_companies() -> None:
    rows = [
        row(1, us_transfer_path=True),
        row(2, us_transfer_path=True, country="IE", city="Dublin"),
        row(3, us_transfer_path=False, country="IE", city="Dublin"),
    ]
    digest = build_digest(rows, TODAY)
    india, europe = section(digest, INDIA), section(digest, EUROPE)
    assert [line.star for line in india.lines] == [False]
    assert {line.job_ids[0]: line.star for line in europe.lines} == {2: True, 3: False}
    assert europe.stars
    assert not india.stars


# --- merging: exact duplicates only ------------------------------------------------------------


def test_exact_duplicates_merge_into_one_line() -> None:
    same = {"title": "Software Development Engineer II", "company_slug": "amazon", "description_hash": "abc"}
    rows = [row(1, 82, **same), row(2, 86, **same)]
    [line] = section(build_digest(rows, TODAY), INDIA).lines
    assert line.job_ids == (1, 2)
    assert line.duplicates == 1
    assert (line.score, line.url) == (86, "https://example.com/jobs/2")  # the best one's link


def test_same_title_and_city_but_different_description_stay_separate() -> None:
    sde2 = {"title": "Software Development Engineer II", "company_slug": "amazon", "company": "Amazon"}
    rows = [row(1, 82, description_hash="aaa", **sde2), row(2, 86, description_hash="bbb", **sde2)]
    lines = section(build_digest(rows, TODAY), INDIA).lines
    assert [line.job_ids for line in lines] == [(2,), (1,)]
    assert all(line.duplicates == 0 for line in lines)


@pytest.mark.parametrize(
    "change", [{"city": "Hyderabad"}, {"title": "SDE III"}, {"company_slug": "other"}], ids=str
)
def test_any_other_difference_keeps_postings_apart(change: dict[str, str]) -> None:
    rows = [row(1, description_hash="same"), replace(row(2, description_hash="same"), **change)]
    assert len(section(build_digest(rows, TODAY), INDIA).lines) == 2


def test_jobs_without_a_description_hash_never_merge() -> None:
    rows = [row(1, description_hash=None, title="X"), row(2, description_hash=None, title="X")]
    assert len(section(build_digest(rows, TODAY), INDIA).lines) == 2


def test_every_merged_id_counts_as_emailed() -> None:
    rows = [row(1, description_hash="h", title="X"), row(2, description_hash="h", title="X"), row(3)]
    assert build_digest(rows, TODAY).job_ids == {1, 2, 3}  # type: ignore[union-attr]


# --- lines -------------------------------------------------------------------------------------


def test_rule_fallback_is_flagged() -> None:
    [line] = section(build_digest([row(1, 60, model=RULE_MODEL)], TODAY), INDIA).lines
    assert line.rule


def test_order_is_score_then_newest() -> None:
    rows = [
        row(1, 80, posted_at=datetime(2026, 11, 1, tzinfo=UTC)),
        row(2, 80, posted_at=datetime(2026, 11, 25, tzinfo=UTC)),
        row(3, 80, posted_at=None),
        row(4, 90),
    ]
    assert [line.job_ids[0] for line in section(build_digest(rows, TODAY), INDIA).lines] == [4, 2, 1, 3]


def test_location_text() -> None:
    digest = build_digest([row(1), row(2, city=None, country="IN", location_raw="India")], TODAY)
    assert [line.location for line in section(digest, INDIA).lines] == ["Bengaluru, IN", "IN"]


@pytest.mark.parametrize(
    ("min_yoe", "max_yoe", "text"),
    [(3, None, "3+ yrs"), (2, 12, "2\u201312 yrs"), (5, 5, "5+ yrs"), (None, None, "YOE n/a")],
)
def test_yoe_text(min_yoe: int | None, max_yoe: int | None, text: str) -> None:
    assert yoe_text(min_yoe, max_yoe) == text


def test_days_since() -> None:
    assert days_since(datetime(2026, 11, 20, tzinfo=UTC), TODAY) == 7
    assert days_since(None, TODAY) is None
    assert days_since(datetime(2026, 11, 28, tzinfo=UTC), TODAY) == 0  # never negative


# --- subject -----------------------------------------------------------------------------------


def test_subject_counts_lines_after_merging() -> None:
    rows = [row(i) for i in range(6)] + [row(10 + i, country="GB", city="London") for i in range(3)]
    rows.append(row(99, description_hash="hash-0", title="Software Engineer 0"))  # duplicate of row 0
    assert build_digest(rows, TODAY).subject == "9 new: 6 India, 3 Europe"  # type: ignore[union-attr]


def test_subject_leaves_out_empty_sections() -> None:
    assert build_digest([row(1), row(2)], TODAY).subject == "2 new: 2 India"  # type: ignore[union-attr]


def test_nothing_new_means_no_digest() -> None:
    assert build_digest([], TODAY) is None
