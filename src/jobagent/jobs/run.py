"""Run entry point.

    python -m jobagent.jobs.run --dry-run           # settings check only (names, never values)
    python -m jobagent.jobs.run                     # crawl every company in companies.yaml
    python -m jobagent.jobs.run --company amazon    # crawl a subset (repeatable)

Crawl only for now: fetch, normalize, store, count new jobs. No ranking or email yet.
"""

import argparse
import asyncio
import logging
import sys
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
from jobagent.pipeline.normalize import NormalizeStats

COLUMNS = ("company", "sources", "fetched", "new", "errors", "unknown_loc")


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


def format_summary(result: CrawlResult) -> str:
    rows = company_rows(result.outcomes)
    table = [COLUMNS, *(row.cells() for row in rows)]
    widths = [max(len(cell) for cell in column) for column in zip(*table, strict=True)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip() for line in table
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
