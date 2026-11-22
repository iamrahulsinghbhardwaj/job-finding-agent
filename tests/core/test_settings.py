import pytest

from jobagent.core.settings import Settings


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "GMAIL_TOKEN", "CV_BLOB_SAS_URL"):
        monkeypatch.delenv(name, raising=False)


def test_secrets_default_to_none() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.database_url is None
    assert settings.openai_api_key is None


def test_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.require("openai_api_key") == "sk-test"


def test_secret_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@host/db")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_require_raises_when_missing() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    with pytest.raises(RuntimeError, match="GMAIL_TOKEN"):
        settings.require("gmail_token")
