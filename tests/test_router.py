"""Cheapest-wins supplier routing: selection, fallbacks, caps, dry runs."""
from decimal import Decimal

import pytest

from shop.config import CanbosoSettings
from shop.pricing import Pricer
from shop.router import Router
from shop.store import ShopError

FX = {"USD": Decimal("50"), "VND": Decimal("0.03")}


def supplier_product(store, sku="routed-key", price_stars=400, product_id="pid_a"):
    store.upsert_product(
        {
            "sku": sku,
            "title": "Routed product",
            "description": "Multi-supplier test product",
            "category": "Keys",
            "price_stars": price_stars,
            "is_demo": True,
            "source": "supplier",
            "supplier": {
                "provider": "canboso",
                "product_id": product_id,
                "product_type": "account",
                "currency": "USD",
                "max_cost": "10",
            },
        }
    )
    return sku


def snap(*entries):
    return {
        "products": [
            {
                "productId": pid,
                "productType": "account",
                "price": {"amount": amount, "currency": currency},
                "availability": {"available": stock, "sold": 0},
                "purchaseRequirements": {"quantityFixed": 1},
            }
            for pid, amount, currency, stock in entries
        ]
    }


@pytest.fixture
def router(store):
    store.supplier.configure(CanbosoSettings(enabled=True, api_key="TEST_ONLY_KEY"))
    return Router(store, FX)


def test_cheapest_candidate_becomes_active_mapping(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="canboso", product_id="pid_b",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5),
                                              ("pid_b", "6", "USD", 5))})
    winner = [d for d in decisions if d.sku == sku][0]
    assert winner.winner == "canboso:pid_b"
    assert winner.changed
    assert store.supplier.mapping(sku)["product_id"] == "pid_b"


def test_out_of_stock_falls_back_to_next_cheapest(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="canboso", product_id="pid_b",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5),
                                              ("pid_b", "6", "USD", 0))})
    assert decisions[0].winner == "canboso:pid_a"


def test_over_cap_candidate_is_skipped(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="canboso", product_id="pid_b",
                         product_type="account", currency="USD", max_cost="7")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5),
                                              ("pid_b", "9", "USD", 5))})
    # pid_b is over its own approved cap despite being "available"
    assert decisions[0].winner == "canboso:pid_a"


def test_no_winner_keeps_existing_mapping(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 0))})
    assert decisions[0].winner is None
    assert "out of stock" in decisions[0].reason
    assert store.supplier.mapping(sku)["product_id"] == "pid_a"


def test_cross_currency_comparison_uses_stars(store, router, monkeypatch):
    import shop.providers

    monkeypatch.setattr(shop.providers, "PROVIDERS", {"canboso", "testshop"})
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="testshop", product_id="pid_v",
                         product_type="account", currency="VND", max_cost="20000")
    # USD 8 = 400 Stars; VND 15000 * 0.03 = 450 Stars. USD wins.
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5)),
                              "testshop": snap(("pid_v", "15000", "VND", 5))})
    assert decisions[0].winner == "canboso:pid_a"
    assert store.supplier.mapping(sku)["product_id"] == "pid_a"
    # VND 12000 * 0.03 = 360 Stars. Now the VND candidate wins.
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5)),
                              "testshop": snap(("pid_v", "12000", "VND", 5))})
    assert decisions[0].winner == "testshop:pid_v"
    mapping = store.supplier.mapping(sku)
    assert mapping["provider"] == "testshop"
    assert mapping["currency"] == "VND"


def test_missing_fx_rejects_candidate(store):
    store.supplier.configure(CanbosoSettings(enabled=True, api_key="TEST_ONLY_KEY"))
    router = Router(store, {"USD": Decimal("50")})  # no VND rate
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    router.add_candidate(sku, provider="canboso", product_id="pid_v",
                         product_type="account", currency="VND", max_cost="20000")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5),
                                              ("pid_v", "100", "VND", 5))})
    assert decisions[0].winner == "canboso:pid_a"


def test_dry_run_changes_nothing(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_b",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": snap(("pid_b", "6", "USD", 5))}, dry_run=True)
    assert decisions[0].changed  # would switch
    assert store.supplier.mapping(sku)["product_id"] == "pid_a"  # but didn't


def test_remove_candidate(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_b",
                         product_type="account", currency="USD", max_cost="10")
    router.remove_candidate(sku, provider="canboso", product_id="pid_b")
    assert router.candidates(sku) == []
    with pytest.raises(ShopError):
        router.remove_candidate(sku, provider="canboso", product_id="pid_b")


def test_candidate_requires_supplier_product_and_registered_provider(store, router):
    with pytest.raises(ShopError):
        router.add_candidate("sample-key", provider="canboso", product_id="x",
                             product_type="account", currency="USD", max_cost="10")
    sku = supplier_product(store)
    with pytest.raises(ShopError):
        router.add_candidate(sku, provider="noshow", product_id="x",
                             product_type="account", currency="USD", max_cost="10")


def test_repricer_cap_never_exceeds_candidate_cap(store, router):
    sku = supplier_product(store, price_stars=400)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="9")
    pricer = Pricer(store, FX)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=50)
    pricer.reprice(snap(("pid_a", "8", "USD", 5)))
    # cost 8 * 1.5 jump = 12 derived, but the approved candidate cap is 9
    assert Decimal(store.supplier.mapping(sku)["max_cost"]) == Decimal("9.0000")


def test_same_winner_is_not_a_change(store, router):
    sku = supplier_product(store)
    router.add_candidate(sku, provider="canboso", product_id="pid_a",
                         product_type="account", currency="USD", max_cost="10")
    decisions = router.route({"canboso": snap(("pid_a", "8", "USD", 5))})
    assert decisions[0].winner == "canboso:pid_a"
    assert not decisions[0].changed
