"""Amazon (in-house careers site): https://www.amazon.jobs/en/search.json

A custom adapter: only Amazon uses this endpoint. It is undocumented, so expect it to change.

- One query per (keyword, country, category): each is scoped (country required, category from
  SourceConfig.extra) because one search returns at most 10,000 results.
- Pages of 100 via `offset`/`result_limit`. A query stops at a short page, at spec.max_pages
  (hard cap), or, once the query is backfilled (see QueryLedger), at the first page whose jobs
  are ALL already known (`known_job_ids`). So the first full pass pages up to the end or the
  cap, and later runs cost about one request per query. A pass cut short (timeout, error) is
  not a full pass: the next run pages fully again, so no older page is skipped forever.
- `sort=recent` is NOT strictly newest first (it roughly follows recent activity), which is why
  "a whole page is known" is the stop rule rather than "the newest stored id was seen". Results
  are sorted ourselves by posted_date (day precision), ties broken by the numeric public job id.
- Never a full listing (paged and capped), so a search never proves a job has gone.
- Search results already carry the description and qualifications, so get_details() reuses
  them instead of scraping job pages.
"""

from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from typing import Any

from pydantic import ValidationError

from jobagent.adapters.common import ScopeError, extract_min_yoe, html_to_text, schema_hash, to_iso3
from jobagent.core.contracts import QueryLedger
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, HealthReport, JobDetail, RawJob, SearchSpec, SourceConfig

SITE = "https://www.amazon.jobs"
SEARCH_URL = f"{SITE}/en/search.json"
HOST = "www.amazon.jobs"
PAGE_SIZE = 100
RESULT_CAP = 10_000
_OLDEST = datetime.min.replace(tzinfo=UTC)

Params = list[tuple[str, str | int]]


@dataclass(frozen=True)
class _Query:
    keyword: str | None
    country: str  # ISO-3
    category: str

    @property
    def key(self) -> str:
        """Stable name in the ledger, e.g. "|IND|software-development"."""
        return f"{self.keyword or ''}|{self.country}|{self.category}"


