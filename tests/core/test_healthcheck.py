import logging

import httpx
import pytest

from jobagent.core.healthcheck import ping
from jobagent.core.settings import Settings
from jobagent.jobs.run import final_ping

URL = "https://hc-ping.example.com/PRIVATE-CHECK-UUID"


def transport(status: int = 200, error: BaseException | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if error:
            raise error
        assert request.url == URL
        return httpx.Response(status)

    return httpx.MockTransport(handle)


def test_ok_ping() -> None:
    assert ping(URL, transport=transport(200)) is True


@pytest.mark.parametrize(
    "mock",
    [
        transport(500),
        transport(404),
        transport(error=httpx.ConnectTimeout("slow")),
        transport(error=OSError("dns")),
    ],
)
def test_a_failed_ping_returns_false_and_never_raises(mock: httpx.MockTransport) -> None:
    assert ping(URL, transport=mock) is False


def test_ping_never_logs_the_url(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        ping(URL, transport=transport(200))
        ping(URL, transport=transport(error=OSError("dns")))
    assert "PRIVATE-CHECK-UUID" not in caplog.text
    assert "healthcheck ping: ok" in caplog.text
    assert "healthcheck ping: failed" in caplog.text


def test_short_timeout() -> None:
    from jobagent.core import healthcheck

    assert healthcheck.TIMEOUT_SECONDS <= 10


@pytest.mark.parametrize(
    ("code", "dry_run", "url", "pinged"),
    [
        (0, False, URL, True),  # successful sending run: ping
        (0, True, URL, False),  # dry run: never (it must not hide stopped sends)
        (1, False, URL, False),  # failed run: never
        (0, False, None, False),  # no HEALTHCHECK_URL: nothing to do
    ],
)
def test_final_ping_only_after_a_successful_send(
    monkeypatch: pytest.MonkeyPatch, code: int, dry_run: bool, url: str | None, pinged: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("jobagent.core.healthcheck.ping", lambda u, **_: calls.append(u) or True)
    if url:
        monkeypatch.setenv("HEALTHCHECK_URL", url)
    result = final_ping(code, dry_run=dry_run, settings=Settings())
    assert (calls == [URL]) is pinged
    assert (result is True) is pinged
