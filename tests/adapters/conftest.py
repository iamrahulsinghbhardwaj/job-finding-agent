import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, Platform, SourceConfig, Tier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def stripe() -> Company:
    return Company(
        slug="stripe",
        name="Stripe",
        tier=Tier.T1,
        india_presence=False,
        europe_presence=True,
        us_transfer_path=True,
        sources=[SourceConfig(platform=Platform.GREENHOUSE, tenant="stripe")],
    )


@pytest.fixture
async def http() -> AsyncIterator[PoliteClient]:
    async def no_sleep(_: float) -> None:
        return None

    async with PoliteClient(min_delay=0, sleep=no_sleep) as client:
        yield client
