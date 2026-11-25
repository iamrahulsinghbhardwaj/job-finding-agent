import logging
from decimal import Decimal

import httpx2  # the HTTP library the openai SDK is built on
import openai
import pytest
from pydantic import BaseModel

from jobagent.core.llm import LlmError, LlmLedger, estimate_cost, parse, price_for
from jobagent.core.llm_prices import PRICES, PRICES_CHECKED
from tests.llm_fakes import FakeOpenAI, completion

MODEL = "gpt-5.4-mini"
SECRET = "Jane Doe private CV text"


class Answer(BaseModel):
    value: int


class FakeClock:
    def __init__(self, *times: float) -> None:
        self.times = list(times)

    def __call__(self) -> float:
        return self.times.pop(0)


def _messages() -> list:
    return [{"role": "user", "content": SECRET}]


async def test_parse_returns_output_and_records_the_call() -> None:
    fake = FakeOpenAI(
        completion(Answer(value=7), prompt_tokens=1000, cached_tokens=400, completion_tokens=200)
    )
    ledger = LlmLedger()

    answer, call = await parse(
        fake.client,
        model=MODEL,
        messages=_messages(),
        schema=Answer,
        purpose="test",
        ledger=ledger,
        clock=FakeClock(10.0, 10.25),
    )

    assert answer == Answer(value=7)
    assert (call.purpose, call.model) == ("test", MODEL)
    assert (call.input_tokens, call.cached_tokens, call.output_tokens) == (1000, 400, 200)
    assert call.latency_ms == 250
    # 600 uncached * 0.75 + 400 cached * 0.075 + 200 out * 4.50, per million
    assert call.cost_usd == Decimal("0.00138")
    assert ledger.calls == [call]
    request = fake.requests[0]
    assert request["model"] == MODEL
    assert request["response_format"] is Answer
    assert request["messages"] == _messages()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.4-nano", Decimal("0.00045")),  # 1000 * 0.20 + 200 * 1.25
        ("gpt-5.4-mini-2026-03-17", Decimal("0.00165")),  # dated snapshot priced as its family
        ("some-new-model", None),
    ],
)
def test_estimate_cost(model: str, expected: Decimal | None) -> None:
    assert estimate_cost(model, 1000, 0, 200) == expected


def test_cached_tokens_never_exceed_input() -> None:
    assert estimate_cost(MODEL, 100, 500, 0) == estimate_cost(MODEL, 100, 100, 0)


def test_price_lookup() -> None:
    assert price_for("gpt-5.4-nano") is not None
    assert price_for("gpt-5.4-nano-2026-03-17") == price_for("gpt-5.4-nano")
    assert price_for("gpt-4") is None


async def test_unknown_model_cost_is_unknown_not_guessed(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeOpenAI(completion(Answer(value=1)))
    ledger = LlmLedger()
    with caplog.at_level(logging.INFO, logger="jobagent.core.llm"):
        _, call = await parse(
            fake.client, model="mystery", messages=_messages(), schema=Answer, purpose="t", ledger=ledger
        )
    assert call.cost_usd is None
    assert ledger.unpriced == 1
    assert ledger.cost_usd == 0
    assert "cost=unknown" in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        f"no price for model mystery in llm_prices (prices checked {PRICES_CHECKED}): cost unknown"
    )


def test_known_model_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert estimate_cost(MODEL, 1000, 0, 200) is not None
    assert caplog.records == []


def test_every_price_is_positive_and_cached_is_cheaper() -> None:
    assert PRICES  # at least the models in use
    for price in PRICES.values():
        assert 0 < price.cached_input <= price.input
        assert price.output > 0


async def test_missing_usage_records_zero_tokens() -> None:
    fake = FakeOpenAI(completion(Answer(value=1), usage=False))
    _, call = await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t")
    assert (call.input_tokens, call.cached_tokens, call.output_tokens) == (0, 0, 0)
    assert call.cost_usd == 0


async def test_cached_tokens_none_reads_as_zero() -> None:
    fake = FakeOpenAI(completion(Answer(value=1), cached_tokens=None))
    _, call = await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t")
    assert call.cached_tokens == 0


def test_ledger_totals() -> None:
    ledger = LlmLedger()
    assert (ledger.cost_usd, ledger.input_tokens, ledger.output_tokens, ledger.unpriced) == (0, 0, 0, 0)


async def test_ledger_sums_several_calls() -> None:
    fake = FakeOpenAI(completion(Answer(value=1)), completion(Answer(value=2)))
    ledger = LlmLedger()
    for _ in range(2):
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t", ledger=ledger)
    assert len(ledger.calls) == 2
    assert ledger.input_tokens == 2000
    assert ledger.output_tokens == 400
    assert ledger.cost_usd == 2 * estimate_cost(MODEL, 1000, 0, 200)  # type: ignore[operator]


async def test_refusal_raises_but_the_call_is_still_recorded() -> None:
    fake = FakeOpenAI(completion(None, refusal="I can't help with that"))
    ledger = LlmLedger()
    with pytest.raises(LlmError, match="refused"):
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t", ledger=ledger)
    assert len(ledger.calls) == 1  # tokens were spent


async def test_no_parsed_output_raises() -> None:
    fake = FakeOpenAI(completion(None))
    with pytest.raises(LlmError, match="no parsed output"):
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t")


async def test_api_error_message_keeps_status_and_code_only() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = openai.BadRequestError(
        f"bad request echoing {SECRET}",
        response=httpx2.Response(400, request=request),
        body={"code": "model_not_found", "message": SECRET},
    )
    fake = FakeOpenAI(error)
    with pytest.raises(LlmError) as info:
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="profile")
    assert str(info.value) == "OpenAI HTTP 400 (model_not_found) for profile"
    assert info.value.__cause__ is None


async def test_connection_error_becomes_llm_error() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    fake = FakeOpenAI(openai.APIConnectionError(request=request))
    with pytest.raises(LlmError, match="APIConnectionError"):
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="t")


async def test_logs_counts_never_prompt_or_output(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeOpenAI(completion(Answer(value=424242)))
    with caplog.at_level(logging.DEBUG):
        await parse(fake.client, model=MODEL, messages=_messages(), schema=Answer, purpose="profile")
    assert "llm profile model=gpt-5.4-mini in=1000 cached=0 out=200" in caplog.text
    assert SECRET not in caplog.text
    assert "424242" not in caplog.text
