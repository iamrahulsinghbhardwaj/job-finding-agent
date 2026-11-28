"""Dead-man's switch: ping HEALTHCHECK_URL at the end of a successful sending run.

A check service (e.g. healthchecks.io) alerts you when no ping arrives within its period, which
catches the silent failures: the schedule paused, a run that never started, the runner gone.
The ping must never fail the run: any error is swallowed and logged as "failed" (never the URL).
"""

import logging

import httpx

from jobagent.core.http import USER_AGENT

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0


def ping(url: str, *, timeout: float = TIMEOUT_SECONDS, transport: httpx.BaseTransport | None = None) -> bool:
    """GET the check URL. True when it answered < 400; False on any error (never raises)."""
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
            ok = client.get(url).status_code < 400
    except Exception:  # a broken ping must never break the run
        ok = False
    log.info("healthcheck ping: %s", "ok" if ok else "failed")
    return ok
