from jobagent.core.models import NormalizedJob, RawJob
from jobagent.pipeline.dedupe import collapse_cross_source, dedupe_key
from jobagent.pipeline.normalize import normalize
from tests.pipeline.test_normalize import raw_job

AMAZON = "amazon:amazon.jobs"
OTHER = "yc:amazon"


def job(
    job_id: str, source: str, title: str = "SDE II , Payments", location: str = "Hyderabad"
) -> NormalizedJob:
    raw: RawJob = raw_job(job_id, title=title, location=location)
    return normalize(raw, source=source)


def ids(jobs: list[NormalizedJob]) -> list[tuple[str, str]]:
    return [(j.source, j.source_job_id) for j in jobs]


def test_same_source_same_canonical_title_both_survive() -> None:
    # Two Amazon reqs that read the same once canonical: two openings, never merged.
    first = job("1", AMAZON, title="SDE II, Payments")
    second = job("2", AMAZON, title="Software Dev Engineer 2, Payments")
    assert first.title_canonical == second.title_canonical
    assert ids(collapse_cross_source([first, second])) == [(AMAZON, "1"), (AMAZON, "2")]


def test_different_sources_collapse_and_first_source_wins() -> None:
    jobs = [job("1", AMAZON), job("x9", OTHER, title="Software Development Engineer II, Payments")]
    assert ids(collapse_cross_source(jobs)) == [(AMAZON, "1")]


def test_owner_source_keeps_all_its_jobs_while_others_drop() -> None:
    jobs = [job("1", AMAZON), job("x9", OTHER), job("2", AMAZON)]
    assert ids(collapse_cross_source(jobs)) == [(AMAZON, "1"), (AMAZON, "2")]


def test_different_country_or_title_is_not_a_duplicate() -> None:
    jobs = [
        job("1", AMAZON),
        job("x1", OTHER, location="London"),
        job("x2", OTHER, title="SDE III, Payments"),
    ]
    assert len(collapse_cross_source(jobs)) == 3


def test_unknown_country_is_never_merged() -> None:
    jobs = [job("1", AMAZON, location="N/A"), job("x9", OTHER, location="N/A")]
    assert dedupe_key(jobs[0]) is None
    assert len(collapse_cross_source(jobs)) == 2


def test_key_ignores_case() -> None:
    a = job("1", AMAZON, title="Software Engineer II, Payments")
    b = job("x9", OTHER, title="software engineer ii, payments")
    assert dedupe_key(a) == dedupe_key(b)