class AmazonSource:
    full_listing = False  # paged and capped: missing jobs may just be on a page we did not fetch

    def __init__(self, company: Company, source: SourceConfig, http: PoliteClient) -> None:
        self._company = company
        self._categories = _categories(company, source)
        self._http = http
        self._cache: dict[str, dict[str, Any]] = {}  # job id -> listing, from the last search
        self.skipped = 0  # listings that failed validation in the last search (count only)
        self.capped = False  # a query matched more than Amazon's 10,000-result cap

    async def search(
        self, spec: SearchSpec, known_job_ids: AbstractSet[str], ledger: QueryLedger | None = None
    ) -> AsyncIterator[RawJob]:
        if not spec.countries:
            raise ScopeError(f"{self._company.slug}: Amazon searches need at least one country")
        countries = [to_iso3(c) for c in spec.countries]
        self._cache = {}
        self.skipped = 0
        self.capped = False
        ledger = ledger if ledger is not None else QueryLedger()
        jobs: dict[str, RawJob] = {}
        for keyword, country, category in product(spec.keywords or [None], countries, self._categories):
            query = _Query(keyword=keyword, country=country, category=category)
            for job in await self._run_query(query, spec.max_pages, known_job_ids, ledger):
                jobs.setdefault(job.source_job_id, job)
        for job in sorted(jobs.values(), key=_sort_key, reverse=True):
            yield job

    async def get_details(self, job_id: str) -> JobDetail:
        item = self._cache.get(job_id)
        if item is None:
            raise FetchError(f"job {job_id} not in the last search; search first", host=HOST)
        description = _text(item.get("description"))
        basic = _text(item.get("basic_qualifications"))
        preferred = _text(item.get("preferred_qualifications"))
        parts = [
            description,
            f"Basic qualifications\n{basic}" if basic else "",
            f"Preferred qualifications\n{preferred}" if preferred else "",
        ]
        # Minimum experience comes from the REQUIRED (basic) qualifications, else the description.
        # Never from preferred qualifications: "8+ years preferred" is not a minimum.
        min_yoe = extract_min_yoe(basic)
        return JobDetail(
            source_job_id=job_id,
            description_text="\n".join(part for part in parts if part),
            min_yoe=min_yoe if min_yoe is not None else extract_min_yoe(description),
        )

    async def health_check(self) -> HealthReport:
        params: Params = [*self._category_params(), ("sort", "recent"), ("offset", 0), ("result_limit", 1)]
        try:
            data = await self._http.get_json(SEARCH_URL, params=params)
        except BlockedError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=f"blocked: {exc}")
        except FetchError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=str(exc))
        jobs = data.get("jobs") or []
        hits = data.get("hits") if isinstance(data.get("hits"), int) else len(jobs)
        return HealthReport(ok=hits > 0, job_count=hits, schema_hash=schema_hash(jobs[0]) if jobs else "")

    async def _run_query(
        self, query: _Query, max_pages: int, known: AbstractSet[str], ledger: QueryLedger
    ) -> list[RawJob]:
        key = query.key
        ledger.started.add(key)
        can_stop_early = ledger.can_stop_early(key)
        jobs: list[RawJob] = []
        for page in range(max_pages):
            params: Params = [
                ("category[]", query.category),
                ("normalized_country_code[]", query.country),
                ("sort", "recent"),
                ("offset", page * PAGE_SIZE),
                ("result_limit", PAGE_SIZE),
            ]
            if query.keyword:
                params.append(("base_query", query.keyword))
            data = await self._http.get_json(SEARCH_URL, params=params)
            if data.get("error"):
                raise FetchError(f"search error from {HOST}", host=HOST)
            if isinstance(data.get("hits"), int) and data["hits"] > RESULT_CAP:
                self.capped = True
            items = data.get("jobs") or []
            page_jobs = self._parse(items)
            jobs.extend(page_jobs)
            if len(items) < PAGE_SIZE:
                ledger.full_pass.add(key)  # reached the last page
                break
            if can_stop_early and page_jobs and all(job.source_job_id in known for job in page_jobs):
                break  # caught up: everything on this page is already stored
        else:
            ledger.full_pass.add(key)  # stopped by the max_pages hard cap
            ledger.capped.add(key)
        ledger.finished.add(key)
        return jobs

    def _parse(self, items: list[dict[str, Any]]) -> list[RawJob]:
        jobs: list[RawJob] = []
        for item in items:
            try:
                job = self._to_raw_job(item)
            except (ValidationError, KeyError, TypeError, ValueError):
                self.skipped += 1
                continue
            jobs.append(job)
            self._cache[job.source_job_id] = item
        return jobs

    def _to_raw_job(self, item: dict[str, Any]) -> RawJob:
        return RawJob.model_validate(
            {
                "source_job_id": str(item["id_icims"]),
                "company_slug": self._company.slug,
                "title": item["title"],
                "location_raw": item.get("normalized_location") or item.get("location") or "",
                "url": SITE + item["job_path"],
                "posted_at": _parse_posted_date(item.get("posted_date")),
                "raw": item,
            }
        )

    def _category_params(self) -> Params:
        return [("category[]", c) for c in self._categories]


def _categories(company: Company, source: SourceConfig) -> list[str]:
    categories: Any = source.extra.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ScopeError(f"{company.slug}: extra.categories must be a non-empty list of strings")
    if not all(isinstance(c, str) and c for c in categories):
        raise ScopeError(f"{company.slug}: extra.categories must be a non-empty list of strings")
    return [str(c) for c in categories]


def _text(html: Any) -> str:
    """HTML field to text, dropping the literal "null" Amazon appends to some fields."""
    lines = html_to_text(html or "").splitlines()
    return "\n".join(line for line in lines if line.strip().lower() != "null")


def _parse_posted_date(value: Any) -> datetime | None:
    """Amazon gives a day only, e.g. "November 23, 2026"; treat it as midnight UTC."""
    if not value:
        return None
    return datetime.strptime(value, "%B %d, %Y").replace(tzinfo=UTC)


def _sort_key(job: RawJob) -> tuple[datetime, int]:
    job_number = int(job.source_job_id) if job.source_job_id.isdigit() else -1
    return (job.posted_at or _OLDEST, job_number)
