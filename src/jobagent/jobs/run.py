"""Run entry point.

    python -m jobagent.jobs.run --check-settings    # settings check only (names, never values)
    python -m jobagent.jobs.run --dry-run           # full run; write out/digest.html, send nothing
    python -m jobagent.jobs.run                     # full run (sending arrives with the Gmail brick)
    python -m jobagent.jobs.run --company amazon    # crawl a subset (repeatable)

Crawl (fetch, normalize, store, count new jobs), the rule filter over every job fetched this run
(counts per company, top rejection reasons), then the LLM ranker over filter survivors that have
no LLM ranking for the current effective profile. Ranking needs OPENAI_API_KEY, RANK_MODEL,
PROFILE_MODEL and CV_BLOB_SAS_URL; without them it is skipped. Locally it prints the top jobs;
under CI (Actions logs are public) only counts. Then the digest (digest/select, digest/render):
written to out/digest.html. Nothing is sent or recorded as emailed yet: the Gmail brick will send,
then record Run.emailed_job_ids. No new jobs -> no digest file.
"""

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from jobagent.core.http import FetchError, PoliteClient
from jobagent.core.llm import LlmError, LlmLedger
from jobagent.core.models import Company, NormalizedJob, SearchSpec, Tier
from jobagent.core.registry import RegistryError, load_registry, load_search_specs
from jobagent.core.settings import Settings
from jobagent.core.tokens import TokenizerError, ensure_encoding_file, load_tokenizer
from jobagent.db.session import make_engine
from jobagent.digest.render import write_digest
from jobagent.digest.select import Digest, select_digest
from jobagent.pipeline.crawl import CrawlResult, SourceOutcome, crawl
from jobagent.pipeline.filter import FilterReport, filter_jobs
from jobagent.pipeline.normalize import NormalizeStats
from jobagent.pipeline.profile import CvError, OverridesError, ProfileError, ProfileSync, ensure_profile
from jobagent.pipeline.rank import DEFAULT_TOKEN_BUDGET, RankRun, rank_survivors

