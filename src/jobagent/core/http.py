"""Polite HTTP client. All crawling of public sites goes through PoliteClient.

Rule: PoliteClient for crawling public sites (job boards, careers APIs) and plain downloads (the
CV blob, the tiktoken table). The APIs of your own accounts use their SDKs: OpenAI (core/llm),
Gmail (digest/gmail).

- One shared httpx.AsyncClient (timeouts, HTTP/2, identifying User-Agent).
- Per domain: at most `per_domain_concurrency` requests in flight, and at least `min_delay`
  seconds between the start of consecutive requests (retries included).
- Retries timeouts, connection errors, 429 and 5xx with exponential backoff + jitter,
  honouring Retry-After. Never retries other 4xx; 403 raises BlockedError so the caller
  reports and stops (no workarounds).
- Never stores or sends cookies (a reject-all cookie jar), so a site that needs cookies
  fails visibly instead of silently working through a session.
- Error messages carry status and host only, never response bodies (logs are public), and the
  httpx request log (full URLs) is turned off.
"""

import asyncio
import http.cookiejar
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from jobagent import __version__

USER_AGENT = f"jobagent/{__version__} (+https://github.com/iamrahulsinghbhardwaj/job-finding-agent)"
MAX_RETRY_AFTER = 60.0

# httpx (and httpx2, under the openai SDK) log every request at INFO with the full URL, query
# included: that would put the CV link's SAS signature in public logs. Quiet them for every caller.
# PoliteClient logs its own line instead: host + path, the query string stripped.
URL_LOGGERS = ("httpx", "httpx2")
for _name in URL_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

log = logging.getLogger(__name__)

_Scalar = str | int | float | bool
# A mapping, or a sequence of pairs when a key repeats (e.g. category[]=a&category[]=b).
Params = Mapping[str, _Scalar] | Sequence[tuple[str, _Scalar]] | None
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class _RejectAllCookies(http.cookiejar.DefaultCookiePolicy):
    """Cookie policy that never accepts a cookie and never returns one."""

    def set_ok(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        return False


class FetchError(Exception):
    """Base for every HTTP failure raised by PoliteClient."""

    def __init__(self, message: str, *, host: str, status: int | None = None) -> None:
        super().__init__(message)
        self.host = host
        self.status = status


class BlockedError(FetchError):
    """The site returned 403. Stop and report; do not work around it."""


class TransientError(FetchError):
    """A failure worth retrying (timeout, connection error, 429, 5xx)."""

    def __init__(
        self, message: str, *, host: str, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, host=host, status=status)
        self.retry_after = retry_after


@dataclass
class DomainStats:
    requests: int = 0  # every attempt sent, retries included
    retries: int = 0
    errors: int = 0  # requests that finally failed


@dataclass
class _DomainState:
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request_at: float | None = None
    stats: DomainStats = field(default_factory=DomainStats)


class PoliteClient:
    def __init__(
        self,
        *,
        per_domain_concurrency: int = 2,
        min_delay: float = 1.0,
        max_attempts: int = 3,
        timeout: float = 20.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if per_domain_concurrency < 1 or min_delay < 0 or max_attempts < 1:
            raise ValueError("concurrency and attempts must be >= 1, delay >= 0")
        self._concurrency = per_domain_concurrency
        self._min_delay = min_delay
        self._max_attempts = max_attempts
        self._clock = clock
        self._sleep = sleep
        self._backoff = wait_exponential_jitter(initial=1, max=30, jitter=1)
        self._domains: dict[str, _DomainState] = {}
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            cookies=http.cookiejar.CookieJar(policy=_RejectAllCookies()),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, params: Params = None) -> Any:
        response = await self._get(url, params)
        try:
            return response.json()
        except ValueError:
            host = response.url.host
            raise FetchError(f"invalid JSON from {host}", host=host, status=response.status_code) from None

    async def get_text(self, url: str, params: Params = None) -> str:
        return (await self._get(url, params)).text

    async def get_bytes(self, url: str, params: Params = None) -> bytes:
        """Raw body, for binary files such as the CV PDF."""
        return (await self._get(url, params)).content

    def stats(self) -> dict[str, DomainStats]:
        """Per-domain counts (copies), for the run log."""
        return {host: replace(state.stats) for host, state in self._domains.items()}

    # --- internals ------------------------------------------------------------

    async def _get(self, url: str, params: Params) -> httpx.Response:
        host = httpx.URL(url).host
        state = self._domain(host)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception_type(TransientError),
            sleep=self._sleep,
            before_sleep=lambda _: self._count_retry(state),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._attempt(url, params, host, state)
        except FetchError:
            state.stats.errors += 1
            raise
        raise AssertionError("unreachable: tenacity either returns or reraises")

    async def _attempt(self, url: str, params: Params, host: str, state: _DomainState) -> httpx.Response:
        async with state.semaphore:
            await self._wait_turn(state)
            state.stats.requests += 1
            started = self._clock()
            try:
                response = await self._client.get(url, params=_query(params))
            except httpx.TransportError as exc:  # timeouts, connection errors, protocol errors
                log.debug("GET %s -> %s", safe_url(url), type(exc).__name__)
                raise TransientError(f"{type(exc).__name__} from {host}", host=host) from None
        elapsed_ms = round((self._clock() - started) * 1000)
        log.debug("GET %s -> %d in %dms", safe_url(url), response.status_code, elapsed_ms)
        return self._check_status(response, host)

    def _check_status(self, response: httpx.Response, host: str) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        message = f"HTTP {status} from {host}"
        if status == 403:
            raise BlockedError(message, host=host, status=status)
        if status == 429 or status >= 500:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise TransientError(message, host=host, status=status, retry_after=retry_after)
        raise FetchError(message, host=host, status=status)

    async def _wait_turn(self, state: _DomainState) -> None:
        """Space out requests to one domain by at least min_delay seconds."""
        async with state.lock:
            if state.last_request_at is not None:
                wait = state.last_request_at + self._min_delay - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            state.last_request_at = self._clock()

    def _wait(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, TransientError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER)
        return self._backoff(retry_state)

    def _domain(self, host: str) -> _DomainState:
        if host not in self._domains:
            self._domains[host] = _DomainState(semaphore=asyncio.Semaphore(self._concurrency))
        return self._domains[host]

    @staticmethod
    def _count_retry(state: _DomainState) -> None:
        state.stats.retries += 1


def safe_url(url: str) -> str:
    """host + path only, for logs: the query string (tokens, SAS signatures) and fragment dropped."""
    parsed = httpx.URL(url)
    return f"{parsed.host}{parsed.path}"


def _query(params: Params) -> httpx.QueryParams | None:
    if params is None:
        return None
    return httpx.QueryParams(params if isinstance(params, Mapping) else list(params))


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delay-seconds or an HTTP date."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
