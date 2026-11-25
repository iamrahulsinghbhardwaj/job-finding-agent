"""Lever job boards: https://api.lever.co/v0/postings/{tenant}?mode=json

The list endpoint returns the whole board in one response (no paging, no server-side filters),
so keyword/country filtering happens later in the pipeline.

Ordering uses `createdAt` (milliseconds since epoch, fixed at posting time), so the stop-at rule
is not fooled by edits. Lever ids are UUID strings, so ties break on the id as text.
The response is not sorted, so we sort it ourselves.
"""

from collections.abc import AsyncIterator
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from jobagent.adapters.common import extract_yoe, html_to_text, schema_hash, years_fields
from jobagent.core.contracts import QueryLedger
from jobagent.core.http import BlockedError, FetchError, PoliteClient
from jobagent.core.models import Company, HealthReport, JobDetail, RawJob, SearchSpec, SourceConfig

BASE_URL = "https://api.lever.co/v0/postings"
PARAMS = {"mode": "json"}
_OLDEST = datetime.min.replace(tzinfo=UTC)


class LeverSource:
    full_listing = True  # one request returns the entire board

    def __init__(self, company: Company, source: SourceConfig, http: PoliteClient) -> None:
        self._company = company
        self._tenant = source.tenant
        self._http = http
        self.skipped = 0  # listings that failed validation in the last search (count only)

    @property
    def _postings_url(self) -> str:
        return f"{BASE_URL}/{self._tenant}"

    async def search(
        self, spec: SearchSpec, known_job_ids: AbstractSet[str], ledger: QueryLedger | None = None
    ) -> AsyncIterator[RawJob]:
        # The whole board comes back in one request: known ids and the ledger save nothing.
        for job in await self._fetch_jobs():
            yield job

    async def get_details(self, job_id: str) -> JobDetail:
        data = await self._http.get_json(f"{self._postings_url}/{job_id}", params=PARAMS)
        text = _description_text(data)
        return JobDetail(
            source_job_id=str(data["id"]), description_text=text, **years_fields(extract_yoe(text))
        )

    async def health_check(self) -> HealthReport:
        try:
            jobs = await self._http.get_json(self._postings_url, params=PARAMS)
        except BlockedError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=f"blocked: {exc}")
        except FetchError as exc:
            return HealthReport(ok=False, job_count=0, schema_hash="", error=str(exc))
        if not isinstance(jobs, list):
            return HealthReport(ok=False, job_count=0, schema_hash="", error="unexpected response shape")
        return HealthReport(
            ok=bool(jobs), job_count=len(jobs), schema_hash=schema_hash(jobs[0]) if jobs else ""
        )

    async def _fetch_jobs(self) -> list[RawJob]:
        data = await self._http.get_json(self._postings_url, params=PARAMS)
        jobs: list[RawJob] = []
        self.skipped = 0
        for item in data if isinstance(data, list) else []:
            try:
                jobs.append(self._to_raw_job(item))
            except (ValidationError, KeyError, TypeError, ValueError):
                self.skipped += 1
        jobs.sort(key=lambda j: (j.posted_at or _OLDEST, j.source_job_id), reverse=True)
        return jobs

    def _to_raw_job(self, item: dict[str, Any]) -> RawJob:
        categories = item.get("categories") or {}
        return RawJob.model_validate(
            {
                "source_job_id": str(item["id"]),
                "company_slug": self._company.slug,
                "title": item["text"],
                "location_raw": categories.get("location") or "",
                "url": item["hostedUrl"],
                "posted_at": _from_epoch_ms(item.get("createdAt")),
                "raw": item,
                **_years(item),  # the list has full descriptions
            }
        )


def _from_epoch_ms(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:  # platform-dependent for absurd values
        raise ValueError(f"createdAt out of range: {value}") from exc


def _years(item: dict[str, Any]) -> dict[str, Any]:
    text = _description_text(item)
    return {"has_description": bool(text), **years_fields(extract_yoe(text))}


def _description_text(data: dict[str, Any]) -> str:
    """Intro, then each list section (heading + bullets), then the closing text."""
    parts: list[str] = [data.get("descriptionPlain") or ""]
    for section in data.get("lists") or []:
        heading = (section.get("text") or "").strip()
        bullets = html_to_text(section.get("content") or "")
        parts.append(f"{heading}\n{bullets}" if heading else bullets)
    parts.append(data.get("additionalPlain") or "")
    lines = (line.strip() for part in parts for line in part.replace("\xa0", " ").splitlines())
    return "\n".join(line for line in lines if line)
