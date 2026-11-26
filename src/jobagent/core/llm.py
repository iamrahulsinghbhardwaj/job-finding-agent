"""The one place that calls OpenAI: our callback layer (HLD §11, cost per run).

Every call goes through `parse()`, which asks for Structured Outputs into a Pydantic model and
records model, input / cached / output tokens, latency and estimated cost as an `LlmCall`. Calls
are added to an `LlmLedger` (one per run) and logged as counts only: prompts and outputs carry
CV and job text, so they never reach the (public) logs.
"""

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from jobagent.core.llm_prices import PRICES, PRICES_CHECKED, Price

log = logging.getLogger(__name__)

_PER_MILLION = Decimal(1_000_000)
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")  # "gpt-5.4-mini-2026-03-17" -> "gpt-5.4-mini"


class LlmError(Exception):
    """The call failed, was refused, or returned nothing usable. Message is safe to log.

    `call` is set when the request was answered (and billed) but unusable: refused, no output.
    """

    def __init__(self, message: str, call: "LlmCall | None" = None) -> None:
        super().__init__(message)
        self.call = call


@dataclass(frozen=True)
class LlmCall:
    purpose: str  # what the call was for: "profile", "rank", ...
    model: str
    input_tokens: int  # all prompt tokens, cached ones included
    cached_tokens: int
    output_tokens: int  # reasoning tokens included
    latency_ms: int
    cost_usd: Decimal | None  # None: model missing from llm_prices


@dataclass
class LlmLedger:
    """Every call of one run, for the run summary and Run.cost_usd."""

    calls: list[LlmCall] = field(default_factory=list)

    def record(self, call: LlmCall) -> None:
        self.calls.append(call)

    @property
    def cost_usd(self) -> Decimal:
        """Sum of known costs (calls with an unknown price count as 0; see `unpriced`)."""
        return sum((c.cost_usd for c in self.calls if c.cost_usd is not None), Decimal(0))

    @property
    def unpriced(self) -> int:
        return sum(c.cost_usd is None for c in self.calls)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def cached_tokens(self) -> int:
        return sum(c.cached_tokens for c in self.calls)

    @property
    def cached_ratio(self) -> float:
        """Share of input tokens served from OpenAI's prompt cache (0.0 with no calls)."""
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0.0

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)


def price_for(model: str) -> Price | None:
    return PRICES.get(model) or PRICES.get(_SNAPSHOT.sub("", model))


def estimate_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> Decimal | None:
    """USD, or None (with a warning) for a model missing from llm_prices: never a guess."""
    price = price_for(model)
    if price is None:
        log.warning(
            "no price for model %s in llm_prices (prices checked %s): cost unknown", model, PRICES_CHECKED
        )
        return None
    cached = min(cached_tokens, input_tokens)
    total = (input_tokens - cached) * price.input + cached * price.cached_input + output_tokens * price.output
    return total / _PER_MILLION


async def parse[T: BaseModel](
    client: AsyncOpenAI,
    *,
    model: str,
    messages: Sequence[ChatCompletionMessageParam],
    schema: type[T],
    purpose: str,
    ledger: LlmLedger | None = None,
    options: Mapping[str, Any] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[T, LlmCall]:
    """One Structured Outputs call: the parsed `schema` instance and the recorded call.

    `options` are passed through to the API (prompt_cache_key, reasoning_effort, max_completion_tokens).
    """
    started = clock()
    try:
        completion = await client.chat.completions.parse(
            model=model, messages=list(messages), response_format=schema, **dict(options or {})
        )
    except openai.APIStatusError as exc:
        # Status and error code only: OpenAI's message can quote the request.
        raise LlmError(f"OpenAI HTTP {exc.status_code} ({_code(exc)}) for {purpose}") from None
    except openai.OpenAIError as exc:
        raise LlmError(f"OpenAI {type(exc).__name__} for {purpose}") from None
    latency_ms = round((clock() - started) * 1000)

    usage = completion.usage
    input_tokens = usage.prompt_tokens if usage else 0
    details = usage.prompt_tokens_details if usage else None
    cached_tokens = (details.cached_tokens or 0) if details else 0
    output_tokens = usage.completion_tokens if usage else 0
    call = LlmCall(
        purpose=purpose,
        model=model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=estimate_cost(model, input_tokens, cached_tokens, output_tokens),
    )
    if ledger is not None:
        ledger.record(call)
    log.info(
        "llm %s model=%s in=%d cached=%d out=%d ms=%d cost=%s",
        purpose,
        model,
        input_tokens,
        cached_tokens,
        output_tokens,
        latency_ms,
        "unknown" if call.cost_usd is None else f"${call.cost_usd:.6f}",
    )

    message = completion.choices[0].message if completion.choices else None
    if message is None:
        raise LlmError(f"OpenAI returned no choices for {purpose}", call)
    if message.refusal:
        raise LlmError(f"model refused the {purpose} request", call)
    if message.parsed is None:
        raise LlmError(f"no parsed output for {purpose}", call)
    return message.parsed, call


def _code(exc: openai.APIStatusError) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else "no code"
