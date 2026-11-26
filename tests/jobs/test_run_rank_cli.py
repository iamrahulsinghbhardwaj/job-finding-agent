from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import HttpUrl

from jobagent.core.llm import LlmCall, LlmLedger
from jobagent.core.models import Company, Platform, RawJob, SourceConfig, Tier
from jobagent.jobs.run import RANK_OPTIONS, format_rank, format_top, in_ci
from jobagent.pipeline.normalize import normalize
from jobagent.pipeline.rank import RULE_MODEL, Ranked, RankReport, RankRun

COMPANY = Company(
    slug="stripe",
    name="Stripe",
    tier=Tier.T1,
    india_presence=True,
    europe_presence=True,
    us_transfer_path=False,
    sources=[SourceConfig(platform=Platform.GREENHOUSE, tenant="stripe")],
)


def ranked(score: int, title: str, *, fallback: str | None = None) -> Ranked:
    raw = RawJob(
        source_job_id=title,
        company_slug="stripe",
        title=title,
        location_raw="Dublin",
        url=HttpUrl(f"https://example.com/{score}"),
        posted_at=datetime(2026, 11, 20, tzinfo=UTC),
        raw={},
    )
    job = normalize(raw, source="greenhouse:stripe")
    fit = "High" if score >= 75 else "Medium" if score >= 50 else "Low"
    model = RULE_MODEL if fallback else "gpt-5.4-nano"
    return Ranked(score, job, COMPANY, score, fit, f"Reason for {title}.", model, fallback=fallback)


def run(*items: Ranked) -> RankRun:
    report = RankReport(
        candidates=len(items),
        llm=sum(r.fallback is None for r in items),
        fallback=Counter(r.fallback for r in items if r.fallback),
        truncated=1,
        budget_limit=1_000_000,
        budget_used=12_345,
    )
    return RankRun(list(items), report)


def ledger() -> LlmLedger:
    return LlmLedger([LlmCall("rank", "gpt-5.4-nano", 2000, 1500, 100, 800, Decimal("0.0002"))])


def test_rank_summary_is_counts_only() -> None:
    items = run(ranked(88, "Senior Backend Engineer"), ranked(40, "Platform Engineer", fallback="budget"))
    text = format_rank(items, ledger())
    assert "ranked by LLM: 1; rule score: 1 (budget 1)" in text
    assert "2,000 in (75% cached) / 100 out" in text
    assert "budget 12,345 of 1,000,000" in text
    assert "cost $0.0002" in text
    assert "Senior Backend Engineer" not in text  # no job data


def test_top_jobs_sorted_by_score_with_fit_reason_and_link() -> None:
    items = [ranked(s, f"Job {s}") for s in (40, 91, 66, 75)] + [ranked(55, "Rule job", fallback="llm_error")]
    text = format_top(run(*items), n=3)
    lines = text.splitlines()
    assert lines[1] == "top 3 by score"
    assert lines[2].split()[:2] == ["91", "High"]
    assert "Stripe | Job 91" in lines[2]
    assert lines[3].strip() == "Reason for Job 91."
    assert lines[4].strip() == "https://example.com/91"
    assert [line.split()[0] for line in lines[2::3]] == ["91", "75", "66"]


def test_rule_scores_are_marked_in_the_top_list() -> None:
    assert "Rule job [rule]" in format_top(run(ranked(55, "Rule job", fallback="budget")))


def test_no_ranked_jobs_prints_nothing() -> None:
    assert format_top(run()) == ""


@pytest.mark.parametrize(
    ("value", "expected"), [("true", True), ("True", True), ("", False), ("false", False)]
)
def test_in_ci(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("CI", value)
    assert in_ci() is expected


def test_rank_options_keep_reasoning_low() -> None:
    assert RANK_OPTIONS == {"reasoning_effort": "low"}
