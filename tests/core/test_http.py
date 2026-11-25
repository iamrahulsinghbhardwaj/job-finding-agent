import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from jobagent.core.http import (
    USER_AGENT,
    BlockedError,
    FetchError,
    PoliteClient,
    TransientError,
    _parse_retry_after,  # pyright: ignore[reportPrivateUsage]
    safe_url,
)

URL = "https://api.example.com/jobs"
OTHER_URL = "https://boards.other.org/jobs"


class FakeClock:
    """Virtual time: sleep() advances the clock instantly and records the duration."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # still yield to the event loop


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[PoliteClient]:
    # min_delay=0 so retry tests only see backoff sleeps; delay tests build their own client.
    async with PoliteClient(min_delay=0, clock=clock, sleep=clock.sleep) as polite:
        yield polite


@respx.mock
async def test_get_bytes_returns_the_raw_body(client: PoliteClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"%PDF-\x00\xff"))
    assert await client.get_bytes(URL) == b"%PDF-\x00\xff"


# --- retries -------------------------------------------------------------------


@respx.mock
async def test_retries_on_503_then_succeeds(client: PoliteClient, clock: FakeClock) -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json={"jobs": [1, 2]})])
    assert await client.get_json(URL) == {"jobs": [1, 2]}
    assert route.call_count == 2
    assert len(clock.sleeps) == 1
    assert 1.0 <= clock.sleeps[0] <= 2.0  # exponential backoff (1s) + up to 1s jitter
    stats = client.stats()["api.example.com"]
    assert (stats.requests, stats.retries, stats.errors) == (2, 1, 0)


@respx.mock
async def test_gives_up_after_3_attempts(client: PoliteClient, clock: FakeClock) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(TransientError, match=r"HTTP 503 from api\.example\.com") as exc_info:
        await client.get_json(URL)
    assert exc_info.value.status == 503
    assert route.call_count == 3
    assert len(clock.sleeps) == 2
    assert 2.0 <= clock.sleeps[1] <= 3.0  # backoff doubles
    stats = client.stats()["api.example.com"]
    assert (stats.requests, stats.retries, stats.errors) == (3, 2, 1)


@respx.mock
async def test_retries_on_timeout_and_connection_error(client: PoliteClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.Response(200, text="ok")]
    )
    assert await client.get_text(URL) == "ok"
    assert route.call_count == 3


@respx.mock
async def test_429_respects_retry_after_seconds(client: PoliteClient, clock: FakeClock) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    assert await client.get_json(URL) == {}
    assert clock.sleeps == [7.0]


@respx.mock
async def test_retry_after_is_capped(client: PoliteClient, clock: FakeClock) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "3600"}), httpx.Response(200, json={})]
    )
    await client.get_json(URL)
    assert clock.sleeps == [60.0]


# --- no retries ------------------------------------------------------------------


@respx.mock
async def test_403_raises_blocked_without_retry(client: PoliteClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(403, text="secret body"))
    with pytest.raises(BlockedError) as exc_info:
        await client.get_json(URL)
    assert route.call_count == 1
    assert exc_info.value.status == 403
    assert exc_info.value.host == "api.example.com"
    assert "secret body" not in str(exc_info.value)  # never leak response bodies
    assert client.stats()["api.example.com"].errors == 1


@pytest.mark.parametrize("status", [400, 401, 404, 410])
@respx.mock
async def test_other_4xx_not_retried(client: PoliteClient, status: int) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc_info:
        await client.get_json(URL)
    assert not isinstance(exc_info.value, TransientError | BlockedError)
    assert route.call_count == 1


@respx.mock
async def test_invalid_json_raises_fetch_error(client: PoliteClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(FetchError, match="invalid JSON"):
        await client.get_json(URL)


# --- politeness ----------------------------------------------------------------


@respx.mock
async def test_per_domain_delay_is_enforced(clock: FakeClock) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    async with PoliteClient(min_delay=1.0, clock=clock, sleep=clock.sleep) as polite:
        for _ in range(3):
            await polite.get_json(URL)
    assert clock.sleeps == [1.0, 1.0]  # first request immediate, then 1s apart


@respx.mock
async def test_delay_only_waits_for_the_remainder(clock: FakeClock) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    async with PoliteClient(min_delay=1.0, clock=clock, sleep=clock.sleep) as polite:
        await polite.get_json(URL)
        clock.now += 0.75  # time spent doing other work
        await polite.get_json(URL)
    assert clock.sleeps == [pytest.approx(0.25)]


@respx.mock
async def test_different_domains_do_not_delay_each_other(clock: FakeClock) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    respx.get(OTHER_URL).mock(return_value=httpx.Response(200, json={}))
    async with PoliteClient(min_delay=1.0, clock=clock, sleep=clock.sleep) as polite:
        await polite.get_json(URL)
        await polite.get_json(OTHER_URL)
        assert set(polite.stats()) == {"api.example.com", "boards.other.org"}
    assert clock.sleeps == []


@respx.mock
async def test_per_domain_concurrency_limit(clock: FakeClock) -> None:
    in_flight = 0
    peak = 0

    async def slow_response(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        for _ in range(5):
            await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={})

    respx.get(URL).mock(side_effect=slow_response)
    async with PoliteClient(per_domain_concurrency=2, min_delay=0, clock=clock, sleep=clock.sleep) as polite:
        await asyncio.gather(*(polite.get_json(URL) for _ in range(6)))
    assert peak == 2


# --- misc ------------------------------------------------------------------------


@respx.mock
async def test_sends_user_agent_and_params(client: PoliteClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    await client.get_json(URL, params={"page": 2})
    request = route.calls.last.request
    assert request.headers["User-Agent"] == USER_AGENT
    assert "github.com" in USER_AGENT
    assert "@" not in USER_AGENT  # no email address
    assert request.url.params["page"] == "2"


def test_rejects_bad_config() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        PoliteClient(per_domain_concurrency=0)


def test_parse_retry_after() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("12") == 12.0
    assert _parse_retry_after("soon") is None
    in_30s = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= (_parse_retry_after(in_30s) or 0) <= 30
    assert _parse_retry_after("Mon, 01 Jan 2001 00:00:00 GMT") == 0.0


@respx.mock
async def test_cookies_are_never_stored_or_sent(client: PoliteClient) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={}, headers={"Set-Cookie": "session=abc123; Path=/"}),
            httpx.Response(200, json={}),
        ]
    )
    await client.get_json(URL)
    await client.get_json(URL)
    assert "cookie" not in route.calls[1].request.headers  # Set-Cookie was not sent back
    assert list(client._client.cookies.jar) == []  # pyright: ignore[reportPrivateUsage]


@respx.mock
async def test_request_urls_never_reach_the_logs(
    client: PoliteClient, caplog: pytest.LogCaptureFixture
) -> None:
    # httpx logs each request's full URL at INFO; a SAS signature in the query must not leak.
    secret_url = f"{URL}?sig=FAKESIG"
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))  # any query
    with caplog.at_level(logging.DEBUG):
        await client.get_json(secret_url, params={"token": "FAKETOKEN"})
    assert "FAKESIG" not in caplog.text
    assert "FAKETOKEN" not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.name == "jobagent.core.http"]
    assert lines == ["GET api.example.com/jobs -> 200 in 0ms"]


@respx.mock
async def test_failed_request_line_has_no_query(
    client: PoliteClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with caplog.at_level(logging.DEBUG), pytest.raises(TransientError):
        await client.get_json(f"{URL}?sig=FAKESIG")
    lines = [r.getMessage() for r in caplog.records if r.name == "jobagent.core.http"]
    assert lines == ["GET api.example.com/jobs -> ConnectTimeout"] * 3  # one per attempt
    assert "FAKESIG" not in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://fake.blob.core.windows.net/c/x.pdf?sig=FAKESIG", "fake.blob.core.windows.net/c/x.pdf"),
        ("https://api.example.com/jobs?page=2#top", "api.example.com/jobs"),
        ("https://api.example.com", "api.example.com/"),
    ],
)
def test_safe_url_keeps_host_and_path_only(url: str, expected: str) -> None:
    assert safe_url(url) == expected
