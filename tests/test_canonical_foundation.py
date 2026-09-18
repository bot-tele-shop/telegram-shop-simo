import json
import logging

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from digital_shelf.api import create_app
from digital_shelf.config import Settings
from digital_shelf.logging import JsonFormatter


def production_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment": "production",
        "database_url": "postgresql+asyncpg://app:password@database.example/shop",
        "webhook_path_secret": "p" * 32,
        "webhook_header_secret": "h" * 32,
        "supabase_auth_issuer": "https://project.supabase.co/auth/v1",
        "inventory_encryption_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return values


def test_production_requires_both_strong_webhook_secrets() -> None:
    for missing in ("webhook_path_secret", "webhook_header_secret"):
        values = production_settings()
        values[missing] = None
        with pytest.raises(ValidationError, match=missing):
            Settings(**values)


def test_production_rejects_default_database_url() -> None:
    # Pass the field default explicitly so a SHOP_DATABASE_URL set in the
    # environment (CI does this) cannot leak in and satisfy the validator.
    with pytest.raises(ValidationError, match="non-default database URL"):
        Settings(
            environment="production",
            database_url=Settings.model_fields["database_url"].default,
            webhook_path_secret="p" * 32,
            webhook_header_secret="h" * 32,
            supabase_auth_issuer="https://project.supabase.co/auth/v1",
        )


def test_production_requires_https_supabase_auth_issuer() -> None:
    for issuer in (None, "http://project.supabase.co/auth/v1", "https://example.com/wrong"):
        values = production_settings(supabase_auth_issuer=issuer)
        with pytest.raises(ValidationError, match="supabase_auth_issuer"):
            Settings(**values)


def test_jwks_url_is_derived_from_validated_issuer() -> None:
    settings = Settings(**production_settings())

    assert settings.supabase_jwks_url == (
        "https://project.supabase.co/auth/v1/.well-known/jwks.json"
    )


def test_production_requires_valid_inventory_encryption_key() -> None:
    for key in (None, "not-a-fernet-key"):
        values = production_settings(inventory_encryption_key=key)
        with pytest.raises(ValidationError, match="inventory_encryption_key"):
            Settings(**values)


def test_settings_repr_does_not_expose_secrets() -> None:
    settings = Settings(**production_settings())
    rendered = repr(settings)
    assert "p" * 32 not in rendered
    assert "password@" not in rendered


def test_live_endpoint_does_not_require_database() -> None:
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_reports_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(_engine: object) -> bool:
        return False

    monkeypatch.setattr("digital_shelf.api.database_ready", unavailable)
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_structured_logs_redact_sensitive_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)
    record.fields = {
        "order_id": "order-1",
        "authorization": "Bearer private",
        "nested": {"stock_ciphertext": "encrypted-secret"},
    }
    payload = json.loads(JsonFormatter().format(record))
    assert payload["order_id"] == "order-1"
    assert payload["authorization"] == "[REDACTED]"
    assert payload["nested"]["stock_ciphertext"] == "[REDACTED]"
