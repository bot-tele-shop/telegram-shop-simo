from concurrent.futures import ThreadPoolExecutor

import pytest

from shop.store import ShopError, Store


def pay(store, order, charge="charge-1"):
    return store.record_payment(order["id"], order["user_id"], "XTR", 25, charge)


def test_requires_terms(store):
    with pytest.raises(ShopError, match="accept"):
        store.create_order(102, "sample-key", "terms-v1")


def test_invoice_taps_reuse_order_without_reserving(store, order):
    assert store.create_order(101, "sample-key", "terms-v1")["id"] == order["id"]
    assert store.stats()["stock"] == {"available": 2}


@pytest.mark.parametrize(
    "currency,amount,user",
    [("USD", 25, 101), ("XTR", 1, 101), ("XTR", 25, 102), ("XTR", True, 101)],
)
def test_checkout_validates_customer_amount_currency(store, order, currency, amount, user):
    with pytest.raises(ShopError):
        store.approve_checkout(order["id"], user, currency, amount, "q", "terms-v1")
    assert store.stats()["stock"] == {"available": 2}


def test_terms_change_rejects_old_invoice(store, order):
    with pytest.raises(ShopError, match="terms changed"):
        store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v2")


def test_checkout_does_not_deliver(store, order):
    store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")
    assert store.claim_delivery(order["id"]) is None
    assert store.stats()["stock"] == {"available": 1, "reserved": 1}


def test_precheckout_idempotent_for_same_query(store, order):
    for _ in range(2):
        store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")
    assert store.stats()["stock"]["reserved"] == 1
    with pytest.raises(ShopError):
        store.approve_checkout(order["id"], 101, "XTR", 25, "different-q", "terms-v1")


def test_unpaid_reservation_expires(store, order, clock):
    store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")
    clock[0] += 901
    store.expire_orders()
    assert store.get_order(order["id"])["state"] == "expired"
    assert store.stats()["stock"]["available"] == 2


def test_expired_invoice_cannot_checkout(store, order, clock):
    clock[0] += 601
    with pytest.raises(ShopError):
        store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")


def test_concurrent_checkout_cannot_oversell(store):
    orders = []
    for user in (101, 102, 103):
        store.accept_terms(user, "terms-v1")
        orders.append(store.create_order(user, "sample-key", "terms-v1"))

    def checkout(order):
        try:
            store.approve_checkout(
                order["id"], order["user_id"], "XTR", 25, "q-" + order["id"], "terms-v1"
            )
            return True
        except ShopError:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(checkout, orders))
    assert sum(outcomes) == 2
    assert store.stats()["stock"] == {"reserved": 2}


def test_confirmed_payment_is_durable_and_duplicate_safe(store, paid, key, clock):
    duplicate = pay(store, paid)
    assert duplicate.duplicate
    reopened = Store(store.path, key, clock=lambda: clock[0])
    reopened.initialize()
    assert reopened.get_order(paid["id"])["state"] == "paid"
    assert reopened.stats()["stock"] == {"available": 1, "sold": 1}


def test_concurrent_duplicate_payment_allocates_once(store, order):
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(lambda _: pay(store, order), range(4)))
    assert sum(not e.duplicate for e in events) == 1
    assert store.stats()["stock"]["sold"] == 1


def test_extra_charge_saved_for_refund_not_delivered_twice(store, paid):
    event = pay(store, paid, "charge-extra")
    assert event.status == "review" and event.order_id is None
    assert store.get_payment(event.event_id)["reason"] == "extra_charge_for_order"
    assert store.stats()["stock"]["sold"] == 1


@pytest.mark.parametrize(
    "user,currency,amount", [(102, "XTR", 25), (101, "USD", 25), (101, "XTR", 2)]
)
def test_mismatched_success_is_recorded_for_review(store, order, user, currency, amount):
    event = store.record_payment(order["id"], user, currency, amount, "bad-charge")
    assert event.status == "review"
    assert store.get_payment(event.event_id)["amount"] == amount
    assert store.claim_delivery(order["id"]) is None
    assert store.stats()["stock"] == {"available": 2}


