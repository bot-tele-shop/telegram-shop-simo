import hashlib
import json
from dataclasses import replace

import pytest

from shop.config import CanbosoSettings, load_settings

KEY = "test-only-canboso-config-key"


def test_canboso_defaults_disable_spending():
    settings = CanbosoSettings()
    assert not settings.enabled
    assert not settings.allow_purchases
    assert not settings.resale_authorized
    assert not settings.acknowledge_price_race
    assert settings.problems("test") == []


def test_key_repr_is_private_and_fingerprint_stable():
    settings = CanbosoSettings(api_key=KEY)
    assert KEY not in repr(settings)
    assert settings.key_fingerprint == hashlib.sha256(KEY.encode()).hexdigest()
    assert replace(settings, api_key="different-test-key").key_fingerprint != settings.key_fingerprint


def test_production_acknowledgments_do_not_enable_test_spending():
    settings = CanbosoSettings(
        enabled=True, api_key=KEY, allow_purchases=True, resale_authorized=True,
        acknowledge_price_race=True, budget_currency="USD", spend_budget="12.50",
    )
    assert settings.problems("production") == []
    assert settings.problems("test")


@pytest.mark.parametrize("field", ["enabled", "allow_purchases", "resale_authorized", "acknowledge_price_race"])
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_canboso_boolean_config_is_strict(tmp_path, field, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"canboso": {field: value}}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON boolean"):
        load_settings(path)


@pytest.mark.parametrize("value", [None, [], "enabled", True, 1])
def test_canboso_config_requires_object(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"canboso": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="canboso must be a JSON object"):
        load_settings(path)


def test_environment_key_override_without_exposure(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"canboso": {"api_key": "local-test-key"}}), encoding="utf-8")
    monkeypatch.setenv("CANBOSO_API_KEY", KEY)
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    config = load_settings(path)
    assert config.canboso.api_key == KEY
    assert KEY not in repr(config)
    assert not config.canboso.allow_purchases


@pytest.mark.parametrize("key", [None, True, 123, "", "short", "has space in key", "key-with\nnewline", "x" * 513])
def test_invalid_api_keys_report_only_safe_guidance(key):
    settings = CanbosoSettings(enabled=True, api_key=key)
    issues = settings.problems("test")
    assert issues
    if isinstance(key, str) and len(key) >= 8:
        assert key not in "\n".join(issues)


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity", "-Infinity", "invalid", ""])
def test_live_spending_requires_finite_positive_budget(budget):
    settings = CanbosoSettings(
        enabled=True, api_key=KEY, allow_purchases=True, resale_authorized=True,
        acknowledge_price_race=True, budget_currency="VND", spend_budget=budget,
    )
    assert any("positive cumulative spend_budget" in issue for issue in settings.problems("production"))
