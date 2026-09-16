import json
from dataclasses import replace

import pytest

from shop.config import Settings, initialize_config, load_settings
from shop.suppliers import SupplierNotConfigured, UnconfiguredSupplier


def test_config_init_unique_key_and_no_overwrite(tmp_path):
    path = tmp_path / "config.local.json"
    initialize_config(path)
    config = load_settings(path)
    assert not config.enable_sales
    assert config.environment == "test"
    assert config.stock_encryption_key
    config.validate(require_bot=False)
    with pytest.raises(FileExistsError):
        initialize_config(path)
    assert load_settings(path).stock_encryption_key == config.stock_encryption_key


def test_token_never_in_settings_repr(key):
    settings = Settings(bot_token="private-token", stock_encryption_key=key)
    assert "private-token" not in repr(settings)
    assert key not in repr(settings)


def test_production_requires_explicit_ack(key):
    settings = Settings(stock_encryption_key=key, environment="production")
    with pytest.raises(ValueError, match="Production"):
        settings.validate(require_bot=False)


def test_setup_requires_support_terms_privacy_and_admins(key):
    issues = Settings(stock_encryption_key=key).problems()
    assert any("support" in x for x in issues)
    assert any("terms_text" in x for x in issues)
    assert any("privacy_text" in x for x in issues)
    assert any("admin ID" in x for x in issues)


def test_terms_version_changes_with_privacy_or_terms():
    settings = Settings(terms_text="a", privacy_text="b")
    assert replace(settings, terms_text="c").terms_version != settings.terms_version
    assert replace(settings, privacy_text="c").terms_version != settings.terms_version


def test_string_boolean_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"enable_sales": "false"}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON boolean"):
        load_settings(path)


def test_supplier_adapter_fails_without_network():
    import asyncio

    with pytest.raises(SupplierNotConfigured):
        asyncio.run(UnconfiguredSupplier().fulfill("sku", idempotency_key="order"))
