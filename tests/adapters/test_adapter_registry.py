import pytest

from jobagent.adapters import ADAPTERS, UnsupportedPlatformError, build_source
from jobagent.adapters.greenhouse import GreenhouseSource
from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, Platform, SourceConfig


def test_greenhouse_registered(stripe: Company, http: PoliteClient) -> None:
    assert ADAPTERS[Platform.GREENHOUSE] is GreenhouseSource
    assert isinstance(build_source(stripe, stripe.sources[0], http), GreenhouseSource)


def test_unsupported_platform_raises(stripe: Company, http: PoliteClient) -> None:
    eightfold = SourceConfig(platform=Platform.EIGHTFOLD, tenant="microsoft.com")
    with pytest.raises(UnsupportedPlatformError, match="eightfold"):
        build_source(stripe, eightfold, http)
