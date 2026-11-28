"""scripts/gmail_auth.py without a browser or network: config, .env writing, missing settings."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gmail_auth.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gmail_auth", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_set_env_value_replaces_the_existing_line() -> None:
    script = load()
    text = "DATABASE_URL=x\nGMAIL_REFRESH_TOKEN=\nDIGEST_TO=y\n"
    assert script.set_env_value(text, "GMAIL_REFRESH_TOKEN", "1//new") == (
        "DATABASE_URL=x\nGMAIL_REFRESH_TOKEN=1//new\nDIGEST_TO=y\n"
    )


def test_set_env_value_appends_when_absent() -> None:
    script = load()
    assert script.set_env_value("A=1", "GMAIL_REFRESH_TOKEN", "t") == "A=1\nGMAIL_REFRESH_TOKEN=t\n"
    assert script.set_env_value("", "GMAIL_REFRESH_TOKEN", "t") == "GMAIL_REFRESH_TOKEN=t\n"


def test_set_env_value_keeps_backslashes_literally() -> None:
    script = load()
    assert script.set_env_value("K=old\n", "K", r"a\1b") == "K=a\\1b\n"


def test_client_config_is_an_installed_app(monkeypatch: pytest.MonkeyPatch) -> None:
    script = load()
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csecret")
    from jobagent.core.settings import Settings

    config = script.client_config(Settings())
    assert config["installed"]["client_id"] == "cid"
    assert config["installed"]["redirect_uris"] == ["http://localhost"]


def test_missing_client_settings_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    script = load()
    assert script.main([]) == 2
    assert "GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET" in capsys.readouterr().err


def test_script_asks_for_send_and_readonly_only() -> None:
    script = load()
    assert set(script.SCOPES) == {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
    assert "gmail.compose" not in SCRIPT.read_text()