def test_unknown_success_is_not_lost(store):
    event = store.record_payment("unknown-order", 101, "XTR", 25, "unknown-charge")
    assert store.get_payment(event.event_id)["reason"] == "unknown_order"


def test_conflicting_duplicate_is_rejected(store, paid):
    with pytest.raises(ShopError, match="Conflicting"):
        store.record_payment(paid["id"], 102, "XTR", 25, "charge-1")


def test_late_success_can_fulfill_remaining_stock(store, order, clock):
    store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")
    clock[0] += 901
    store.expire_orders()
    pay(store, order)
    assert store.get_order(order["id"])["state"] == "paid"


def test_late_payment_without_stock_needs_refund(store, order, clock):
    clock[0] += 601
    store.expire_orders()
    for user in (102, 103):
        store.accept_terms(user, "terms-v1")
        other = store.create_order(user, "sample-key", "terms-v1")
        pay(store, other, f"charge-{user}")
    event = pay(store, order)
    assert event.status == "accepted"
    assert store.get_order(order["id"])["state"] == "needs_refund"
    assert len(store.review_payments()) == 1
    with pytest.raises(ShopError, match="Still out of stock"):
        store.retry_order(order["id"])
    store.import_stock("sample-key", ["LICENSE-003"])
    store.retry_order(order["id"])
    assert store.get_order(order["id"])["state"] == "paid"


def test_delivery_lease_and_retry_reuse_same_item(store, paid, clock):
    first = store.claim_delivery(paid["id"])
    assert store.claim_delivery(paid["id"]) is None
    clock[0] += 181
    second = store.claim_delivery(paid["id"])
    assert second.payload == first.payload
    assert second.lease_token != first.lease_token
    assert not store.finish_delivery(first, 10)
    assert store.finish_delivery(second, 11)
    assert store.claim_delivery(paid["id"]) is None
    assert store.stats()["stock"]["sold"] == 1


def test_delivery_failures_back_off_then_stop_for_review(store, paid, clock):
    values = set()
    for _ in range(8):
        delivery = store.claim_delivery(paid["id"])
        values.add(delivery.payload)
        store.fail_delivery(delivery, "TelegramNetworkError")
        clock[0] += 3601
    assert len(values) == 1
    assert store.get_order(paid["id"])["state"] == "delivery_failed"
    store.retry_order(paid["id"])
    assert store.claim_delivery(paid["id"]).payload in values


def test_only_buyer_can_retrieve_delivered_item(store, paid):
    delivery = store.claim_delivery(paid["id"])
    store.finish_delivery(delivery, 10)
    assert store.retrieve_delivery(paid["id"], 101) == "LICENSE-001"
    with pytest.raises(ShopError):
        store.retrieve_delivery(paid["id"], 102)
    with pytest.raises(ShopError):
        store.get_order(paid["id"], user_id=102)


def test_inventory_is_encrypted_and_duplicates_skipped(store):
    added, skipped = store.import_stock("sample-key", ["LICENSE-001", "LICENSE-003"])
    assert (added, skipped) == (1, 1)
    with store.connection() as db:
        ciphertext = db.execute("SELECT ciphertext FROM stock LIMIT 1").fetchone()[0]
    assert "LICENSE" not in ciphertext
    assert b"LICENSE-001" not in store.path.read_bytes()


def test_wrong_key_and_environment_fail_closed(store, key):
    from cryptography.fernet import Fernet

    with pytest.raises(ShopError, match="Wrong stock encryption key"):
        Store(store.path, Fernet.generate_key().decode()).initialize()
    with pytest.raises(ShopError, match="separate databases"):
        Store(store.path, key, "production").initialize()


def test_supplier_products_cannot_be_sold(store):
    store.upsert_product(
        {
            "sku": "api-product",
            "title": "Supplier item",
            "description": "Needs real API",
            "price_stars": 25,
            "source": "supplier",
        }
    )
    with pytest.raises(ShopError, match="not connected"):
        store.create_order(101, "api-product", "terms-v1")


