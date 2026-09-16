import asyncio
import copy
import socket
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from shop.canboso import (
    BALANCE_PATH,
    PRODUCTS_PATH,
    PURCHASE_PATH,
    CanbosoClient,
    HttpTransport,
    Reply,
)
from shop.config import CanbosoSettings
from shop.delivery import DeliveryWorker
from shop.store import ShopError, Store
from shop.supplier_worker import SupplierWorker

BUYER_KEY = "TEST_ONLY_OFFLINE_SUPPLIER_BUYER_KEY"
EMAIL = "Buyer+Offline@example.invalid"
PASSWORD = "TEST_ONLY_PRIVATE_ACCOUNT_PASSWORD"
EVIDENCE = "Offline supplier verification: original request is safe to replay unchanged."
TERMS = "supplier-test-terms-v1"
STARS = 73
PRODUCT_IDS = {
    "account": "offline_account",
    "slot": "offline_slot",
    "business": "slot_chatgpt_business",
}


def catalog_product(sku):
    slot = sku != "account"
    requirements = {"quantityFixed": 1, "customerEmail": slot, "slotMonths": sku == "business"}
    if sku == "business":
        requirements["allowedMonths"] = [1, 3, 6, 12]
    return {
        "productId": PRODUCT_IDS[sku],
        "name": f"Offline {sku}",
        "productType": "slot" if slot else "account",
        "price": {"amount": 8, "currency": "USD", "text": "USD 8.00"},
        "availability": {"available": 100, "sold": 0},
        "promotions": [],
        "purchaseRequirements": requirements,
    }


def purchase_reply(sku="account", *, pending=False, amount=8, currency="USD", email=EMAIL):
    order = {
        "orderCode": f"offline-reference-{sku}",
        "productId": PRODUCT_IDS[sku],
        "productName": f"Offline {sku}",
        "productType": "account" if sku == "account" else "slot",
        "status": "paid" if pending else "completed",
        "quantity": 1,
        "bonusQuantity": 0,
        "finalQuantity": 1,
    }
    body = {
        "success": True,
        "lang": "en",
        "order": order,
        "payment": {
            "amount": amount,
            "amountText": f"{currency} {amount}",
            "originalAmount": amount,
            "originalAmountText": f"{currency} {amount}",
            "discountPercent": 0,
            "discountAmount": 0,
            "discountAmountText": f"{currency} 0",
            "currency": currency,
            "balance": 92,
            "balanceText": f"{currency} 92",
        },
    }
    if sku == "account":
        body["delivery"] = {"accounts": [{
            "user": "offline-login@example.invalid",
            "password": PASSWORD,
            "verifyEmail": "offline-verify@example.invalid",
            "expiryText": "one month",
            "otherInfo": "Offline delivery only",
        }]}
    else:
        order.update(customerEmail=email, fulfillmentStatus="waiting_seller" if pending else "invited")
        if sku == "business":
            order["slotMonths"] = 3
    return Reply(200, body)


