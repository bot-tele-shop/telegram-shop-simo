"""Provider-agnostic guard rails for the multi-supplier layer:
a disabled or locked provider never touches its API, API keys never leak
into logs/errors/evidence, and a purchase flows end to end through the
worker for a non-Canboso provider exactly once."""
import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from test_new_supplier_clients import (
    EMAIL_INPUT,
    FakeTransport,
    jaha_order,
    jaha_product,
    settings_for,
)
from test_providers import multi_store as multi_store

from shop import acczone, elite_emporium, jaha_digital
from shop.canboso import PurchaseUncertain, Reply
from shop.delivery import DeliveryWorker
from shop.supplier_worker import SupplierWorker

MODULES = {"jaha_digital": jaha_digital, "elite_emporium": elite_emporium,
           "acczone": acczone}
BODIES = {
    "jaha_digital": {"product_code": "p1", "quantity": 1, "max_unit_price_usdt": "10"},
    "elite_emporium": {"product_id": 7, "quantity": 1,
                       "idempotency_key": "ds-order-0001-test"},
    "acczone": {"service_key": "gemini", "quantity": 1},
}
IDEMPOTENCY = "ds-order-0001-test"


@pytest.mark.parametrize("name", list(MODULES))
def test_disabled_provider_never_touches_the_api(name):
    settings = replace(settings_for(name), enabled=False)
    transport = FakeTransport()
    client = MODULES[name].Client(settings, transport, "production")
    for call in (lambda: client.products(), lambda: client.balance(),
                 lambda: client.purchase(BODIES[name], IDEMPOTENCY)):
        with pytest.raises(Exception, match="disabled_or_key_missing"):
            asyncio.run(call())
    assert transport.calls == []


@pytest.mark.parametrize("name", list(MODULES))
def test_keyless_provider_never_touches_the_api(name):
    settings = replace(settings_for(name), api_key="")
    transport = FakeTransport()
    client = MODULES[name].Client(settings, transport, "production")
    with pytest.raises(Exception, match="disabled_or_key_missing"):
        asyncio.run(client.products())
    assert transport.calls == []


@pytest.mark.parametrize("name", list(MODULES))
def test_locked_spending_blocks_purchase_before_any_request(name):
    settings = replace(settings_for(name), allow_purchases=False)
    transport = FakeTransport()
    client = MODULES[name].Client(settings, transport, "production")
    with pytest.raises(Exception, match="live_supplier_spending_locked"):
        asyncio.run(client.purchase(BODIES[name], IDEMPOTENCY))
    assert transport.calls == []


@pytest.mark.parametrize("name", list(MODULES))
def test_api_key_never_leaks_into_logs_errors_or_evidence(name, caplog):
    settings = settings_for(name)
    transport = FakeTransport(aiohttp.ClientError("connection reset"))
    client = MODULES[name].Client(settings, transport, "production")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PurchaseUncertain) as excinfo:
            asyncio.run(client.purchase(BODIES[name], IDEMPOTENCY))
    assert settings.api_key not in str(excinfo.value)
    assert settings.api_key not in repr(excinfo.value.args)
    raw = excinfo.value.raw
    assert raw is None or settings.api_key not in repr(raw)
    for record in caplog.records:
        assert settings.api_key not in record.getMessage()


def _insert_queued_intent(store, order_id, provider, body):
    now = store.clock()
    key_hash = store.supplier.settings_for(provider).key_fingerprint
    with store.transaction() as db:
        db.execute("INSERT INTO orders(id,user_id,sku,title,price_stars,terms_version,state,"
                   "created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (order_id, 101, f"{provider}-sku", f"Multi {provider}", 400, "terms-v1",
                    "paid", now, now, now + 900))
        db.execute("INSERT INTO supplier_intents(order_id,idempotency_key,request_ciphertext,"
                   "key_hash,provider,product_id,product_type,max_cost,currency,state,"
                   "attempts,first_sent_at,budget_held,next_attempt_at,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (order_id, f"ds-{order_id}", store.supplier.encrypt(body), key_hash,
                    provider, body.get("product_code") or body.get("product_id")
                    or body.get("service_key"), "account", "10", "USD", "queued",
                    0, None, 1, 0, now, now))