@pytest.mark.parametrize("price", [0, -1, 1.5, True, "25"])
def test_invalid_prices_rejected(store, price):
    with pytest.raises(ShopError):
        store.upsert_product(
            {"sku": "bad-price", "title": "Test", "description": "Test", "price_stars": price}
        )


def test_production_rejects_demo_products(tmp_path, key):
    store = Store(tmp_path / "prod.sqlite3", key, "production")
    store.initialize()
    with pytest.raises(ShopError, match="Demo"):
        store.upsert_product(
            {
                "sku": "demo",
                "title": "Test",
                "description": "Test",
                "price_stars": 1,
                "is_demo": True,
            }
        )


def test_price_snapshot_survives_catalog_change(store, order):
    p = store.get_product("sample-key")
    p.update(price_stars=99, active=True, is_demo=True)
    store.upsert_product(p)
    store.approve_checkout(order["id"], 101, "XTR", 25, "q", "terms-v1")
    assert store.get_order(order["id"])["price_stars"] == 25


def test_refund_holds_delivery_and_quarantines_item(store, paid):
    event = store.payment_for_order(paid["id"])
    store.begin_refund(event["id"])
    assert store.claim_delivery(paid["id"]) is None
    store.finish_refund(event["id"])
    store.finish_refund(event["id"])
    assert store.get_order(paid["id"])["state"] == "refunded"
    assert store.stats()["stock"] == {"available": 1, "quarantined": 1}
    with pytest.raises(ShopError):
        store.retrieve_delivery(paid["id"], 101)


def test_refund_blocked_while_delivery_inflight(store, paid):
    store.claim_delivery(paid["id"])
    with pytest.raises(ShopError, match="in flight"):
        store.begin_refund(store.payment_for_order(paid["id"])["id"])


def test_duplicate_extra_charge_refund_does_not_refund_original(store, paid):
    event = pay(store, paid, "extra-charge")
    store.begin_refund(event.event_id)
    store.finish_refund(event.event_id)
    assert store.get_order(paid["id"])["state"] == "paid"


def test_out_of_order_refund_prevents_delivery(store, order):
    store.record_refund("charge-1", 101, "XTR", 25)
    event = pay(store, order)
    assert event.status == "refunded"
    assert store.claim_delivery(order["id"]) is None


def test_refund_update_validates_payment(store, paid):
    with pytest.raises(ShopError):
        store.record_refund("charge-1", 102, "XTR", 25)
    store.record_refund("charge-1", 101, "XTR", 25)
    assert store.get_order(paid["id"])["state"] == "refunded"


def test_bot_identity_is_bound_to_database(store):
    store.bind_bot(55)
    store.bind_bot(55)
    with pytest.raises(ShopError, match="another bot"):
        store.bind_bot(56)


def test_inbox_is_durable_before_offset_advances(store, key, clock):
    body = '{"update_id":44,"private":"NEVER-IN-PLAINTEXT"}'
    store.save_updates([(44, "payment", body)])
    assert store.polling_offset() == 45
    reopened = Store(store.path, key, clock=lambda: clock[0])
    assert reopened.claim_update("ui") is None
    assert reopened.claim_update("payment") == (44, body)
    with store.connection() as db:
        encrypted = db.execute("SELECT ciphertext FROM update_inbox").fetchone()[0]
    assert "NEVER-IN-PLAINTEXT" not in encrypted
    store.finish_update(44)
    assert store.claim_update("payment") is None
    store.save_updates([(44, "payment", body)])
    assert store.claim_update("payment") is None


def test_payment_update_retries_never_silently_discard(store, clock):
    store.save_updates([(45, "payment", "{}")])
    for _ in range(12):
        assert store.claim_update("payment") == (45, "{}")
        store.retry_update(45, "TemporaryDatabaseError")
        clock[0] += 65
    assert store.claim_update("payment") == (45, "{}")


def test_inbox_stale_claim_recovered_after_crash(store, clock):
    store.save_updates([(46, "payment", "{}")])
    assert store.claim_update("payment") == (46, "{}")
    assert store.claim_update("payment") is None
    clock[0] += 181
    assert store.claim_update("payment") == (46, "{}")
