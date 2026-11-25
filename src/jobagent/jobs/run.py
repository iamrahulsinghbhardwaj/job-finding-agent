"""Run entry point.

    python -m jobagent.jobs.run --dry-run           # settings check only (names, never values)
    python -m jobagent.jobs.run                     # crawl every company in companies.yaml
    python -m jobagent.jobs.run --company amazon    # crawl a subset (repeatable)

Crawl (fetch, normalize, store, count new jobs), then the rule filter over every job fetched this
run: counts per company and the top rejection reasons. No ranking or email yet.
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, SearchSpec, Tier
from jobagent.core.registry import RegistryError, load_registry, load_search_specs
from jobagent.core.settings import Settings
from jobagent.db.session import make_engine
from jobagent.pipeline.crawl import CrawlResult, SourceOutcome, crawl
from jobagent.pipeline.filter import FilterReport, filter_jobs
from jobagent.pipeline.normalize import NormalizeStats

COLUMNS = ("company", "sources", "fetched", "new", "errors", "unknown_loc")
FILTER_COLUMNS = ("company", "jobs", "passed", "passed_new")
TOP_REASONS = 10


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.run", description="Run the job agent.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load settings, report which are present (names only) and exit.",
    )
    parser.add_argument("--path", type=Path, default=Path("companies.yaml"), help="registry file")
    parser.add_argument(
        "--company", action="append", default=[], help="only crawl this company slug (repeatable)"
    )
    return parser.parse_args(argv)


def _report_settings(settings: Settings) -> None:
    present = settings.present_settings()
    for name, is_set in present.items():
        print(f"{name}: {'present' if is_set else 'missing'}")
    print(f"{sum(present.values())}/{len(present)} settings present")


@dataclass
class CompanyRow:
    company: str
    sources: int = 0
    blocked: int = 0
    fetched: int = 0
    new: int = 0
    errors: int = 0
    unknown_location: int = 0

    def cells(self) -> tuple[str, ...]:
        sources = f"{self.sources}" + (f" (+{self.blocked} blocked)" if self.blocked else "")
        numbers = (self.fetched, self.new, self.errors, self.unknown_location)
        return (self.company, sources, *(f"{n:,}" for n in numbers))


def company_rows(outcomes: Sequence[SourceOutcome]) -> list[CompanyRow]:
    """One row per company, in registry order (outcomes are grouped by company slug)."""
    rows: dict[str, CompanyRow] = {}
    for outcome in outcomes:
        row = rows.setdefault(outcome.company_slug, CompanyRow(outcome.company_slug))
        if outcome.blocked:
            row.blocked += 1
            continue
        row.sources += 1
        row.fetched += outcome.fetched
        row.new += outcome.new
        row.errors += outcome.errors
        row.unknown_location += outcome.stats.unknown_location
    return list(rows.values())


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    table = [tuple(header), *(tuple(row) for row in rows)]
    widths = [max(len(cell) for cell in column) for column in zip(*table, strict=True)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip() for line in table
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return lines


def format_summary(result: CrawlResult) -> str:
    lines = _table(COLUMNS, [row.cells() for row in company_rows(result.outcomes)])

    fetched = [o for o in result.outcomes if not o.blocked]
    stats = sum((o.stats for o in fetched), NormalizeStats())
    blocked = len(result.outcomes) - len(fetched)
    lines += [
        "",
        f"run {result.run_id}: {len(fetched)} sources fetched, {blocked} blocked, "
        f"{sum(o.fetched for o in fetched):,} jobs, {len(result.new_job_ids):,} new, "
        f"{sum(o.errors for o in fetched)} errors, "
        f"{stats.unknown_share:.1%} unknown location",
    ]
    return "\n".join(lines)


def run_filter(result: CrawlResult, companies: Sequence[Company]) -> tuple[FilterReport, Counter[str]]:
    """The rule filter over every job stored this run; also counts survivors first seen this run."""
    by_slug = {company.slug: company for company in companies}
    kept, report = filter_jobs((crawled.job for crawled in result.jobs), by_slug)
    new = {(c.job.source, c.job.source_job_id) for c in result.jobs if c.is_new}
    passed_new = Counter(job.company_slug for job in kept if (job.source, job.source_job_id) in new)
    return report, passed_new


def format_filter(report: FilterReport, passed_new: Counter[str]) -> str:
    """Counts only: safe for public logs."""
    rows = [
        (slug, f"{n:,}", f"{report.after[slug]:,}", f"{passed_new[slug]:,}")
        for slug, n in report.before.items()
    ]
    lines = ["", "rule filter", *_table(FILTER_COLUMNS, rows)]
    lines += [
        "",
        f"filter: {report.total_before:,} -> {report.total_after:,} passed "
        f"({sum(passed_new.values()):,} new, {report.remote_unspecified:,} labelled Remote (unspecified))",
    ]
    if report.reasons:
        lines += ["", f"top rejection reasons (of {sum(report.reasons.values()):,} rejected):"]
        width = max(len(reason) for reason, _ in report.reasons.most_common(TOP_REASONS))
        lines += [f"  {reason.ljust(width)}  {n:,}" for reason, n in report.reasons.most_common(TOP_REASONS)]
    return "\n".join(lines)


async def _crawl(session: Session, companies: list[Company], specs: dict[Tier, SearchSpec]) -> CrawlResult:
    async with PoliteClient() as http:
        return await crawl(session, companies, specs, http=http)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = Settings()
    if args.dry_run:
        _report_settings(settings)
        return 0

    if settings.database_url is None:
        print("DATABASE_URL is not set: put it in .env (see .env.example).", file=sys.stderr)
        return 2
    try:
        companies = load_registry(args.path)
        specs = load_search_specs(args.path)
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        return 1
    if args.company:
        unknown = sorted(set(args.company) - {c.slug for c in companies})
        if unknown:
            print(f"Unknown company: {', '.join(unknown)}", file=sys.stderr)
            return 1
        companies = [c for c in companies if c.slug in args.company]

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request floods public logs
    engine = make_engine(settings.require("database_url"))
    try:
        with Session(engine) as session:
            result = asyncio.run(_crawl(session, companies, specs))
    finally:
        engine.dispose()
    print(format_summary(result))
    print(format_filter(*run_filter(result, companies)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
