"""Multi-provider registry, per-provider state isolation, and safe fallbacks."""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shop import providers
from shop.canboso import CanbosoError
from shop.config import SupplierSettings, load_settings
from shop.delivery import DeliveryWorker
from shop.errors import ShopError
from shop.pricing import Pricer
from shop.router import Router
from shop.store import Store
from shop.supplier_worker import SupplierWorker

FX = {"USD": Decimal("50")}

CANBOSO = SupplierSettings(
    provider="canboso", enabled=True, api_key="TEST_ONLY_CANBOSO_KEY",
    allow_purchases=True, resale_authorized=True, acknowledge_price_race=True,
    budget_currency="USD", spend_budget="100",
)
JAHA = SupplierSettings(
    provider="jaha_digital", enabled=True, api_key="TEST_ONLY_JAHA_KEY",
    allow_purchases=True, resale_authorized=True, acknowledge_price_race=True,
    budget_currency="USD", spend_budget="100",
)


def catalog(product_id, amount="8"):
    return {
        "products": [{
            "productId": product_id,
            "name": "Offline multi-provider item",
            "productType": "account",
            "price": {"amount": amount, "currency": "USD", "text": f"USD {amount}"},
            "availability": {"available": 10, "sold": 0},
            "promotions": [],
            "purchaseRequirements": {"quantityFixed": 1},
        }],
        "walletCurrency": "USD",
    }


def wallet(balance=100):
    return {"success": True, "walletCurrency": "USD", "balance": balance,
            "balanceText": f"USD {balance}"}


class FakeClient:
    def __init__(self, products=None, balance=None, error=None):
        self._products = products or catalog("unused")
        self._balance = balance or wallet()
        self.error = error
        self.calls = []

    async def products(self):
        self.calls.append("products")
        if self.error:
            raise self.error
        return self._products

    async def balance(self):
        self.calls.append("balance")
        if self.error:
            raise self.error
        return self._balance


@pytest.fixture
def multi_store(tmp_path, key, clock):
    store = Store(tmp_path / "multi.sqlite3", key, "production", lambda: clock[0])
    store.initialize()
    store.supplier.configure(CANBOSO)
    store.supplier.configure(JAHA)
    return store


def supplier_product(store, sku, provider, product_id, max_cost="10"):
    store.upsert_product({
        "sku": sku,
        "title": f"Multi {sku}",
        "description": "Multi-provider test product",
        "category": "Keys",
        "price_stars": 400,
        "source": "supplier",
        "active": True,
        "is_demo": False,
        "supplier": {
            "provider": provider,
            "product_id": product_id,
            "product_type": "account",
            "currency": "USD",
            "max_cost": max_cost,
        },
    })
    return sku


def test_registry_lists_canboso_and_the_new_suppliers():
    for name in ("canboso", "jaha_digital", "elite_emporium", "acczone"):
        assert providers.registered(name)
        assert providers.entry(name) is not None
    assert not providers.registered("noshow")
    assert providers.display("jaha_digital") == "Jaha Digital"
    assert providers.entry("canboso").documented
    assert not providers.entry("acczone").documented


def test_new_provider_config_sections_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "jaha_digital": {"enabled": True, "api_key": "local-jaha-key-123"},
        "acczone": {"enabled": True, "api_key": "local-acczone-key-123"},
    }), encoding="utf-8")
    monkeypatch.setenv("ACCZONE_API_KEY", "env-acczone-key-override")
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    settings = load_settings(path)
    assert settings.other_suppliers["jaha_digital"].enabled
    assert settings.other_suppliers["jaha_digital"].api_key == "local-jaha-key-123"
    assert settings.other_suppliers["acczone"].api_key == "env-acczone-key-override"
    assert settings.all_supplier_settings()["canboso"].enabled is False
    assert "local-jaha-key-123" not in repr(settings)


