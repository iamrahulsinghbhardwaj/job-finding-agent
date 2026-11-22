"""Runtime settings, read from environment variables (and a local .env file).

Secrets are optional at load time so that tests and local tooling work without them;
code that needs a secret should call `require()` to fail loudly when it is missing.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gmail_token: SecretStr | None = None
    cv_blob_sas_url: SecretStr | None = None

    log_level: str = "INFO"

    def require(self, name: str) -> str:
        """Return the secret value of setting `name`, or raise if it is unset."""
        value = getattr(self, name)
        if value is None:
            raise RuntimeError(f"Missing required setting: {name.upper()}")
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
