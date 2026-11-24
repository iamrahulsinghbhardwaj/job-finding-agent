"""Cross-source dedupe (HLD §7 step 2): the same role listed on two sources is kept once.

Within one source, dedupe is the DB upsert on (company, source_job_id). Here, jobs from DIFFERENT
sources collapse on company + canonical title + country, e.g. a YC listing and the same role on
Ashby. Jobs from the same source are never merged: two Amazon reqs titled "SDE II, Payments" in
Hyderabad are two openings. Jobs with no country are never merged (nothing safe to match on).
"""

from collections.abc import Iterable

from jobagent.core.models import NormalizedJob

DedupeKey = tuple[str, str, str]


def dedupe_key(job: NormalizedJob) -> DedupeKey | None:
    if job.country is None:
        return None
    return (job.company_slug, job.title_canonical.casefold(), job.country)


def collapse_cross_source(jobs: Iterable[NormalizedJob]) -> list[NormalizedJob]:
    """Keep jobs in order, dropping any that duplicate a job from an earlier, different source.

    The first source to list a key owns it, so pass jobs in source-preference order.
    """
    owner: dict[DedupeKey, str] = {}
    kept: list[NormalizedJob] = []
    for job in jobs:
        key = dedupe_key(job)
        if key is None or owner.setdefault(key, job.source) == job.source:
            kept.append(job)
    return kept
