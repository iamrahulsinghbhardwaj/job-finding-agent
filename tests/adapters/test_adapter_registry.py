import pytest

from jobagent.adapters import ADAPTERS, UnsupportedPlatformError, build_source
from jobagent.adapters.amazon import AmazonSource
from jobagent.adapters.ashby import AshbySource
from jobagent.adapters.greenhouse import GreenhouseSource
from jobagent.adapters.lever import LeverSource
from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, Platform, SourceConfig


def test_greenhouse_registered(stripe: Company, http: PoliteClient) -> None:
    assert ADAPTERS[Platform.GREENHOUSE] is GreenhouseSource
    assert isinstance(build_source(stripe, stripe.sources[0], http), GreenhouseSource)


def test_unsupported_platform_raises(stripe: Company, http: PoliteClient) -> None:
    eightfold = SourceConfig(platform=Platform.EIGHTFOLD, tenant="microsoft.com")
    with pytest.raises(UnsupportedPlatformError, match="eightfold"):
        build_source(stripe, eightfold, http)


def test_lever_registered(palantir: Company, http: PoliteClient) -> None:
    assert ADAPTERS[Platform.LEVER] is LeverSource
    assert isinstance(build_source(palantir, palantir.sources[0], http), LeverSource)


def test_ashby_registered(openai: Company, http: PoliteClient) -> None:
    assert ADAPTERS[Platform.ASHBY] is AshbySource
    assert isinstance(build_source(openai, openai.sources[0], http), AshbySource)


def test_amazon_registered(amazon: Company, http: PoliteClient) -> None:
    assert ADAPTERS[Platform.AMAZON] is AmazonSource
    assert isinstance(build_source(amazon, amazon.sources[0], http), AmazonSource)