class FakeSupplierTransport:
    def __init__(self):
        self.catalog = {
            "success": True,
            "walletCurrency": "USD",
            "products": [catalog_product(sku) for sku in PRODUCT_IDS],
        }
        self.wallet = {"success": True, "walletCurrency": "USD", "balance": 100,
                       "balanceText": "USD 100.00"}
        self.calls = []
        self.outcomes = deque()
        self.read_outcomes = deque()
        self.on_post = None

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    async def request(self, method, path, *, query=None, body=None, headers=None):
        self.calls.append((method, path, copy.deepcopy({
            "query": query, "body": body, "headers": headers,
        })))
        if method == "GET" and path in (PRODUCTS_PATH, BALANCE_PATH):
            if not query or set(query) != {"key"} or body is not None or headers is not None:
                pytest.fail("Supplier GET must use only the documented buyer-key query")
            outcome = self.read_outcomes.popleft() if self.read_outcomes else Reply(
                200, copy.deepcopy(self.catalog if path == PRODUCTS_PATH else self.wallet)
            )
        elif method == "POST" and path == PURCHASE_PATH:
            if query is not None or set(headers or {}) != {"Idempotency-Key"}:
                pytest.fail("Supplier POST must use the documented idempotency header")
            if not self.outcomes:
                pytest.fail("Unexpected supplier POST: no approved mocked purchase remains")
            if self.on_post:
                await self.on_post(body, headers)
            outcome = self.outcomes.popleft()
        else:
            pytest.fail(f"Undocumented supplier endpoint: {method} {path}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SupplierShop:
    def __init__(self, tmp_path, key, clock):
        self.key = key
        self.clock = clock
        self.settings = CanbosoSettings(
            enabled=True, api_key=BUYER_KEY, allow_purchases=True,
            resale_authorized=True, acknowledge_price_race=True,
            budget_currency="USD", spend_budget="100",
        )
        self.transport = FakeSupplierTransport()
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=4242)),
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=4243)),
            refund_star_payment=AsyncMock(),
        )
        self.store = Store(tmp_path / "supplier.sqlite3", key, "production", lambda: clock[0])
        self.store.initialize()
        self.wire()
        for sku in PRODUCT_IDS:
            self.store.upsert_product(self.product(sku))
        self.sync()

    @staticmethod
    def product(sku):
        mapping = {"provider": "canboso", "product_id": PRODUCT_IDS[sku],
                   "product_type": "account" if sku == "account" else "slot",
                   "max_cost": "10", "currency": "USD"}
        if sku == "business":
            mapping["slot_months"] = 3
        return {"sku": sku, "title": f"Offline {sku}", "description": "Fictional test item",
                "category": "Offline tests", "price_stars": STARS, "source": "supplier",
                "active": True, "is_demo": False, "supplier": mapping}

    def wire(self):
        self.store.supplier.configure(self.settings)
        self.client = CanbosoClient(self.settings, self.transport, "production")
        self.delivery = DeliveryWorker(self.store, self.bot, frozenset({999}))
        self.worker = SupplierWorker(self.store, self.client, self.delivery)

    def configure(self, **changes):
        self.settings = replace(self.settings, **changes)
        self.store.supplier.configure(self.settings)
        self.client.settings = self.settings

    def restart(self):
        self.store = Store(self.store.path, self.key, "production", lambda: self.clock[0])
        self.store.initialize()
        self.wire()

    def sync(self):
        assert asyncio.run(self.worker.synchronize()) is True

    def new_order(self, sku="account", user=101, email=None):
        self.store.accept_terms(user, TERMS)
        kwargs = {"customer_email": email} if email is not None else {}
        return self.store.create_order(user, sku, TERMS, **kwargs)

    def approve(self, order):
        self.store.approve_checkout(order["id"], order["user_id"], "XTR", STARS,
                                    "checkout-" + order["id"], TERMS)

    def pay(self, order, charge=None):
        return self.store.record_payment(order["id"], order["user_id"], "XTR", STARS,
                                         charge or "charge-" + order["id"])

    def paid_order(self, sku="account", user=101, email=None):
        order = self.new_order(sku, user, email)
        self.approve(order)
        assert self.pay(order).status == "accepted"
        return order

    def intent(self, order):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM supplier_intents WHERE order_id=?", (order["id"],)).fetchone()
            assert row is not None
            return dict(row)

    def rows(self, table):
        assert table in {"supplier_intents", "supplier_cache", "supplier_audit", "stock", "payment_events"}
        with self.store.connection() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


