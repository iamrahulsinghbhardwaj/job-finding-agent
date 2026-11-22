from pathlib import Path

import pytest

from jobagent.core.settings import Settings

SETTING_ENV_NAMES = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear every setting env var and run from an empty dir so no real .env is read."""
    for name in SETTING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
