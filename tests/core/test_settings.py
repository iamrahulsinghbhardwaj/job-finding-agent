import pytest

from jobagent.core.settings import Settings

EXPECTED_NAMES = {
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "RANK_MODEL",
    "PROFILE_MODEL",
    "CV_BLOB_SAS_URL",
    "PROFILE_OVERRIDES_SAS_URL",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "DIGEST_TO",
}


def test_all_settings_default_to_none() -> None:
    settings = Settings()
    assert set(settings.present_settings()) == EXPECTED_NAMES
    assert not any(settings.present_settings().values())


def test_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("RANK_MODEL", "some-model")
    settings = Settings()
    assert settings.require("openai_api_key") == "sk-test"
    assert settings.require("rank_model") == "some-model"
    assert settings.present_settings()["OPENAI_API_KEY"] is True


def test_reads_from_dotenv_file() -> None:
    # isolated_env chdirs into an empty tmp dir, so this .env is the only one seen.
    with open(".env", "w") as f:
        f.write("GMAIL_CLIENT_ID=abc\n")
    assert Settings().require("gmail_client_id") == "abc"


def test_secrets_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@host/db")
    monkeypatch.setenv("DIGEST_TO", "me@example.com")
    settings = Settings()
    for text in (repr(settings), str(settings.model_dump())):
        assert "hunter2" not in text
        assert "me@example.com" not in text


def test_require_raises_when_missing() -> None:
    with pytest.raises(RuntimeError, match="GMAIL_REFRESH_TOKEN"):
        Settings().require("gmail_refresh_token")


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_value_counts_as_unset(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PROFILE_MODEL", value)
    settings = Settings()
    assert settings.profile_model is None
    assert settings.present_settings()["PROFILE_MODEL"] is False
    with pytest.raises(RuntimeError, match="PROFILE_MODEL"):
        settings.require("profile_model")


def test_empty_line_in_dotenv_counts_as_unset() -> None:
    # isolated_env chdirs into an empty tmp dir, so this .env is the only one seen.
    with open(".env", "w") as f:
        f.write("CV_BLOB_SAS_URL=\nRANK_MODEL=gpt-5.4-nano\n")
    settings = Settings()
    assert settings.cv_blob_sas_url is None
    assert settings.rank_model == "gpt-5.4-nano"