@pytest.fixture
def supplier_shop(tmp_path, key, clock, monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Real network access is forbidden in supplier integration tests")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        # Windows asyncio needs its local wake-up socket pair, not external network access.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        blocked()

    def connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect_ex(sock, address)
        blocked()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", blocked)
    monkeypatch.setattr(HttpTransport, "request", blocked)
    return SupplierShop(tmp_path, key, clock)


def test_supplier_mapping_is_persisted_with_manual_stars_price(supplier_shop):
    h = supplier_shop
    for sku in PRODUCT_IDS:
        assert h.store.supplier.mapping(sku) == h.product(sku)["supplier"]
        assert h.store.get_product(sku)["price_stars"] == STARS
    h.transport.catalog["products"][0]["price"]["amount"] = 9
    h.sync()
    assert h.store.get_product("account")["price_stars"] == STARS
    assert h.new_order()["price_stars"] == STARS
    with h.store.connection() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='environment'").fetchone()[0] == "production"
    assert not h.transport.posts


def test_invoice_and_precheckout_cannot_claim_supplier_or_delivery(supplier_shop):
    h = supplier_shop
    order = h.new_order()
    for approve in (False, True):
        if approve:
            h.approve(order)
            h.approve(order)
        assert h.store.supplier.claim() is None
        assert h.store.claim_delivery(order["id"]) is None
        assert h.store.due_deliveries() == []
        assert h.intent(order)["state"] == "draft"
        assert h.intent(order)["attempts"] == 0
    assert not h.rows("stock")
    assert not h.rows("payment_events")
    assert not h.transport.posts


def test_concurrent_duplicate_payment_creates_one_queued_intent(supplier_shop):
    h = supplier_shop
    order = h.new_order()
    h.approve(order)
    gate = Barrier(4)

    def pay_once(_):
        gate.wait(timeout=10)
        return h.pay(order)

    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(pay_once, range(4)))
    assert sum(not event.duplicate for event in events) == 1
    assert len({event.event_id for event in events}) == 1
    assert len(h.rows("supplier_intents")) == len(h.rows("payment_events")) == 1
    assert h.intent(order)["state"] == "queued"
    assert h.intent(order)["budget_held"] == 1
    assert h.store.get_order(order["id"])["state"] == "paid"
    assert not h.rows("stock")
    assert h.store.supplier.claim()["order_id"] == order["id"]
    assert h.store.supplier.claim() is None


@pytest.mark.parametrize("user,currency,amount", [(102, "XTR", STARS), (101, "USD", STARS), (101, "XTR", 1)])
def test_mismatched_payment_is_reviewed_without_queueing(supplier_shop, user, currency, amount):
    h = supplier_shop
    order = h.new_order()
    event = h.store.record_payment(order["id"], user, currency, amount, "wrong-payment")
    assert event.status == "review" and event.order_id is None
    assert h.intent(order)["state"] == "draft"
    assert h.store.supplier.claim() is None
    assert h.store.claim_delivery(order["id"]) is None
    assert len(h.store.review_payments()) == 1


