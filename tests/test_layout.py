import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    ["core", "adapters", "pipeline", "agents", "digest", "db", "jobs"],
)
def test_subpackages_import(module: str) -> None:
    importlib.import_module(f"jobagent.{module}")