COLUMNS = ("company", "sources", "fetched", "new", "errors", "unknown_loc")
FILTER_COLUMNS = ("company", "jobs", "passed", "passed_new")
TOP_REASONS = 10
TOP_JOBS = 10
RANK_SETTINGS = ("openai_api_key", "rank_model", "profile_model", "cv_blob_sas_url")
RANK_ERRORS = (CvError, OverridesError, ProfileError, FetchError, LlmError, TokenizerError)
RANK_OPTIONS = {"reasoning_effort": "low"}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jobagent.jobs.run", description="Run the job agent.")
    parser.add_argument(
        "--check-settings",
        action="store_true",
        help="Load settings, report which are present (names only) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Full run, but write the digest to out/digest.html instead of sending it.",
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


def filter_run(
    result: CrawlResult, companies: Sequence[Company]
) -> tuple[list[NormalizedJob], FilterReport, Counter[str]]:
    """The rule filter over every job stored this run: survivors, the report, and survivors first
    seen this run per company."""
    by_slug = {company.slug: company for company in companies}
    kept, report = filter_jobs((crawled.job for crawled in result.jobs), by_slug)
    new = {(c.job.source, c.job.source_job_id) for c in result.jobs if c.is_new}
    passed_new = Counter(job.company_slug for job in kept if (job.source, job.source_job_id) in new)
    return kept, report, passed_new


def run_filter(result: CrawlResult, companies: Sequence[Company]) -> tuple[FilterReport, Counter[str]]:
    _, report, passed_new = filter_run(result, companies)
    return report, passed_new


def format_rank(run: RankRun, ledger: LlmLedger) -> str:
    """Counts only: safe for public logs."""
    r = run.report
    fallback = ", ".join(f"{reason} {n}" for reason, n in sorted(r.fallback.items())) or "none"
    cost = f"${ledger.cost_usd:.4f}" + (f" (+{ledger.unpriced} unpriced calls)" if ledger.unpriced else "")
    return "\n".join(
        [
            "",
            "ranking",
            f"  to rank: {r.candidates:,} survivors without an LLM ranking for this profile "
            f"({r.skipped_stale:,} stale skipped, {r.refiltered_out:,} dropped by full-posting years)",
            f"  ranked by LLM: {r.llm:,}; rule score: {sum(r.fallback.values()):,} ({fallback})",
            f"  descriptions: {r.truncated:,} truncated, {r.no_description:,} missing, "
            f"{r.details_fetched:,} fetched, {r.descriptions_saved:,} saved; "
            f"fit corrected: {r.fit_mismatch:,}",
            f"  tokens: {ledger.input_tokens:,} in ({ledger.cached_ratio:.0%} cached) / "
            f"{ledger.output_tokens:,} out; budget {r.budget_used:,} of {r.budget_limit:,}; cost {cost}",
        ]
    )


def format_top(run: RankRun, n: int = TOP_JOBS) -> str:
    """The best-scored jobs with fit and reason: job data, so local terminals only (never CI)."""
    best = sorted(run.ranked, key=lambda r: (-r.score, r.company.slug, r.job.title))[:n]
    if not best:
        return ""
    lines = ["", f"top {len(best)} by score"]
    for r in best:
        model = " [rule]" if r.fallback else ""
        lines.append(f"  {r.score:>3} {r.fit:<6} {r.company.name} | {r.job.title}{model}")
        lines.append(f"             {r.reason}")
        lines.append(f"             {r.job.url}")
    return "\n".join(lines)


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


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


@dataclass(frozen=True)
class RunOutput:
    crawl: CrawlResult
    survivors: list[NormalizedJob]
    report: FilterReport
    passed_new: Counter[str]
    rank: RankRun | None = None
    ledger: LlmLedger | None = None
    rank_note: str | None = None  # why ranking was skipped or failed (safe to print)
    profile: ProfileSync | None = None


async def _run(
    session: Session, companies: list[Company], specs: dict[Tier, SearchSpec], settings: Settings
) -> RunOutput:
    async with PoliteClient() as http:  # open for the whole run: ranking may call get_details
        result = await crawl(session, companies, specs, http=http)
        survivors, report, passed_new = filter_run(result, companies)
        output = RunOutput(result, survivors, report, passed_new)
        missing = [name.upper() for name in RANK_SETTINGS if getattr(settings, name) is None]
        if missing:
            return _with(output, rank_note=f"ranking skipped: missing {', '.join(missing)}")
        ledger = LlmLedger()
        client = AsyncOpenAI(api_key=settings.require("openai_api_key"))
        try:
            await ensure_encoding_file(http)
            overrides = settings.profile_overrides_sas_url
            profile = await ensure_profile(
                session,
                http=http,
                client=client,
                cv_url=settings.require("cv_blob_sas_url"),
                model=settings.require("profile_model"),
                overrides_url=overrides.get_secret_value() if overrides else None,
                ledger=ledger,
            )
            session.commit()
            ranked = await rank_survivors(
                session,
                survivors,
                {c.slug: c for c in companies},
                sources=result.sources,
                client=client,
                profile=profile,
                model=settings.require("rank_model"),
                tokenizer=load_tokenizer(),
                token_budget=settings.rank_token_budget or DEFAULT_TOKEN_BUDGET,
                ledger=ledger,
                run_id=result.run_id,
                options=RANK_OPTIONS,
            )
            session.commit()
        except RANK_ERRORS as exc:
            session.rollback()
            return _with(output, ledger=ledger, rank_note=f"ranking failed: {exc}")
        finally:
            await client.close()
        return _with(output, rank=ranked, ledger=ledger, profile=profile)


def _with(output: RunOutput, **changes: Any) -> RunOutput:
    return replace(output, **changes)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = Settings()
    if args.check_settings:
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
    digest_line: str | None = None
    try:
        with Session(engine) as session:
            output = asyncio.run(_run(session, companies, specs, settings))
            if output.profile is not None:
                digest_line = _write_digest(session, output.profile, dry_run=args.dry_run)
    finally:
        engine.dispose()
    print(format_summary(output.crawl))
    print(format_filter(output.report, output.passed_new))
    if output.rank is not None and output.ledger is not None:
        print(format_rank(output.rank, output.ledger))
        if not in_ci():
            print(format_top(output.rank))
    if digest_line:
        print(f"\n{digest_line}")
    if output.rank_note:
        print(f"\n{output.rank_note}", file=sys.stderr)
        return 1 if output.rank_note.startswith("ranking failed") else 0
    return 0


def format_digest_counts(digest: Digest) -> str:
    """Counts only: safe for public logs."""
    parts = ", ".join(f"{s.name}: {len(s.full)} full + {len(s.compact)} compact" for s in digest.sections)
    return f"digest: {digest.count} lines ({len(digest.job_ids)} jobs); {parts}"


def _write_digest(session: Session, profile: ProfileSync, *, dry_run: bool) -> str:
    digest = select_digest(session, profile.effective_hash, datetime.now(UTC).date())
    if digest is None:
        return "digest: no new jobs, no digest"
    path = write_digest(digest)
    # Nothing is sent yet, so nothing is recorded as emailed (the Gmail brick sends, then records).
    mode = "dry run, not sent" if dry_run else "not sent: sending arrives with the Gmail brick"
    return f"{format_digest_counts(digest)} -> {path} ({mode})"


if __name__ == "__main__":
    sys.exit(main())
