"""Runtime settings, read from environment variables (and a local .env file).

Everything is optional at load time so that tests and local tooling work without secrets;
code that needs a value should call `require()` to fail loudly when it is missing.
Secret-like values are `SecretStr` so they never appear in reprs or logs.
"""

from functools import lru_cache
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage
    database_url: SecretStr | None = None

    # LLM
    openai_api_key: SecretStr | None = None
    rank_model: str | None = None
    profile_model: str | None = None

    # CV
    cv_blob_sas_url: SecretStr | None = None
    profile_overrides_sas_url: SecretStr | None = None  # optional; private YAML next to the CV

    # Gmail (OAuth installed-app flow; refresh token minted once, locally)
    gmail_client_id: SecretStr | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_refresh_token: SecretStr | None = None
    digest_to: SecretStr | None = None  # personal address: keep out of public logs

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: Any) -> Any:
        """An empty `KEY=` line in .env means "not set", not an empty string."""
        return None if isinstance(value, str) and not value.strip() else value

    def present_settings(self) -> dict[str, bool]:
        """Map each setting's env var name to whether it is set. Never returns values."""
        return {name.upper(): getattr(self, name) is not None for name in type(self).model_fields}

    def require(self, name: str) -> str:
        """Return the value of setting `name`, or raise if it is unset."""
        value = getattr(self, name)
        if value is None:
            raise RuntimeError(f"Missing required setting: {name.upper()}")
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