def test_extra_charge_refund_does_not_cancel_original_supplier_intent(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    original = h.store.payment_for_order(order["id"])
    extra = h.pay(order, "extra-charge")
    assert extra.status == "review" and extra.order_id is None
    assert h.store.get_payment(extra.event_id)["reason"] == "extra_charge_for_order"
    h.store.begin_refund(extra.event_id)
    h.store.finish_refund(extra.event_id)
    assert h.store.get_payment(original["id"])["status"] == "accepted"
    assert h.store.get_order(order["id"])["state"] == "paid"
    assert h.intent(order)["state"] == "queued"
    assert len(h.rows("supplier_intents")) == 1


@pytest.mark.parametrize("sku", ["slot", "business"])
def test_slot_input_and_exact_request_survive_restart(supplier_shop, sku):
    h = supplier_shop
    order = h.new_order(sku, email=EMAIL)
    initial = h.intent(order)
    h.restart()
    expected = {"customer_email": EMAIL}
    if sku == "business":
        expected["slot_months"] = 3
    assert h.store.supplier.preview_input(order["id"], 101) == expected
    with pytest.raises(ShopError, match="not found"):
        h.store.supplier.preview_input(order["id"], 102)
    assert h.intent(order)["request_ciphertext"] == initial["request_ciphertext"]
    assert h.store.supplier.decrypt(initial["request_ciphertext"]) == {
        "key": BUYER_KEY, "product_id": PRODUCT_IDS[sku], "quantity": 1, **expected,
    }
    assert BUYER_KEY not in str(h.store.supplier.inspect(order["id"]))


def test_changed_slot_email_never_reuses_the_previous_request(supplier_shop):
    h = supplier_shop
    first = h.new_order("slot", email=EMAIL)
    second = h.new_order("slot", email="second@example.invalid")
    assert first["id"] != second["id"]
    assert h.store.supplier.preview_input(first["id"], 101) == {"customer_email": EMAIL}
    assert h.store.supplier.preview_input(second["id"], 101) == {"customer_email": "second@example.invalid"}


@pytest.mark.parametrize("email", [None, "not-an-email", "buyer@example.invalid\nsecond@example.invalid"])
def test_invalid_slot_email_rolls_back_order_and_intent(supplier_shop, email):
    h = supplier_shop
    with pytest.raises(ShopError, match="email"):
        h.new_order("slot", email=email)
    assert h.store.user_orders(101) == []
    assert h.rows("supplier_intents") == []
    assert not h.transport.posts


@pytest.mark.parametrize("limit", ["wallet", "budget", "stock"])
def test_simultaneous_checkouts_reserve_one_affordable_commitment(supplier_shop, limit):
    h = supplier_shop
    if limit == "wallet":
        h.transport.wallet["balance"] = 15
    elif limit == "budget":
        h.configure(spend_budget="15")
    else:
        h.transport.catalog["products"][0]["availability"]["available"] = 1
    h.sync()
    orders = [h.new_order(user=user) for user in (101, 102, 103)]
    gate = Barrier(3)

    def checkout(order):
        gate.wait(timeout=10)
        try:
            h.approve(order)
            return True
        except ShopError as exc:
            assert limit in str(exc).lower()
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(checkout, orders))
    assert sum(outcomes) == 1
    assert sum(h.store.get_order(order["id"])["state"] == "checkout" for order in orders) == 1
    assert all(h.intent(order)["attempts"] == 0 for order in orders)
    assert h.store.supplier.claim() is None
    assert not h.transport.posts


@pytest.mark.parametrize("release", ["expired", "refund"])
def test_reservation_released_only_when_checkout_expires_or_is_cancelled(supplier_shop, release):
    h = supplier_shop
    h.transport.wallet["balance"] = 10
    h.sync()
    first = h.new_order()
    second = h.new_order(user=102)
    h.approve(first)
    with pytest.raises(ShopError, match="wallet"):
        h.approve(second)
    if release == "expired":
        h.clock[0] += 901
        h.store.expire_orders()
        h.sync()
        second = h.new_order(user=102)
    else:
        event = h.pay(first)
        h.store.begin_refund(event.event_id)
        assert h.intent(first)["state"] == "cancelled"
    h.approve(second)
    assert h.store.get_order(second["id"])["state"] == "checkout"
    assert not h.transport.posts