def test_new_provider_config_is_strict(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"jaha_digital": {"enabled": "yes"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="jaha_digital.enabled must be a JSON boolean"):
        load_settings(path)
    path.write_text(json.dumps({"elite_emporium": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="elite_emporium must be a JSON object"):
        load_settings(path)


def test_provider_problems_use_provider_identity():
    settings = SupplierSettings(provider="jaha_digital", enabled=True, api_key="bad")
    issues = settings.problems("test")
    assert any("JAHA_DIGITAL_API_KEY" in issue for issue in issues)
    spending = SupplierSettings(
        provider="elite_emporium", enabled=True, api_key="x" * 16, allow_purchases=True,
    )
    text = "\n".join(spending.problems("production"))
    assert "Elite Digital Emporium" in text


def test_mapping_accepts_new_providers_and_rejects_unknown(multi_store):
    supplier_product(multi_store, "jaha-item", "jaha_digital", "jaha_pid_1")
    supplier_product(multi_store, "acc-item", "acczone", "acc_pid_1")
    assert multi_store.supplier.mapping("jaha-item")["provider"] == "jaha_digital"
    with pytest.raises(ShopError):
        supplier_product(multi_store, "bad-item", "noshow", "x")
    with pytest.raises(ShopError, match="slot_months"):
        multi_store.upsert_product({
            "sku": "bad-months", "title": "Bad months", "description": "x",
            "category": "Keys", "price_stars": 10, "source": "supplier",
            "active": True, "is_demo": False,
            "supplier": {"provider": "acczone", "product_id": "acc_pid_2",
                         "product_type": "slot", "currency": "USD", "max_cost": "5",
                         "slot_months": 3},
        })


def test_cooldowns_are_provider_scoped(multi_store, clock):
    multi_store.supplier.defer_network(60, "jaha_digital")
    assert multi_store.supplier.cooldown_until("jaha_digital") == clock[0] + 60
    assert multi_store.supplier.cooldown_until("canboso") == 0
    assert multi_store.supplier.cooldown_until() == 0  # default stays Canboso


def test_cache_snapshots_are_provider_scoped(multi_store):
    multi_store.supplier.cache_snapshot("canboso", catalog("cb_pid"), wallet())
    multi_store.supplier.cache_snapshot("jaha_digital", catalog("jaha_pid"), wallet(55))
    canboso_snapshot, _, _ = multi_store.supplier.cached_products("canboso")
    jaha_snapshot, _, _ = multi_store.supplier.cached_products("jaha_digital")
    assert canboso_snapshot["products"][0]["productId"] == "cb_pid"
    assert jaha_snapshot["products"][0]["productId"] == "jaha_pid"
    # Rotating one provider's key invalidates only its own snapshot: the cached
    # fingerprint no longer matches the active Jaha key, Canboso's still does.
    multi_store.supplier.configure(SupplierSettings(
        provider="jaha_digital", enabled=True, api_key="TEST_ONLY_ROTATED_JAHA_KEY",
    ))
    _, _, jaha_hash = multi_store.supplier.cached_products("jaha_digital")
    assert jaha_hash != multi_store.supplier.settings_for("jaha_digital").key_fingerprint
    _, _, canboso_hash = multi_store.supplier.cached_products("canboso")
    assert canboso_hash == multi_store.supplier.settings_for("canboso").key_fingerprint


def test_checkout_uses_only_the_owning_providers_snapshot(multi_store):
    supplier_product(multi_store, "jaha-item", "jaha_digital", "jaha_pid_1")
    multi_store.supplier.cache_snapshot("canboso", catalog("jaha_pid_1"), wallet())
    multi_store.accept_terms(101, "terms-v1")
    # Canboso has the product id cached, Jaha does not: the order must fail.
    with pytest.raises(ShopError, match="not been synchronized"):
        multi_store.create_order(101, "jaha-item", "terms-v1")
    # With Jaha's own snapshot, preflight passes and only the missing
    # documented client stops the purchase (no order, no intent).
    multi_store.supplier.cache_snapshot("jaha_digital", catalog("jaha_pid_1"), wallet())
    with pytest.raises(ShopError, match="no documented buyer API client"):
        multi_store.create_order(101, "jaha-item", "terms-v1")
    assert multi_store.user_orders(101) == []


def test_undocumented_provider_never_builds_a_purchase_request(multi_store):
    supplier_product(multi_store, "acc-item", "acczone", "acc_pid_1")
    multi_store.supplier.configure(SupplierSettings(
        provider="acczone", enabled=True, api_key="TEST_ONLY_ACCZONE_KEY",
        allow_purchases=True, resale_authorized=True, acknowledge_price_race=True,
        budget_currency="USD", spend_budget="100",
    ))
    multi_store.supplier.cache_snapshot("acczone", catalog("acc_pid_1"), wallet())
    multi_store.accept_terms(101, "terms-v1")
    # Preflight passes on the cached snapshot, but there is no documented
    # request to build: the order rolls back instead of guessing an API call.
    with pytest.raises(ShopError, match="no documented buyer API client"):
        multi_store.create_order(101, "acc-item", "terms-v1")
    assert multi_store.user_orders(101) == []
    with multi_store.connection() as db:
        assert db.execute("SELECT count(*) FROM supplier_intents").fetchone()[0] == 0


def test_existing_databases_gain_the_provider_column(tmp_path, key, clock):
    path = tmp_path / "legacy.sqlite3"
    store = Store(path, key, "production", lambda: clock[0])
    store.initialize()
    with store.transaction() as db:
        db.execute("ALTER TABLE supplier_intents DROP COLUMN provider")
    legacy = Store(path, key, "production", lambda: clock[0])
    legacy.initialize()
    with legacy.connection() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(supplier_intents)")}
    assert "provider" in columns
    legacy.initialize()  # Migration is idempotent.


def test_worker_synchronizes_each_enabled_provider(multi_store):
    canboso_client = FakeClient(products=catalog("cb_pid"))
    jaha_client = FakeClient(products=catalog("jaha_pid"))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    delivery = DeliveryWorker(multi_store, bot, frozenset({999}))
    worker = SupplierWorker(
        multi_store, {"canboso": canboso_client, "jaha_digital": jaha_client}, delivery,
    )
    assert asyncio.run(worker.synchronize()) is True
    assert canboso_client.calls == ["products", "balance"]
    assert jaha_client.calls == ["products", "balance"]
    with multi_store.connection() as db:
        names = {row["name"] for row in db.execute("SELECT name FROM supplier_cache")}
    assert names == {"canboso:products", "canboso:balance",
                     "jaha_digital:products", "jaha_digital:balance"}


def test_worker_provider_failure_pauses_only_that_provider(multi_store, clock):
    canboso_client = FakeClient()
    jaha_client = FakeClient(error=CanbosoError("supplier_rate_limited", retry_after=30))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    delivery = DeliveryWorker(multi_store, bot, frozenset({999}))
    worker = SupplierWorker(
        multi_store, {"canboso": canboso_client, "jaha_digital": jaha_client}, delivery,
    )
    assert asyncio.run(worker.synchronize()) is False
    assert multi_store.supplier.cooldown_until("jaha_digital") == clock[0] + 30
    assert multi_store.supplier.cooldown_until("canboso") == 0


def test_pricing_uses_the_mappings_own_provider_snapshot(multi_store):
    multi_store.supplier.cache_snapshot("canboso", catalog("cb_pid"), wallet())
    supplier_product(multi_store, "cb-item", "canboso", "cb_pid")
    supplier_product(multi_store, "jaha-item", "jaha_digital", "jaha_pid")
    pricer = Pricer(multi_store, FX)
    for sku in ("cb-item", "jaha-item"):
        pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    changes = pricer.reprice({"jaha_digital": catalog("jaha_pid", amount="4")})
    assert [c.sku for c in changes] == ["jaha-item"]
    assert multi_store.get_product("jaha-item")["price_stars"] == 300  # 4*50*1.5
    assert multi_store.get_product("cb-item")["price_stars"] == 400
    changes = pricer.reprice(catalog("cb_pid", amount="2"))  # bare snapshot = Canboso
    assert [c.sku for c in changes] == ["cb-item"]
    assert multi_store.get_product("cb-item")["price_stars"] == 150


def test_router_compares_real_registered_providers(multi_store):
    sku = supplier_product(multi_store, "routed-item", "canboso", "cb_pid")
    router = Router(multi_store, FX)
    router.add_candidate(sku, provider="canboso", product_id="cb_pid",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="jaha_digital", product_id="jaha_pid",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": catalog("cb_pid", amount="8"),
                              "jaha_digital": catalog("jaha_pid", amount="6")})
    assert decisions[0].winner == "jaha_digital:jaha_pid"
    assert multi_store.supplier.mapping(sku)["provider"] == "jaha_digital"
