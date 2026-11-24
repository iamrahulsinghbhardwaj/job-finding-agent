# Reuse the adapter fixtures (companies + offline PoliteClient) to replay recorded responses.
from tests.adapters.conftest import amazon, http, openai, palantir, stripe

__all__ = ["amazon", "http", "openai", "palantir", "stripe"]
