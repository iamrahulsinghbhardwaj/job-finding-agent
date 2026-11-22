"""Fetch one company's jobs live: `python -m jobagent.jobs.fetch --company stripe --limit 10`."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from jobagent.adapters import UnsupportedPlatformError, build_source
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, RawJob, SearchSpec
from jobagent.core.registry import RegistryError, load_registry


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


async def fetch_jobs(company: Company, limit: int, http: PoliteClient) -> list[RawJob]:
    spec = SearchSpec(keywords=[], countries=[])
    jobs: list[RawJob] = []
    for source_config in company.sources:
        source = build_source(company, source_config, http)
        async for job in source.search(spec, stop_at_job_id=None):
            jobs.append(job)
            if len(jobs) >= limit:
                return jobs
    return jobs


async def _run(company: Company, limit: int) -> list[RawJob]:
    async with PoliteClient() as http:
        return await fetch_jobs(company, limit, http)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.fetch", description="Fetch one company's jobs.")
    parser.add_argument("--company", required=True, help="company slug from the registry")
    parser.add_argument("--limit", type=_positive_int, default=10, help="max jobs to print")
    parser.add_argument("--path", type=Path, default=Path("companies.yaml"), help="registry file")
    args = parser.parse_args(argv)

    try:
        companies = {c.slug: c for c in load_registry(args.path)}
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        return 1
    company = companies.get(args.company)
    if company is None:
        print(f"Unknown company {args.company!r}. Known: {', '.join(sorted(companies))}", file=sys.stderr)
        return 1

    try:
        jobs = asyncio.run(_run(company, args.limit))
    except UnsupportedPlatformError as exc:
        print(f"Not supported yet: {exc}", file=sys.stderr)
        return 1
    except BlockedError as exc:
        print(f"Blocked, stopping: {exc}", file=sys.stderr)
        return 1
    except FetchError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1

    for job in jobs:
        print(f"{job.title} | {job.location_raw or '-'} | {job.url}")
    print(f"\n{len(jobs)} jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
