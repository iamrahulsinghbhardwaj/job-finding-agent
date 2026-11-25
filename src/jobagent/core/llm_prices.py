"""OpenAI prices in USD per 1M tokens, used by core/llm.py to estimate the cost of each call.

Edit when OpenAI changes prices or a new model goes into .env (PROFILE_MODEL, RANK_MODEL).
A model missing here still works: its cost is None and a warning is logged, never a guess.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# When these prices were last checked against OpenAI's model pages (standard tier, no Batch
# discount). Update it whenever you touch PRICES.
PRICES_CHECKED = date(2026, 11, 25)


@dataclass(frozen=True)
class Price:
    input: Decimal
    cached_input: Decimal
    output: Decimal  # includes reasoning tokens, which OpenAI bills as output


PRICES: dict[str, Price] = {
    "gpt-5.4-mini": Price(input=Decimal("0.75"), cached_input=Decimal("0.075"), output=Decimal("4.50")),
    "gpt-5.4-nano": Price(input=Decimal("0.20"), cached_input=Decimal("0.02"), output=Decimal("1.25")),
}
