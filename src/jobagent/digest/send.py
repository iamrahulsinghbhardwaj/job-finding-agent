"""Deliver the digest exactly once (HLD §11: "Duplicate email on retry").

1. reconcile(): runs left in "sending" (the process stopped mid-send) are looked up in Gmail:
   by our Message-ID first, else by the subject token "[r<run id>]" within +/-1 h of the attempt.
   Found -> "sent" (never resent). Not found -> "failed" (its jobs go out again).
2. deliver(): record the run's job ids with status "sending" and COMMIT, then send, then mark
   "sent" (with Gmail's id) or "failed", and commit again.
Jobs of "sent" runs never go out again, nor jobs of runs still "sending" (repo.held_job_ids); a
"failed" run's jobs appear in the next digest with anything new: one email, nothing lost, nothing
duplicated.

This module commits: recording before sending is the point. Logs are counts and run ids only.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from jobagent.db import repo
from jobagent.digest.gmail import Mailer, MailError, build_message, new_message_id, subject_token
from jobagent.digest.render import render_digest, render_text
from jobagent.digest.select import Digest

log = logging.getLogger(__name__)

LOOKUP_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class Delivery:
    status: str  # "sent" or "failed"
    run_id: int
    jobs: int
    error: str | None = None  # safe to print


@dataclass
class Reconciled:
    by_message_id: list[int] = field(default_factory=list)  # run ids found by Message-ID
    by_subject: list[int] = field(default_factory=list)  # run ids found by the subject token
    failed: list[int] = field(default_factory=list)  # not in Gmail: their jobs go out again
    errors: list[str] = field(default_factory=list)  # lookups that failed: run left "sending"


def subject_with_token(digest: Digest, run_id: int) -> str:
    return f"{digest.subject} {subject_token(run_id)}"


def reconcile(session: Session, mailer: Mailer) -> Reconciled:
    """Settle runs whose send outcome is unknown. A failed lookup leaves the run "sending" (its
    jobs stay held back rather than risk a duplicate) and is reported."""
    result = Reconciled()
    for run in repo.sending_runs(session):
        try:
            gmail_id = (
                mailer.find_by_message_id(run.digest_rfc_message_id) if run.digest_rfc_message_id else None
            )
            method = result.by_message_id
            if gmail_id is None and run.digest_attempted_at is not None:
                start, end = run.digest_attempted_at - LOOKUP_WINDOW, run.digest_attempted_at + LOOKUP_WINDOW
                gmail_id = mailer.find_by_subject_token(subject_token(run.id), start, end)
                method = result.by_subject
        except MailError as exc:
            result.errors.append(f"run {run.id}: {exc}")
            continue
        if gmail_id is not None:
            repo.mark_digest_sent(session, run.id, gmail_id)
            method.append(run.id)
        else:
            repo.mark_digest_failed(session, run.id)
            result.failed.append(run.id)
        session.commit()
    if result.by_message_id or result.by_subject or result.failed:
        log.info(
            "digest reconcile: %d found by Message-ID, %d by subject, %d not sent",
            len(result.by_message_id),
            len(result.by_subject),
            len(result.failed),
        )
    return result


def deliver(session: Session, digest: Digest, *, run_id: int, to: str, mailer: Mailer) -> Delivery:
    """Record, then send, then mark. Never raises for a send failure: returns status "failed"."""
    message_id = new_message_id(run_id)
    repo.begin_digest(session, run_id, digest.job_ids, message_id)
    session.commit()  # the record exists before anything leaves

    message = build_message(
        to=to,
        subject=subject_with_token(digest, run_id),
        html=render_digest(digest),
        text=render_text(digest),
        message_id=message_id,
    )
    try:
        gmail_id = mailer.send(message.as_bytes())
    except MailError as exc:
        repo.mark_digest_failed(session, run_id)
        session.commit()
        jobs = len(digest.job_ids)
        log.warning("digest run %d: send failed (%s); its %d jobs go out next run", run_id, exc, jobs)
        return Delivery("failed", run_id, len(digest.job_ids), str(exc))
    repo.mark_digest_sent(session, run_id, gmail_id)
    session.commit()
    log.info("digest run %d: sent, %d jobs", run_id, len(digest.job_ids))
    return Delivery("sent", run_id, len(digest.job_ids))
