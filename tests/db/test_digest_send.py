"""Exactly-once digest delivery against the test database, with a fake Gmail (no network).

The property under test: across any sequence of runs, failures and crashes, every job reaches the
inbox in exactly one accepted email (never twice), and a failed send's jobs are never lost.
"""

import logging
from collections import Counter

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobagent.core.settings import Settings
from jobagent.db import repo
from jobagent.db.models import Run
from jobagent.digest.gmail import new_message_id
from jobagent.digest.select import select_digest
from jobagent.digest.send import deliver, reconcile
from jobagent.jobs.run import digest_step
from jobagent.pipeline.profile import ProfileSync
from jobagent.pipeline.profile_schema import CandidateProfile
from tests.db.conftest import needs_db
from tests.db.test_digest_store import HASH, TODAY, job, rank, seed
from tests.mail_fakes import FakeMailer

pytestmark = needs_db

ADDRESS = "private.person@example.com"
PROFILE = ProfileSync(
    1,
    False,
    None,
    CandidateProfile(
        years_experience=4,
        seniority="mid",
        primary_stack=[],
        secondary_skills=[],
        domains=[],
        notable_projects=[],
        target_titles=[],
        roles=[],
    ),
    (),
    HASH,
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in {
        "GMAIL_CLIENT_ID": "id",
        "GMAIL_CLIENT_SECRET": "secret",
        "GMAIL_REFRESH_TOKEN": "refresh",
        "DIGEST_TO": ADDRESS,
    }.items():
        monkeypatch.setenv(name, value)
    return Settings()


def add_jobs(session: Session, *ids: str) -> dict[str, int]:
    """Store and rank High new jobs (Bengaluru), each with its own description."""
    stored = seed(session, {"acme": [job("acme", i) for i in ids]})
    for job_id in stored.values():
        rank(session, job_id, 85, "High")
    return {key[1]: job_id for key, job_id in stored.items()}


def run_once(session: Session, settings: Settings, mailer: FakeMailer) -> tuple[list[str], int, int]:
    run_id = repo.start_run(session)
    session.commit()
    lines, code = digest_step(
        session, PROFILE, run_id=run_id, settings=settings, dry_run=False, mailer_factory=lambda _: mailer
    )
    return lines, code, run_id


def status(session: Session, run_id: int) -> str | None:
    session.expire_all()
    return session.scalar(select(Run.digest_status).where(Run.id == run_id))


def assert_each_job_once(mailer: FakeMailer, expected: int) -> None:
    counts = Counter(mailer.jobs_emailed())
    assert len(counts) == expected
    assert set(counts.values()) == {1}, f"a job was emailed twice: {counts}"


# --- the normal path ---------------------------------------------------------------------------


def test_send_marks_the_run_sent_and_a_second_run_sends_nothing(session: Session, settings: Settings) -> None:
    add_jobs(session, "1", "2")
    mailer = FakeMailer()
    lines, code, run_id = run_once(session, settings, mailer)
    assert code == 0
    assert lines[-1].endswith(f"-> sent (run {run_id})")
    assert status(session, run_id) == repo.DIGEST_SENT
    [message] = mailer.sent
    assert message["To"] == ADDRESS
    assert str(message["Subject"]).endswith(f"[r{run_id}]")

    lines, code, _ = run_once(session, settings, mailer)
    assert (lines, code) == (["digest: no new jobs, no email"], 0)
    assert len(mailer.sent) == 1


def test_ids_are_recorded_and_committed_before_sending(session: Session, settings: Settings) -> None:
    ids = add_jobs(session, "1")
    mailer = FakeMailer()
    seen: list[tuple[str | None, list[int]]] = []

    def inspect() -> None:
        run = session.scalars(select(Run).order_by(Run.id.desc()).limit(1)).one()
        seen.append((run.digest_status, list(run.emailed_job_ids)))

    mailer.on_send = inspect
    run_once(session, settings, mailer)
    assert seen == [(repo.DIGEST_SENDING, [ids["1"]])]


# --- failures: retry never double-sends ---------------------------------------------------------


def test_failed_send_marks_failed_and_the_next_run_sends_old_plus_new_once(
    session: Session, settings: Settings
) -> None:
    add_jobs(session, "1", "2")
    mailer = FakeMailer(fail_sends=1)
    lines, code, first = run_once(session, settings, mailer)
    assert code == 1
    assert "send failed" in lines[-1]
    assert status(session, first) == repo.DIGEST_FAILED
    assert mailer.sent == []

    add_jobs(session, "3")  # a new job arrives before the retry
    lines, code, second = run_once(session, settings, mailer)
    assert code == 0
    assert status(session, second) == repo.DIGEST_SENT
    assert len(mailer.sent) == 1  # one email with the failed run's jobs and the new one
    assert_each_job_once(mailer, 3)


def test_crash_after_gmail_accepted_is_found_by_message_id_and_not_resent(
    session: Session, settings: Settings
) -> None:
    add_jobs(session, "1", "2")
    mailer = FakeMailer()
    crashed = _send_then_crash(session, mailer)

    lines, code, _ = run_once(session, settings, mailer)
    assert code == 0
    assert status(session, crashed) == repo.DIGEST_SENT
    assert f"runs [{crashed}] found by Message-ID" in lines[0]
    assert lines[-1] == "digest: no new jobs, no email"
    assert len(mailer.sent) == 1
    assert_each_job_once(mailer, 2)


def test_rewritten_message_id_falls_back_to_the_subject_token(session: Session, settings: Settings) -> None:
    add_jobs(session, "1")
    mailer = FakeMailer(rewrite_message_id=True)
    crashed = _send_then_crash(session, mailer)
    lines, _, _ = run_once(session, settings, mailer)
    assert mailer.lookups == ["message_id", "subject"]
    assert f"[{crashed}] by subject token" in lines[0]
    assert status(session, crashed) == repo.DIGEST_SENT
    assert len(mailer.sent) == 1


def test_crash_before_gmail_accepted_resends_once(session: Session, settings: Settings) -> None:
    ids = add_jobs(session, "1", "2")
    mailer = FakeMailer()
    crashed = repo.start_run(session)
    repo.begin_digest(session, crashed, ids.values(), new_message_id(crashed))  # recorded, never sent
    session.commit()

    lines, code, retry = run_once(session, settings, mailer)
    assert code == 0
    assert status(session, crashed) == repo.DIGEST_FAILED
    assert status(session, retry) == repo.DIGEST_SENT
    assert f"[{crashed}] not sent" in lines[0]
    assert_each_job_once(mailer, 2)


def test_lookup_failure_holds_jobs_back_rather_than_risk_a_duplicate(
    session: Session, settings: Settings
) -> None:
    add_jobs(session, "1")
    mailer = FakeMailer()
    crashed = _send_then_crash(session, mailer)
    mailer.search_error = True
    add_jobs(session, "2")
    lines, code, _ = run_once(session, settings, mailer)
    assert code == 1
    assert any("reconcile failed, jobs held back" in line for line in lines)
    assert status(session, crashed) == repo.DIGEST_SENDING  # unknown stays unknown
    assert_each_job_once(mailer, 2)  # job 1 not resent; job 2 sent once

    mailer.search_error = False  # Gmail is back: the run is settled, nothing duplicated
    run_once(session, settings, mailer)
    assert status(session, crashed) == repo.DIGEST_SENT
    assert_each_job_once(mailer, 2)


def test_many_retries_never_double_send(session: Session, settings: Settings) -> None:
    add_jobs(session, "1", "2")
    mailer = FakeMailer(fail_sends=3)
    for _ in range(3):
        assert run_once(session, settings, mailer)[1] == 1
    add_jobs(session, "3")
    assert run_once(session, settings, mailer)[1] == 0
    for _ in range(2):
        run_once(session, settings, mailer)
    assert mailer.send_calls == 4
    assert len(mailer.sent) == 1
    assert_each_job_once(mailer, 3)


# --- settings, dry run, logs ---------------------------------------------------------------------


def test_missing_gmail_settings_send_nothing(session: Session) -> None:
    add_jobs(session, "1")  # tests run in an empty tmp dir (conftest), so out/ lands there
    mailer = FakeMailer()
    run_id = repo.start_run(session)
    lines, code = digest_step(
        session, PROFILE, run_id=run_id, settings=Settings(), dry_run=False, mailer_factory=lambda _: mailer
    )
    assert code == 1
    assert lines[0].startswith("digest not sent: missing GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET")
    assert mailer.send_calls == 0
    assert status(session, run_id) is None


def test_dry_run_neither_sends_nor_records(session: Session, settings: Settings) -> None:
    add_jobs(session, "1")
    mailer = FakeMailer()
    run_id = repo.start_run(session)
    lines, code = digest_step(
        session, PROFILE, run_id=run_id, settings=settings, dry_run=True, mailer_factory=lambda _: mailer
    )
    assert code == 0
    assert "dry run, not sent" in lines[0]
    assert mailer.send_calls == 0
    assert status(session, run_id) is None
    assert select_digest(session, HASH, TODAY) is not None  # still pending


def test_logs_never_carry_address_subject_or_titles(
    session: Session, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    add_jobs(session, "1")
    with caplog.at_level(logging.DEBUG):
        lines, _, _ = run_once(session, settings, FakeMailer())
    text = caplog.text + "\n".join(lines)
    for secret in (ADDRESS, "Senior Backend Engineer", "1 new:"):
        assert secret not in text
    assert "digest run" in caplog.text


def test_deliver_and_reconcile_directly(session: Session) -> None:
    ids = add_jobs(session, "1")
    digest = select_digest(session, HASH, TODAY)
    assert digest is not None
    run_id = repo.start_run(session)
    mailer = FakeMailer()
    delivery = deliver(session, digest, run_id=run_id, to=ADDRESS, mailer=mailer)
    assert (delivery.status, delivery.jobs) == ("sent", 1)
    assert reconcile(session, mailer).failed == []  # nothing left "sending"
    assert repo.held_job_ids(session) == {ids["1"]}


class Crash(BaseException):
    """The process dies (not an Exception: nothing catches it)."""


def _send_then_crash(session: Session, mailer: FakeMailer) -> int:
    """Gmail accepted the digest, then the process died before marking the run "sent"."""
    digest = select_digest(session, HASH, TODAY)
    assert digest is not None
    crashed = repo.start_run(session)

    def die(*_: object, **__: object) -> None:
        raise Crash

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repo, "mark_digest_sent", die)
        with pytest.raises(Crash):
            deliver(session, digest, run_id=crashed, to=ADDRESS, mailer=mailer)
    session.rollback()
    assert status(session, crashed) == repo.DIGEST_SENDING
    return crashed
