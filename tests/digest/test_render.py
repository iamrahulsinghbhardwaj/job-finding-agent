"""The rendered digest: a snapshot of fixed data, escaping, and the email-safe rules.

Regenerate the snapshot after an intended template change:
    UPDATE_SNAPSHOTS=1 uv run pytest tests/digest/test_render.py
"""

import os
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from jobagent.digest.render import days_text, render_digest, write_digest
from jobagent.digest.select import Digest, DigestRow, build_digest
from jobagent.pipeline.rank import RULE_MODEL

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "digest_snapshot.html"
TODAY = date(2026, 11, 27)


def row(job_id: int, score: int, **changes: object) -> DigestRow:
    base = DigestRow(
        job_id=job_id,
        score=score,
        fit="High" if score >= 75 else "Medium" if score >= 50 else "Low",
        reason=f"Same backend stack and level; reason {job_id}.",
        model="gpt-5.4-mini",
        title=f"Backend Engineer {job_id}",
        company="Examplecorp",
        company_slug="examplecorp",
        us_transfer_path=True,
        country="IN",
        city="Bengaluru",
        location_raw="Bengaluru, India",
        min_yoe=3,
        max_yoe=None,
        posted_at=datetime(2026, 11, 27 - job_id % 7, tzinfo=UTC),
        url=f"https://jobs.example.com/{job_id}",
        description_hash=f"h{job_id}",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def fixed_digest() -> Digest:
    rows = [
        row(1, 91, title="Software Engineer II", max_yoe=6, min_yoe=2),
        row(2, 84, posted_at=None),
        row(3, 84, description_hash="dup", title="Platform Engineer"),
        row(4, 80, description_hash="dup", title="Platform Engineer"),  # identical posting of 3
        *(row(10 + i, 73 - i) for i in range(12)),  # 12 Medium: 10 full, 2 compact
        row(30, 60, model=RULE_MODEL, reason="Rule score (no LLM): years fit, 2 of your main stack."),
        row(40, 88, company="Beta Pay", country="IE", city="Dublin", us_transfer_path=True),
        row(41, 66, company="Gamma", country="GB", city="London", us_transfer_path=False, min_yoe=None),
    ]
    digest = build_digest(rows, TODAY)
    assert digest is not None
    return digest


def test_snapshot() -> None:
    html = render_digest(fixed_digest())
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.write_text(html, encoding="utf-8")
    assert html == SNAPSHOT.read_text(encoding="utf-8")


def test_fixed_digest_shape() -> None:
    digest = fixed_digest()
    assert digest.subject == "18 new: 16 India, 2 Europe"
    india, europe = digest.sections
    assert (len(india.full), len(india.compact)) == (13, 3)  # 3 High + top 10 Medium; 3 compact
    assert len(europe.full) == 2
    assert len(digest.job_ids) == 19  # the merged duplicate's id counts too


def test_every_line_has_its_parts() -> None:
    html = render_digest(fixed_digest())
    assert ">91<" in html  # score badge
    assert "Examplecorp &middot; Software Engineer II" in html
    assert "Bengaluru, IN &middot; 2\u20136 yrs &middot; 1 day ago" in html
    assert "Same backend stack and level; reason 1." in html
    assert 'href="https://jobs.example.com/1"' in html
    assert "posted date n/a" in html
    assert "+1 identical posting" in html
    assert "YOE n/a" in html


def test_rule_fallback_rows_say_rule_score() -> None:
    html = render_digest(fixed_digest())
    assert html.count("rule score") == 1


def test_compact_rows_have_no_reason() -> None:
    html = render_digest(fixed_digest())
    compact = html.split("More Medium matches")[1].split("Tier 1 — Europe")[0]
    assert "reason" not in compact
    assert compact.count("Apply</a>") == 3
    assert "Rule score (no LLM)" not in compact


def test_star_and_legend_in_europe_only() -> None:
    html = render_digest(fixed_digest())
    india, europe = html.split("Tier 1 — Europe")
    assert "&#9733;" not in india
    assert "US transfer path" in europe
    assert europe.count("&#9733;") == 2  # the legend and Beta Pay's line, not Gamma's


def test_untrusted_text_is_escaped() -> None:
    attack = row(1, 90, title="<script>alert(1)</script>", reason='"><img src=x onerror=alert(1)>')
    digest = build_digest([attack], TODAY)
    assert digest is not None
    html = render_digest(digest)
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_email_safe_html() -> None:
    html = render_digest(fixed_digest())
    assert "<style" not in html  # inline CSS only
    assert "<link" not in html  # no external CSS
    assert "<img" not in html  # no images
    assert "<script" not in html
    assert "max-width:600px" in html
    assert re.search(r'<meta name="viewport"', html)


def test_days_text() -> None:
    assert [days_text(d) for d in (None, 0, 1, 9)] == ["posted date n/a", "today", "1 day ago", "9 days ago"]


def test_write_digest_creates_the_file(tmp_path: Path) -> None:
    path = write_digest(fixed_digest(), tmp_path / "out" / "digest.html")
    assert path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_plain_text_part_mirrors_the_lines() -> None:
    from jobagent.digest.render import render_text

    text = render_text(fixed_digest())
    assert text.startswith("18 new: 16 India, 2 Europe\n")
    assert "91 High  Examplecorp - Software Engineer II" in text
    assert "    https://jobs.example.com/1" in text
    assert "[rule score]" in text
    assert "More Medium matches (3):" in text
    assert "* Beta Pay - Backend Engineer 40" in text  # star in Europe
    assert "<" not in text  # no HTML


def test_a_big_backlog_digest_stays_under_gmails_clip_limit() -> None:
    # Gmail clips HTML over ~102 KB. 171 lines like the first real backlog: 70 full, 101 compact.
    title = "Software Development Engineer II, Team"
    rows = [row(i, 80 + i % 15, title=f"{title} {i} Platform") for i in range(60)]
    rows += [row(100 + i, 64 - i % 14, title=f"{title} {i}") for i in range(101)]
    rows += [row(300 + i, 70, country="IE", city="Dublin") for i in range(10)]
    digest = build_digest(rows, TODAY)
    assert digest is not None
    assert digest.count == 171
    assert len(render_digest(digest).encode()) < 100 * 1024
