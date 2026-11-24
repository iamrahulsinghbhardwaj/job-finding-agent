"""Fetch one company's jobs live.

    python -m jobagent.jobs.fetch --company stripe --limit 10
    python -m jobagent.jobs.fetch --company amazon --country IN --keywords "Software Development Engineer"

--keywords/--country are used by sources that search server-side (Amazon); board sources
(Greenhouse, Lever, Ashby) return the whole board and ignore them.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from jobagent.adapters import UnsupportedPlatformError, build_source
from jobagent.adapters.common import ScopeError
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, NormalizedJob, RawJob, SearchSpec, SourceStatus
from jobagent.core.registry import RegistryError, load_registry
from jobagent.pipeline.normalize import NormalizeStats, normalize_all, source_key


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def report_blocked(company: Company) -> None:
    """Blocked sources are never fetched; say so instead of silently returning nothing."""
    for source in company.sources:
        if source.status is SourceStatus.BLOCKED:
            name = f"{source.platform.value} ({company.slug})"
            print(f"Skipping blocked source {name}: {source.blocked_reason}")


async def fetch_jobs(
    company: Company, spec: SearchSpec, limit: int, http: PoliteClient
) -> tuple[list[NormalizedJob], NormalizeStats]:
    """Fetch up to `limit` jobs across the company's active sources, normalized per source."""
    jobs: list[NormalizedJob] = []
    stats = NormalizeStats()
    for source_config in company.active_sources:
        source = build_source(company, source_config, http)
        raws: list[RawJob] = []
        async for job in source.search(spec, stop_at_job_id=None):
            raws.append(job)
            if len(jobs) + len(raws) >= limit:
                break
        normalized, source_stats = normalize_all(raws, source=source_key(source_config))
        jobs += normalized
        stats += source_stats
        if len(jobs) >= limit:
            break
    return jobs, stats


async def _run(company: Company, spec: SearchSpec, limit: int) -> tuple[list[NormalizedJob], NormalizeStats]:
    async with PoliteClient() as http:
        return await fetch_jobs(company, spec, limit, http)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.fetch", description="Fetch one company's jobs.")
    parser.add_argument("--company", required=True, help="company slug from the registry")
    parser.add_argument("--limit", type=_positive_int, default=10, help="max jobs to print")
    parser.add_argument("--path", type=Path, default=Path("companies.yaml"), help="registry file")
    parser.add_argument("--keywords", action="append", default=[], help="search keywords (repeatable)")
    parser.add_argument("--country", action="append", default=[], help="ISO-2 country code (repeatable)")
    args = parser.parse_args(argv)
    try:
        spec = SearchSpec(keywords=args.keywords, countries=args.country)
    except ValueError as exc:
        print(f"Bad search options: {exc}", file=sys.stderr)
        return 1

    try:
        companies = {c.slug: c for c in load_registry(args.path)}
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        return 1
    company = companies.get(args.company)
    if company is None:
        print(f"Unknown company {args.company!r}. Known: {', '.join(sorted(companies))}", file=sys.stderr)
        return 1

    report_blocked(company)
    try:
        jobs, stats = asyncio.run(_run(company, spec, args.limit))
    except ScopeError as exc:
        print(f"Search scope: {exc}", file=sys.stderr)
        return 1
    except UnsupportedPlatformError as exc:
        print(f"Not supported yet: {exc}", file=sys.stderr)
        return 1
    except BlockedError as exc:
        print(f"Blocked, stopping: {exc}", file=sys.stderr)
        return 1
    except FetchError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1

    for job in jobs:  # the original title, never title_canonical
        print(f"{job.title} | {job.location_raw or '-'} | {job.url}")
    print(f"\n{stats.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