def test_worker_purchases_through_a_non_canboso_provider_exactly_once(multi_store):
    multi_store.upsert_product({
        "sku": "jaha_digital-sku", "title": "Jaha", "description": "x", "category": "Keys",
        "price_stars": 400, "source": "supplier", "active": True, "is_demo": False,
        "supplier": {"provider": "jaha_digital", "product_id": "jaha_pid_1",
                     "product_type": "account", "currency": "USD", "max_cost": "10"},
    })
    body = {"product_code": "jaha_pid_1", "quantity": 1, "max_unit_price_usdt": "10"}
    _insert_queued_intent(multi_store, "jaha-order-1", "jaha_digital", body)

    settings = replace(settings_for("jaha_digital"), api_key="TEST_ONLY_JAHA_KEY")
    transport = FakeTransport(
        Reply(200, {"products": [jaha_product("jaha_pid_1", price="8.5000")],
                    "next_cursor": None}),
        Reply(200, {"account": {
            "client_id": "c1", "balance_usdt": "100.0000", "currency": "USDT",
            "status": "active", "language": "en", "terms_version": "v1",
            "purchase_amount_limit_usdt": None, "daily_turnover_limit_usdt": None}}),
        Reply(201, jaha_order("JD-9001", code="jaha_pid_1", external=None, total="8.5000")),
    )
    client = jaha_digital.JahaClient(settings, transport, "production")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    delivery = DeliveryWorker(multi_store, bot, frozenset({999}))
    worker = SupplierWorker(multi_store, {"jaha_digital": client}, delivery)
    assert asyncio.run(worker.synchronize()) is True
    assert asyncio.run(worker.purchase_one()) is True
    # The exact persisted body and idempotency key were sent, once.
    post = transport.calls[2]
    assert post["method"] == "POST" and post["path"] == "/v1/orders"
    assert post["body"] == body
    assert post["headers"]["Idempotency-Key"] == "ds-jaha-order-1"
    with multi_store.connection() as db:
        intent = db.execute("SELECT * FROM supplier_intents WHERE order_id='jaha-order-1'").fetchone()
        stock = db.execute("SELECT * FROM stock WHERE order_id='jaha-order-1'").fetchone()
    assert intent["state"] == "completed"
    assert intent["supplier_reference"] == "JD-9001"
    assert stock is not None
    # Nothing left to claim: a completed purchase is never replayed.
    assert asyncio.run(worker.purchase_one()) is False
    assert len([c for c in transport.calls if c["method"] == "POST"]) == 1


def _insert_uncertain_intent(store, order_id, provider, product_id, product_type):
    now = store.clock()
    key_hash = store.supplier.settings_for(provider).key_fingerprint
    with store.transaction() as db:
        db.execute("INSERT INTO orders(id,user_id,sku,title,price_stars,terms_version,state,"
                   "created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (order_id, 101, f"{provider}-sku", f"Multi {provider}", 400, "terms-v1",
                    "paid", now, now, now + 900))
        db.execute("INSERT INTO supplier_intents(order_id,idempotency_key,request_ciphertext,"
                   "key_hash,provider,product_id,product_type,max_cost,currency,state,"
                   "attempts,first_sent_at,budget_held,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (order_id, f"ds-{order_id}", store.supplier.encrypt({"quantity": 1}),
                    key_hash, provider, product_id, product_type, "10", "USD", "uncertain",
                    1, now, 1, now, now))


def test_slot_recovery_after_restart_does_not_false_hold(multi_store):
    """A restart leaves the client's catalog cache cold; recovery must sync
    first and must never report a guessed product_type as a mismatch."""
    multi_store.upsert_product({
        "sku": "jaha_digital-sku", "title": "Jaha slot", "description": "x",
        "category": "Keys", "price_stars": 400, "source": "supplier", "active": True,
        "is_demo": False,
        "supplier": {"provider": "jaha_digital", "product_id": "jaha_slot_1",
                     "product_type": "slot", "currency": "USD", "max_cost": "10"},
    })
    _insert_uncertain_intent(multi_store, "slot-order-1", "jaha_digital",
                             "jaha_slot_1", "slot")
    settings = replace(settings_for("jaha_digital"), api_key="TEST_ONLY_JAHA_KEY")
    transport = FakeTransport(
        Reply(200, {"products": [jaha_product("jaha_slot_1", buyer_input=EMAIL_INPUT)],
                    "next_cursor": None}),
        Reply(200, {"account": {
            "client_id": "c1", "balance_usdt": "100.0000", "currency": "USDT",
            "status": "active", "language": "en", "terms_version": "v1",
            "purchase_amount_limit_usdt": None, "daily_turnover_limit_usdt": None}}),
        Reply(200, {"orders": [{"order_number": "JD-7001", "status": "completed",
                                "external_order_id": "ds-slot-order-1"}],
                    "next_cursor": None}),
        Reply(200, jaha_order("JD-7001", code="jaha_slot_1", external="ds-slot-order-1")),
    )
    client = jaha_digital.JahaClient(settings, transport, "production")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    delivery = DeliveryWorker(multi_store, bot, frozenset({999}))
    worker = SupplierWorker(multi_store, {"jaha_digital": client}, delivery)
    asyncio.run(worker.tick())  # Cold start: cache is empty until this sync.
    with multi_store.connection() as db:
        intent = db.execute("SELECT * FROM supplier_intents WHERE order_id='slot-order-1'").fetchone()
        stock = db.execute("SELECT * FROM stock WHERE order_id='slot-order-1'").fetchone()
    assert intent["state"] == "completed"
    assert intent["hold_reason"] == "recovered_via_supplier_lookup"
    assert stock is not None
