"""Greenhouse job boards: https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs

The list endpoint returns the whole board in one response (no paging, no server-side filters),
so keyword/country filtering happens later in the pipeline.

Ordering uses `first_published` (fixed at posting time). `updated_at` changes on every edit,
so ordering by it would let an edited old job hide genuinely new ones from the stop-at rule.
"""

import html
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from jobagent.adapters.common import html_to_text, schema_hash
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, HealthReport, JobDetail, RawJob, SearchSpec, SourceConfig

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"
_OLDEST = datetime.min.replace(tzinfo=UTC)


class GreenhouseSource:
    def __init__(self, company: Company, source: SourceConfig, http: PoliteClient) -> None:
        self._company = company
        self._tenant = source.tenant
        self._http = http
        self.skipped = 0  # listings that failed validation in the last search (count only)

    @property
    def _jobs_url(self) -> str:
        return f"{BASE_URL}/{self._tenant}/jobs"

    async def search(self, spec: SearchSpec, stop_at_job_id: str | None) -> AsyncIterator[RawJob]:
        for job in await self._fetch_jobs():
            if job.source_job_id == stop_at_job_id:
                return
            yield job

    async def get_details(self, job_id: str) -> JobDetail:
        data = await self._http.get_json(f"{self._jobs_url}/{job_id}")
        content = data.get("content") or ""
        # Greenhouse HTML-escapes the HTML ("&lt;p&gt;"), so unescape before parsing.
        return JobDetail(source_job_id=str(data["id"]), description_text=html_to_text(html.unescape(content)))

    async def health_check(self) -> HealthReport:
        try:
            jobs = (await self._http.get_json(self._jobs_url)).get("jobs") or []
        except BlockedError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=f"blocked: {exc}")
        except FetchError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=str(exc))
        return HealthReport(
            ok=bool(jobs), job_count=len(jobs), schema_hash=schema_hash(jobs[0]) if jobs else ""
        )

    async def _fetch_jobs(self) -> list[RawJob]:
        data = await self._http.get_json(self._jobs_url)
        jobs: list[RawJob] = []
        self.skipped = 0
        for item in data.get("jobs") or []:
            try:
                jobs.append(self._to_raw_job(item))
            except (ValidationError, KeyError, TypeError):
                self.skipped += 1
        jobs.sort(key=lambda j: (j.posted_at or _OLDEST, int(j.source_job_id)), reverse=True)
        return jobs

    def _to_raw_job(self, item: dict[str, Any]) -> RawJob:
        location = item.get("location") or {}
        return RawJob.model_validate(
            {
                "source_job_id": str(item["id"]),
                "company_slug": self._company.slug,
                "title": item["title"],
                "location_raw": location.get("name") or "",
                "url": item["absolute_url"],
                "posted_at": item.get("first_published") or item.get("updated_at"),
                "raw": item,
            }
        )
