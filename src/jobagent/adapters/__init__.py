"""Adapter registry: which JobSource implementation handles each platform."""

from collections.abc import Callable

from jobagent.adapters.greenhouse import GreenhouseSource
from jobagent.adapters.lever import LeverSource
from jobagent.core.contracts import JobSource
from jobagent.core.http import PoliteClient
from jobagent.core.models import Company, Platform, SourceConfig

SourceFactory = Callable[[Company, SourceConfig, PoliteClient], JobSource]

ADAPTERS: dict[Platform, SourceFactory] = {
    Platform.GREENHOUSE: GreenhouseSource,
    Platform.LEVER: LeverSource,
}


class UnsupportedPlatformError(LookupError):
    """No adapter has been written for this platform yet."""


def build_source(company: Company, source: SourceConfig, http: PoliteClient) -> JobSource:
    factory = ADAPTERS.get(source.platform)
    if factory is None:
        raise UnsupportedPlatformError(f"no adapter for platform {source.platform.value!r} ({company.slug})")
    return factory(company, source, http)
