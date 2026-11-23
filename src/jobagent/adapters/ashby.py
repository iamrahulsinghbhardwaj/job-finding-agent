"""Ashby job boards: https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true

The board endpoint returns every listed job in one response, each with its full description,
so there is no separate details endpoint: get_details() reuses the last board response.

Ordering uses `publishedAt` (the response is not sorted). Ids are UUID strings, so ties break
on the id as text. Jobs with isListed=false are hidden from the public board and skipped.
Unknown boards return 200 with no jobs rather than 404; health_check reports that as not ok.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from jobagent.adapters.common import html_to_text, schema_hash
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, HealthReport, JobDetail, RawJob, SearchSpec, SourceConfig

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"
PARAMS = {"includeCompensation": "true"}
_OLDEST = datetime.min.replace(tzinfo=UTC)


class AshbySource:
    def __init__(self, company: Company, source: SourceConfig, http: PoliteClient) -> None:
        self._company = company
        self._tenant = source.tenant
        self._http = http
        self._board: dict[str, dict[str, Any]] = {}  # job id -> listing, from the last fetch
        self.skipped = 0  # listings that failed validation in the last search (count only)
        self.unlisted = 0  # listings hidden from the public board in the last search

    @property
    def _board_url(self) -> str:
        return f"{BASE_URL}/{self._tenant}"

    async def search(self, spec: SearchSpec, stop_at_job_id: str | None) -> AsyncIterator[RawJob]:
        for job in await self._fetch_jobs():
            if job.source_job_id == stop_at_job_id:
                return
            yield job

    async def get_details(self, job_id: str) -> JobDetail:
        if job_id not in self._board:
            await self._fetch_jobs()
        item = self._board.get(job_id)
        if item is None:
            raise FetchError(f"job not on board: {self._tenant}", host="api.ashbyhq.com")
        text = (item.get("descriptionPlain") or "").replace("\xa0", " ").strip()
        if not text:
            text = html_to_text(item.get("descriptionHtml") or "")
        return JobDetail(source_job_id=job_id, description_text=text)

    async def health_check(self) -> HealthReport:
        try:
            jobs = (await self._http.get_json(self._board_url, params=PARAMS)).get("jobs") or []
        except BlockedError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=f"blocked: {exc}")
        except FetchError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=str(exc))
        return HealthReport(
            ok=bool(jobs), job_count=len(jobs), schema_hash=schema_hash(jobs[0]) if jobs else ""
        )

    async def _fetch_jobs(self) -> list[RawJob]:
        data = await self._http.get_json(self._board_url, params=PARAMS)
        jobs: list[RawJob] = []
        self._board = {}
        self.skipped = 0
        self.unlisted = 0
        for item in data.get("jobs") or []:
            if item.get("isListed") is False:
                self.unlisted += 1
                continue
            try:
                job = self._to_raw_job(item)
            except (ValidationError, KeyError, TypeError):
                self.skipped += 1
                continue
            jobs.append(job)
            self._board[job.source_job_id] = item
        jobs.sort(key=lambda j: (j.posted_at or _OLDEST, j.source_job_id), reverse=True)
        return jobs

    def _to_raw_job(self, item: dict[str, Any]) -> RawJob:
        return RawJob.model_validate(
            {
                "source_job_id": str(item["id"]),
                "company_slug": self._company.slug,
                "title": item["title"],
                "location_raw": item.get("location") or "",
                "url": item["jobUrl"],
                "posted_at": item.get("publishedAt"),
                "raw": item,
            }
        )
