"""Validated configuration for the canonical runtime."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_DATABASE_URL = (
    "postgresql+asyncpg://telegram_shop:telegram_shop@localhost:5432/telegram_shop"
)
_FORBIDDEN_SECRET_VALUES = {
    "change-me",
    "changeme",
    "placeholder",
    "secret",
    "test",
}


class Settings(BaseSettings):
    """Environment configuration loaded from ``SHOP_`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="SHOP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr = SecretStr(_DEVELOPMENT_DATABASE_URL)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    webhook_path_secret: SecretStr | None = None
    webhook_header_secret: SecretStr | None = None

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.environment != "production":
            return self

        if self.database_url.get_secret_value() == _DEVELOPMENT_DATABASE_URL:
            raise ValueError("production requires a non-default database URL")

        for name in ("webhook_path_secret", "webhook_header_secret"):
            secret = getattr(self, name)
            value = secret.get_secret_value().strip() if secret else ""
            if len(value) < 32 or value.lower() in _FORBIDDEN_SECRET_VALUES:
                raise ValueError(f"production requires a strong {name}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""

    return Settings()
