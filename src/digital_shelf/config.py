"""Validated configuration for the canonical runtime."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

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
    supabase_auth_issuer: str | None = None
    admin_jwt_audience: str = "authenticated"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.supabase_auth_issuer is not None:
            issuer = self.supabase_auth_issuer.rstrip("/")
            parsed = urlsplit(issuer)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.query
                or parsed.fragment
                or not parsed.path.endswith("/auth/v1")
            ):
                raise ValueError("supabase_auth_issuer must be an HTTPS Auth issuer URL")
            self.supabase_auth_issuer = issuer

        if not self.admin_jwt_audience.strip():
            raise ValueError("admin_jwt_audience must not be empty")

        if self.environment != "production":
            return self

        if self.database_url.get_secret_value() == _DEVELOPMENT_DATABASE_URL:
            raise ValueError("production requires a non-default database URL")

        if self.supabase_auth_issuer is None:
            raise ValueError("production requires supabase_auth_issuer")

        for name in ("webhook_path_secret", "webhook_header_secret"):
            secret = getattr(self, name)
            value = secret.get_secret_value().strip() if secret else ""
            if len(value) < 32 or value.lower() in _FORBIDDEN_SECRET_VALUES:
                raise ValueError(f"production requires a strong {name}")
        return self

    @property
    def supabase_jwks_url(self) -> str | None:
        if self.supabase_auth_issuer is None:
            return None
        return f"{self.supabase_auth_issuer}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""

    return Settings()