def test_queued_refund_cancels_before_purchase(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    event = h.store.payment_for_order(order["id"])
    h.store.begin_refund(event["id"])
    assert h.intent(order)["state"] == "cancelled"
    assert h.intent(order)["budget_held"] == 0
    assert h.store.supplier.claim() is None
    h.store.finish_refund(event["id"])
    assert h.store.get_order(order["id"])["state"] == "refunded"
    assert h.store.claim_delivery(order["id"]) is None


@pytest.mark.parametrize("state", ["processing", "uncertain", "retry_approved"])
def test_refund_guard_keeps_unsafe_purchase_and_payment_unchanged(supplier_shop, state):
    h = supplier_shop
    order = h.paid_order()
    h.store.supplier.claim()
    if state != "processing":
        h.store.supplier.fail(order["id"], "supplier_transport_failure")
    if state == "retry_approved":
        h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
    event = h.store.payment_for_order(order["id"])
    with pytest.raises(ShopError, match="pending|uncertain|Resolve"):
        h.store.begin_refund(event["id"])
    assert h.intent(order)["state"] == state
    assert h.store.get_payment(event["id"])["status"] == "accepted"
    assert h.store.get_order(order["id"])["state"] == "paid"


@pytest.mark.parametrize("action", ["retry_same_request", "stop_for_refund", "fulfill"])
def test_operator_resolution_requires_evidence_and_preserves_state(supplier_shop, action):
    h = supplier_shop
    order = h.paid_order()
    h.store.supplier.claim()
    h.store.supplier.fail(order["id"], "supplier_transport_failure")
    with pytest.raises(ShopError, match="evidence"):
        h.store.supplier.resolve(order["id"], action, "", "offline-operator", delivery="verified text")
    assert h.intent(order)["state"] == "uncertain"
    assert h.rows("supplier_audit") == []
    assert h.rows("stock") == []
    with pytest.raises(ShopError):
        h.store.retry_order(order["id"])
    assert h.store.supplier.claim() is None


def test_verified_stop_for_refund_records_encrypted_evidence(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.store.supplier.claim()
    h.store.supplier.fail(order["id"], "supplier_transport_failure")
    evidence = "Offline supplier verified no fulfillment; merchant approved refund."
    h.store.supplier.resolve(order["id"], "stop_for_refund", evidence, "offline-operator")
    audit, = h.rows("supplier_audit")
    assert audit["action"] == "stop_for_refund" and audit["operator_id"] == "offline-operator"
    assert evidence not in audit["evidence_ciphertext"]
    assert h.store.supplier.decrypt(audit["evidence_ciphertext"]) == evidence
    event = h.store.payment_for_order(order["id"])
    h.store.begin_refund(event["id"])
    h.store.finish_refund(event["id"])
    assert h.store.supplier.claim() is None
    assert h.store.get_order(order["id"])["state"] == "refunded"


def test_out_of_order_external_refund_cancels_later_payment(supplier_shop):
    h = supplier_shop
    order = h.new_order()
    h.approve(order)
    charge = "already-refunded-charge"
    h.store.record_refund(charge, 101, "XTR", STARS)
    event = h.pay(order, charge)
    assert event.status == "refunded"
    assert h.pay(order, charge).duplicate is True
    assert h.intent(order)["state"] == "cancelled"
    assert h.intent(order)["budget_held"] == 0
    assert h.store.supplier.claim() is None
    assert h.store.claim_delivery(order["id"]) is None
    assert h.store.get_order(order["id"])["state"] == "refunded"
    assert not h.transport.posts


def test_price_increase_before_checkout_blocks_without_payment_approval(supplier_shop):
    h = supplier_shop
    order = h.new_order()
    h.transport.catalog["products"][0]["price"]["amount"] = 10.01
    h.sync()
    with pytest.raises(ShopError, match="price|limit"):
        h.approve(order)
    assert h.store.get_order(order["id"])["state"] == "invoice"
    assert h.intent(order)["attempts"] == 0
    assert h.rows("payment_events") == []
    assert not h.transport.posts


@pytest.mark.parametrize("change", ["stale", "buyer_key", "cooldown"])
def test_checkout_requires_fresh_same_key_snapshot_without_cooldown(supplier_shop, change):
    h = supplier_shop
    order = h.new_order()
    if change == "stale":
        h.clock[0] += 121
    elif change == "buyer_key":
        h.configure(api_key="TEST_ONLY_ROTATED_BUYER_KEY")
    else:
        h.store.supplier.defer_network(60)
    with pytest.raises(ShopError):
        h.approve(order)
    assert h.store.get_order(order["id"])["state"] == "invoice"
    assert h.intent(order)["attempts"] == 0
    assert h.store.supplier.claim() is None


def test_buyer_key_rotation_holds_paid_intent_and_disallows_operator_replay(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    original = h.intent(order)
    h.configure(api_key="TEST_ONLY_ROTATED_BUYER_KEY")
    h.sync()
    assert h.store.supplier.claim() is None
    assert h.intent(order)["hold_reason"] == "buyer_key_changed"
    assert h.intent(order)["request_ciphertext"] == original["request_ciphertext"]
    assert h.intent(order)["attempts"] == 0
    with pytest.raises(ShopError, match="buyer key"):
        h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
    assert not h.transport.posts
