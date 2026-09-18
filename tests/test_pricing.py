"""Auto-pricing rules: markup math, floors, jump clamps, dry runs, events."""
from decimal import Decimal

import pytest

from shop.config import CanbosoSettings
from shop.pricing import Pricer
from shop.store import ShopError

FX = {"USD": Decimal("50")}  # 1 USD = 50 Stars


def supplier_product(store, sku="auto-key", price_stars=400, max_cost="10"):
    store.upsert_product(
        {
            "sku": sku,
            "title": "Supplier backed",
            "description": "Auto priced test product",
            "category": "Keys",
            "price_stars": price_stars,
            "is_demo": True,
            "source": "supplier",
            "supplier": {
                "provider": "canboso",
                "product_id": f"pid_{sku}",
                "product_type": "account",
                "currency": "USD",
                "max_cost": max_cost,
            },
        }
    )
    return sku


def snapshot(sku="auto-key", amount="8"):
    return {
        "products": [
            {
                "productId": f"pid_{sku}",
                "name": "Offline account",
                "productType": "account",
                "price": {"amount": amount, "currency": "USD", "text": f"USD {amount}"},
                "availability": {"available": 5, "sold": 0},
                "promotions": [],
                "purchaseRequirements": {"quantityFixed": 1},
            }
        ]
    }


@pytest.fixture
def pricer(store):
    store.supplier.configure(CanbosoSettings(enabled=True, api_key="TEST_ONLY_KEY"))
    return Pricer(store, FX)


def test_markup_applied_on_reprice(store, pricer):
    sku = supplier_product(store)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    changes = pricer.reprice(snapshot(amount="8"))
    assert len(changes) == 1
    # cost 8 USD * 50 Stars/USD = 400 cost stars; 400 * 1.5 = 600
    assert changes[0].new_price == 600
    assert store.get_product(sku)["price_stars"] == 600


def test_min_profit_floor_beats_raw_markup(store, pricer):
    sku = supplier_product(store)
    pricer.set_rule(sku, mode="auto", markup_pct=0, min_profit_stars=25, max_jump_pct=100)
    changes = pricer.reprice(snapshot(amount="8"))
    assert changes[0].new_price == 425  # 400 cost + 25 floor


def test_price_drop_lowers_sell_price(store, pricer):
    sku = supplier_product(store, price_stars=600)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    # 600 already equals the target, so no change event
    assert pricer.reprice(snapshot(amount="8")) == []
    changes = pricer.reprice(snapshot(amount="4"))
    assert changes[0].new_price == 300  # follows the supplier down


def test_jump_clamp_flags_and_limits_change(store, pricer):
    sku = supplier_product(store, price_stars=400)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=25)
    changes = pricer.reprice(snapshot(amount="20"))  # target would be 1500 (+275%)
    assert changes[0].flagged
    assert changes[0].new_price == 500  # clamped to +25%
    assert store.get_product(sku)["price_stars"] == 500
    events = pricer.events(sku)
    assert events[0]["flagged"] == 1


def test_max_cost_cap_tracks_new_cost(store, pricer):
    sku = supplier_product(store, max_cost="10")
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=25)
    pricer.reprice(snapshot(amount="8"))
    mapping = store.supplier.mapping(sku)
    assert Decimal(mapping["max_cost"]) == Decimal("10.0000")  # 8 * 1.25


def test_dry_run_changes_nothing(store, pricer):
    sku = supplier_product(store)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    changes = pricer.reprice(snapshot(amount="8"), dry_run=True)
    assert changes[0].new_price == 600
    assert store.get_product(sku)["price_stars"] == 400
    assert pricer.events(sku) == []


def test_manual_products_untouched(store, pricer):
    sku = supplier_product(store)
    pricer.set_rule(sku, mode="manual", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    assert pricer.reprice(snapshot(amount="8")) == []
    assert store.get_product(sku)["price_stars"] == 400


def test_auto_rule_requires_supplier_product(store, pricer):
    with pytest.raises(ShopError):
        pricer.set_rule("sample-key", mode="auto", markup_pct=50,
                        min_profit_stars=0, max_jump_pct=25)


def test_auto_rule_requires_fx(store):
    store.supplier.configure(CanbosoSettings(enabled=True, api_key="TEST_ONLY_KEY"))
    sku = supplier_product(store)
    with pytest.raises(ShopError):
        Pricer(store, {}).set_rule(sku, mode="auto", markup_pct=50,
                                   min_profit_stars=0, max_jump_pct=25)


def test_unknown_sku_rejected(pricer):
    with pytest.raises(ShopError):
        pricer.set_rule("nope", mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=25)


def test_bad_supplier_row_does_not_stop_others(store, pricer):
    good = supplier_product(store, "good-key")
    supplier_product(store, "bad-key")
    pricer.set_rule(good, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    pricer.set_rule("bad-key", mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    snap = snapshot("good-key", "8")
    snap["products"].append(
        {"productId": "pid_bad-key", "price": {"amount": "oops", "currency": "USD"}}
    )
    changes = pricer.reprice(snap)
    assert [c.sku for c in changes] == ["good-key"]


def test_supplier_product_missing_from_snapshot_is_skipped(store, pricer):
    sku = supplier_product(store)
    pricer.set_rule(sku, mode="auto", markup_pct=50, min_profit_stars=0, max_jump_pct=100)
    assert pricer.reprice({"products": []}) == []
    assert store.get_product(sku)["price_stars"] == 400
