import asyncio
import base64
import copy
import json
import socket
import traceback
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from shop.canboso import (
    BALANCE_PATH,
    BASE_URL,
    PRODUCTS_PATH,
    PURCHASE_PATH,
    CanbosoClient,
    CanbosoError,
    HttpTransport,
    PurchaseRejected,
    PurchaseUncertain,
    RateLimited,
    Reply,
    money,
    valid_email,
)
from shop.config import CanbosoSettings

KEY = "test-buyer-key-never-use-live"
IDEMPOTENCY = "purchase:20260915.test-0001"
ACCOUNT_ID = "64f0c0f2b90c2b4c5a123456"
SLOT_ID = "64f0c0f2b90c2b4c5a654321"
BUSINESS_ID = "slot_chatgpt_business"
EMAIL = "buyer@example.com"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Live network access is forbidden in Canboso tests")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        # Windows asyncio creates a loopback socket pair for its wake-up pipe.
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


@pytest.fixture
def settings():
    return CanbosoSettings(
        enabled=True, api_key=KEY, allow_purchases=True, resale_authorized=True,
        acknowledge_price_race=True, budget_currency="VND", spend_budget="1000000",
    )


@pytest.fixture(scope="module")
def contract():
    path = Path(__file__).resolve().parents[1] / "references/canboso-openapi-2.1.0.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def account_reply(contract):
    examples = contract["paths"][PURCHASE_PATH]["post"]["responses"]["200"]["content"]["application/json"]["examples"]
    body = copy.deepcopy(examples["normalProduct"]["value"])
    # The public example abbreviates three delivered accounts to one. Make the
    # fixture internally consistent with the shop's single-item order policy.
    body["order"].update(quantity=1, bonusQuantity=0, finalQuantity=1)
    return body


@pytest.fixture
def slot_reply(contract):
    examples = contract["paths"][PURCHASE_PATH]["post"]["responses"]["200"]["content"]["application/json"]["examples"]
    return copy.deepcopy(examples["manualSlotProduct"]["value"])


@pytest.fixture
def business_reply(contract):
    examples = contract["paths"][PURCHASE_PATH]["post"]["responses"]["200"]["content"]["application/json"]["examples"]
    return copy.deepcopy(examples["slotProduct"]["value"])


@pytest.fixture
def product():
    return {
        "productId": ACCOUNT_ID, "name": "Test account", "productType": "account",
        "price": {"amount": 50000, "currency": "VND", "text": "VND 50,000"},
        "availability": {"available": 60, "sold": 40}, "promotions": [],
    }


class FakeTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        assert self.outcomes, "Unexpected request or automatic retry"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def client_for(settings, *outcomes, environment="production"):
    transport = FakeTransport(*outcomes)
    return CanbosoClient(settings, transport, environment), transport


def request_body(product_id=ACCOUNT_ID, **kwargs):
    return {"key": KEY, "product_id": product_id, "quantity": 1, **kwargs}


def purchase(client, body=None, key=IDEMPOTENCY):
    return asyncio.run(client.purchase(request_body() if body is None else body, key))


def change(body, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        body = body[int(part)] if isinstance(body, list) else body[part]
    last = int(parts[-1]) if isinstance(body, list) else parts[-1]
    body[last] = value


def assert_private(exc, *secrets):
    rendered = str(exc) + repr(exc) + repr(exc.args) + "".join(traceback.format_exception(exc))
    for secret in secrets:
        assert secret not in rendered


def test_only_public_contract_endpoints(contract):
    assert contract["info"]["version"] == "2.1.0"
    assert BASE_URL == contract["servers"][0]["url"]
    assert set(contract["paths"]) == {PRODUCTS_PATH, BALANCE_PATH, PURCHASE_PATH}
    for path in (PRODUCTS_PATH, BALANCE_PATH):
        assert contract["paths"][path]["get"]["parameters"][0]["in"] == "query"
        assert contract["paths"][path]["get"]["parameters"][0]["name"] == "key"
    header = contract["paths"][PURCHASE_PATH]["post"]["parameters"][0]
    assert (header["in"], header["name"]) == ("header", "Idempotency-Key")


def test_get_key_query_and_balance_currency(settings, product, contract):
    catalog = {"success": True, "walletCurrency": "VND", "products": [product]}
    examples = contract["paths"][BALANCE_PATH]["get"]["responses"]["200"]["content"]["application/json"]["examples"]
    balance = examples["usdWallet"]["value"]
    client, transport = client_for(settings, Reply(200, catalog), Reply(200, balance))
    assert asyncio.run(client.products()) is catalog
    assert asyncio.run(client.balance()) is balance
    assert transport.calls == [
        ("GET", PRODUCTS_PATH, {"query": {"key": KEY}}),
        ("GET", BALANCE_PATH, {"query": {"key": KEY}}),
    ]
    assert balance["balance"] == 18.259259
    assert balance["balance"] != balance["balanceVnd"]


@pytest.mark.parametrize("currency,amount", [("VND", 90000), ("USD", 18.259259), ("VND", 0)])
def test_account_completion_exact_post_and_private_result(settings, account_reply, currency, amount):
    account_reply["payment"].update(currency=currency, amount=amount)
    client, transport = client_for(settings, Reply(200, account_reply))
    body = request_body()
    before = copy.deepcopy(body)
    result = purchase(client, body)
    assert result.status == "completed"
    assert result.reference == account_reply["order"]["orderCode"]
    assert result.amount == Decimal(str(amount))
    assert result.currency == currency
    assert "Login: account1@example.com" in result.payload
    assert "Password: secret-password" in result.payload
    assert result.raw is account_reply
    assert "secret-password" not in repr(result)
    assert body == before
    assert transport.calls == [("POST", PURCHASE_PATH, {
        "body": body, "headers": {"Idempotency-Key": IDEMPOTENCY},
    })]
    assert transport.calls[0][2]["body"] is body


def test_optional_product_id_and_default_quantity(settings, account_reply):
    del account_reply["order"]["productId"]
    body = {"key": KEY, "product_id": ACCOUNT_ID}
    client, transport = client_for(settings, Reply(200, account_reply))
    assert purchase(client, body).status == "completed"
    assert "quantity" not in transport.calls[0][2]["body"]


def test_account_bonus_delivery_and_nullable_fields(settings, account_reply):
    account_reply["order"].update(bonusQuantity=1, finalQuantity=2)
    first = account_reply["delivery"]["accounts"][0]
    first.update(verifyEmail=None, expiryText="one month", otherInfo="Test note")
    account_reply["delivery"]["accounts"].append({"user": "second", "password": "second-password"})
    client, _ = client_for(settings, Reply(200, account_reply))
    result = purchase(client)
    assert result.status == "completed"
    assert "Item 2\nLogin: second" in result.payload
    assert "Expiry: one month" in result.payload
    assert "Notes: Test note" in result.payload
    assert "Verification email: None" not in result.payload


def test_catalog_slot_is_pending_not_delivered(settings, slot_reply):
    client, transport = client_for(settings, Reply(200, slot_reply))
    result = purchase(client, request_body(SLOT_ID, customer_email=EMAIL))
    assert result.status == "pending"
    assert result.payload == ""
    assert result.raw is slot_reply
    assert len(transport.calls) == 1


@pytest.mark.parametrize("fulfillment", ["invited", "completed"])
def test_catalog_slot_completed_with_fulfillment_confirmation(settings, slot_reply, fulfillment):
    slot_reply["order"].update(status="completed", fulfillmentStatus=fulfillment)
    client, _ = client_for(settings, Reply(200, slot_reply))
    result = purchase(client, request_body(SLOT_ID, customer_email=EMAIL))
    assert result.status == "completed"
    assert EMAIL in result.payload
    assert fulfillment in result.payload


@pytest.mark.parametrize("months", [1, 3, 6, 12])
def test_business_slot_allowed_durations(settings, business_reply, months):
    business_reply["order"]["slotMonths"] = months
    client, transport = client_for(settings, Reply(200, business_reply))
    body = request_body(BUSINESS_ID, customer_email=EMAIL, slot_months=months)
    del body["quantity"]
    assert purchase(client, body).status == "completed"
    assert transport.calls[0][2]["body"] == body


@pytest.mark.parametrize("field,value", [
    ("enabled", False), ("api_key", ""), ("api_key", "short"),
    ("allow_purchases", False), ("resale_authorized", False),
    ("acknowledge_price_race", False), ("budget_currency", "EUR"),
    ("spend_budget", "0"), ("spend_budget", "NaN"),
])
def test_purchase_safety_gates_do_not_send(settings, field, value):
    client, transport = client_for(replace(settings, **{field: value}))
    with pytest.raises(CanbosoError) as caught:
        purchase(client)
    assert not isinstance(caught.value, PurchaseUncertain)
    assert not transport.calls


@pytest.mark.parametrize("environment", ["test", "staging", "", "Production"])
def test_live_purchase_disabled_outside_production(settings, environment):
    client, transport = client_for(settings, environment=environment)
    with pytest.raises(CanbosoError, match="live_supplier_spending_locked"):
        purchase(client)
    assert not transport.calls


@pytest.mark.parametrize("method", ["products", "balance"])
def test_read_only_disabled_without_key(settings, method):
    client, transport = client_for(replace(settings, api_key=""))
    with pytest.raises(CanbosoError):
        asyncio.run(getattr(client, method)())
    assert not transport.calls


@pytest.mark.parametrize("quantity", [True, False, 1.0, "1", None, [], {}, 0, -1, 2])
def test_quantity_requires_exact_integer_one(settings, quantity):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="only_single_item_orders_supported"):
        purchase(client, request_body(quantity=quantity))
    assert not transport.calls


@pytest.mark.parametrize("body", [None, [], "body", True, 1])
def test_purchase_request_requires_object(settings, body):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_purchase_request"):
        asyncio.run(client.purchase(body, IDEMPOTENCY))
    assert not transport.calls


@pytest.mark.parametrize("product_id", [None, 1, True, [], {}, "", "  "])
def test_product_id_requires_nonempty_string(settings, product_id):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_product_id"):
        purchase(client, request_body(product_id))
    assert not transport.calls


@pytest.mark.parametrize("key", [None, 12345678, True, [], "short", "x" * 129, "purchase\r\ninject", "purchase\x00key", "purchase\x7fkey"])
def test_invalid_idempotency_key_never_sends(settings, key):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_idempotency_key"):
        purchase(client, key=key)
    assert not transport.calls


@pytest.mark.parametrize("key", ["x" * 8, "x" * 128, "purchase:exact.value/one+two=three"])
def test_contract_idempotency_length_and_punctuation_preserved(settings, account_reply, key):
    client, transport = client_for(settings, Reply(200, account_reply))
    purchase(client, key=key)
    assert transport.calls[0][2]["headers"] == {"Idempotency-Key": key}


@pytest.mark.parametrize("extra", [{"key": "different-buyer-key"}, {"max_price": 100}, {"bot_id": 1}, {"product_type": "account"}])
def test_changed_key_and_undocumented_fields_never_sent(settings, extra):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError):
        purchase(client, request_body(**extra))
    assert not transport.calls


@pytest.mark.parametrize("months", [None, True, False, 1.0, "3", 0, -1, 2, 24, [], {}])
def test_business_slot_rejects_invalid_months(settings, months):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_slot_months"):
        purchase(client, request_body(BUSINESS_ID, customer_email=EMAIL, slot_months=months))
    assert not transport.calls


def test_business_slot_requires_months(settings):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_slot_months"):
        purchase(client, request_body(BUSINESS_ID, customer_email=EMAIL))
    assert not transport.calls


@pytest.mark.parametrize("email", [None, "", True, [], "not-email", "buyer@example.com\r\nother", " buyer@example.com "])
def test_business_slot_requires_valid_exact_email(settings, email):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_customer_email"):
        purchase(client, request_body(BUSINESS_ID, customer_email=email, slot_months=3))
    assert not transport.calls


def test_business_slot_requires_email(settings):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="invalid_customer_email"):
        purchase(client, request_body(BUSINESS_ID, slot_months=3))
    assert not transport.calls


@pytest.mark.parametrize("product_id", [ACCOUNT_ID, SLOT_ID])
def test_months_not_sent_for_non_business_products(settings, product_id):
    client, transport = client_for(settings)
    with pytest.raises(CanbosoError, match="months_only_allowed_for_business_slot"):
        purchase(client, request_body(product_id, customer_email=EMAIL, slot_months=3))
    assert not transport.calls


@pytest.mark.parametrize("product_type", ["slot", "upgrade_account"])
def test_catalog_requirements_prevent_unsupported_purchase(settings, product, product_type):
    product["productType"] = product_type
    client, transport = client_for(settings, Reply(200, {"success": True, "products": [product]}))
    asyncio.run(client.products())
    code = "invalid_customer_email" if product_type == "slot" else "unsupported_product_type"
    with pytest.raises(CanbosoError, match=code):
        purchase(client)
    assert len(transport.calls) == 1


def test_business_duration_must_be_advertised_when_catalog_loaded(settings, product):
    product.update(productId=BUSINESS_ID, productType="slot", purchaseRequirements={
        "customerEmail": True, "slotMonths": True, "quantityFixed": 1, "allowedMonths": [1, 6],
    })
    client, transport = client_for(settings, Reply(200, {"success": True, "products": [product]}))
    asyncio.run(client.products())
    with pytest.raises(CanbosoError, match="invalid_slot_months"):
        purchase(client, request_body(BUSINESS_ID, customer_email=EMAIL, slot_months=3))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("path,value", [
    ("success", 1), ("success", "true"), ("lang", None), ("order", []), ("payment", []),
    ("order.orderCode", []), ("order.orderCode", ""), ("order.orderCode", "bad\nreference"),
    ("order.orderCode", "x" * 129), ("order.productId", None), ("order.productId", []),
    ("order.productId", "wrong"), ("order.productName", None), ("order.status", None),
    ("order.status", "paid"), ("order.productType", "upgrade_account"),
    ("order.productType", "license"), ("order.productType", []), ("order.productType", None),
    ("order.quantity", True), ("order.quantity", 1.0), ("order.quantity", "1"), ("order.quantity", 2),
    ("order.bonusQuantity", True), ("order.bonusQuantity", -1), ("order.bonusQuantity", "0"),
    ("order.finalQuantity", True), ("order.finalQuantity", 1.0), ("order.finalQuantity", 0),
    ("order.finalQuantity", 2), ("order.finalQuantity", 101), ("order.customerEmail", None),
    ("order.customerEmail", "other@example.com"), ("order.slotMonths", 3),
    ("order.fulfillmentStatus", []), ("order.autoCompleted", "false"),
    ("payment.amount", True), ("payment.amount", "90000"), ("payment.amount", -1),
    ("payment.amount", float("nan")), ("payment.amount", float("inf")),
    ("payment.amount", []), ("payment.amountText", None), ("payment.originalAmount", None),
    ("payment.discountPercent", True), ("payment.discountAmount", -1),
    ("payment.balance", "120000"), ("payment.currency", "EUR"), ("payment.currency", []),
    ("delivery", []), ("delivery", None), ("delivery.accounts", None),
    ("delivery.accounts", []), ("delivery.accounts", {}), ("delivery.accounts.0", "credential"),
    ("delivery.accounts.0.user", None), ("delivery.accounts.0.user", 7),
    ("delivery.accounts.0.password", True), ("delivery.accounts.0.password", ""),
    ("delivery.accounts.0.verifyEmail", []), ("delivery.accounts.0.expiryText", 123),
    pytest.param("delivery.accounts.0.otherInfo", "x" * 50001, id="oversized-notes"),
])
def test_malformed_purchase_is_uncertain_with_raw(settings, account_reply, path, value):
    change(account_reply, path, value)
    client, transport = client_for(settings, Reply(200, account_reply))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is account_reply
    assert_private(caught.value, KEY, "secret-password", "account1@example.com")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("section,fields", [
    (None, ["success", "lang", "order", "payment"]),
    ("order", ["orderCode", "status", "productName", "productType", "quantity", "bonusQuantity", "finalQuantity"]),
    ("payment", ["amount", "amountText", "originalAmount", "originalAmountText", "discountPercent", "discountAmount", "discountAmountText", "currency", "balance", "balanceText"]),
])
def test_all_required_purchase_fields_enforced(settings, account_reply, contract, section, fields):
    schema = contract["components"]["schemas"]["PurchaseResponse"]
    required = schema["required"] if section is None else schema["properties"][section]["required"]
    assert set(fields) == set(required)
    for field in fields:
        body = copy.deepcopy(account_reply)
        del (body if section is None else body[section])[field]
        client, transport = client_for(settings, Reply(200, body))
        with pytest.raises(PurchaseUncertain) as caught:
            purchase(client)
        assert caught.value.raw is body
        assert len(transport.calls) == 1


@pytest.mark.parametrize("status,fulfillment", [
    ("paid", "invited"), ("completed", "waiting_seller"), ("completed", None),
    ("completed", []), ("cancelled", "waiting_seller"), ("unknown", "unknown"),
])
def test_unconfirmed_slot_is_uncertain(settings, slot_reply, status, fulfillment):
    slot_reply["order"].update(status=status, fulfillmentStatus=fulfillment)
    client, _ = client_for(settings, Reply(200, slot_reply))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client, request_body(SLOT_ID, customer_email=EMAIL))
    assert caught.value.raw is slot_reply


@pytest.mark.parametrize("field,value", [
    ("customerEmail", "other@example.com"), ("customerEmail", None),
    ("slotMonths", True), ("slotMonths", 3.0), ("slotMonths", 6), ("slotMonths", None),
    ("productType", "account"), ("quantity", True), ("autoCompleted", 1),
])
def test_business_slot_response_matches_exact_request(settings, business_reply, field, value):
    business_reply["order"][field] = value
    client, _ = client_for(settings, Reply(200, business_reply))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client, request_body(BUSINESS_ID, customer_email=EMAIL, slot_months=3))
    assert caught.value.raw is business_reply


@pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 400, 401, 403, 404, 409, 500, 502, 503])
@pytest.mark.parametrize("body", [None, [], "<html>private-response</html>", {}, {"success": "true"}])
def test_unconfirmed_status_and_nonobject_replies_preserve_raw(settings, status, body):
    client, transport = client_for(settings, Reply(status, body))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is body
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 404])
def test_explicit_rejection_is_not_uncertainty(settings, status):
    body = {"success": False, "message": f"Private message {KEY}"}
    client, transport = client_for(settings, Reply(status, body))
    with pytest.raises(PurchaseRejected) as caught:
        purchase(client)
    assert caught.value.code == f"supplier_rejected_{status}"
    assert_private(caught.value, KEY, "Private message")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [200, 409, 500, 502, 503])
def test_false_success_does_not_prove_no_debit(settings, status):
    body = {"success": False, "message": "supplier-private-error"}
    client, transport = client_for(settings, Reply(status, body))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is body
    assert len(transport.calls) == 1


@pytest.mark.parametrize("error_type", [CanbosoError, aiohttp.ClientConnectionError, TimeoutError, OSError])
def test_network_failure_uncertain_and_never_retried(settings, error_type):
    failure = error_type(f"transport failure {KEY}")
    client, transport = client_for(settings, failure)
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is None
    assert_private(caught.value, KEY)
    assert len(transport.calls) == 1


def test_transport_exception_raw_preserved(settings):
    raw = {"partial": "private-response"}
    failure = CanbosoError("safe_transport_error")
    failure.raw = raw
    client, _ = client_for(settings, failure)
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is raw
    assert_private(caught.value, "private-response")


def test_existing_uncertainty_preserves_evidence(settings):
    failure = PurchaseUncertain("safe_error", raw={"partial": "private-response"})
    client, transport = client_for(settings, failure)
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value is failure
    assert len(transport.calls) == 1


def test_cancelled_purchase_is_never_automatically_retried(settings):
    client, transport = client_for(settings, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        purchase(client)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("headers,body,expected", [
    ({"Retry-After": "300"}, {"rateLimit": {"retryAfter": 60}}, 300),
    ({"rEtRy-AfTeR": "1"}, None, 1),
    ({"Retry-After": "2"}, {"rateLimit": {"retryAfter": 900}}, 900),
    ({}, {"rateLimit": {"retryAfter": 12}}, 12),
    ({"Retry-After": "invalid"}, {"rateLimit": {"retryAfter": 300}}, 300),
    ({"Retry-After": "21600"}, {"rateLimit": {"retryAfter": []}}, 21600),
    ({"Retry-After": "120"}, "<html>rate limited</html>", 120),
    ({}, {"rateLimit": []}, 60),
    ({}, {"rateLimit": {"retryAfter": True}}, 60),
    ({"Retry-After": "-2"}, {"rateLimit": {"retryAfter": 0}}, 60),
    ({"Retry-After": "Infinity"}, {"rateLimit": {"retryAfter": float("inf")}}, 60),
])
@pytest.mark.parametrize("operation", ["purchase", "products", "balance"])
def test_429_retry_after_propagated_without_retry(settings, headers, body, expected, operation):
    client, transport = client_for(settings, Reply(429, body, headers))
    with pytest.raises(RateLimited) as caught:
        if operation == "purchase":
            purchase(client)
        else:
            asyncio.run(getattr(client, operation)())
    assert caught.value.retry_after == expected
    assert len(transport.calls) == 1


@pytest.mark.parametrize("path,value", [
    ("productId", None), ("productId", []), ("productId", ""), ("name", None),
    ("productType", "other"), ("productType", []), ("price", []),
    ("price.amount", True), ("price.amount", "50000"), ("price.amount", -1),
    ("price.amount", float("inf")), ("price.currency", []), ("price.currency", "EUR"),
    ("price.text", None), ("availability", None), ("availability", []),
    ("availability.available", True), ("availability.sold", None),
    ("promotions", {}), ("promotions", [None]), ("promotions", [{"minQty": True}]),
    ("purchaseRequirements", None), ("purchaseRequirements", []),
    ("purchaseRequirements", {"customerEmail": "true"}),
    ("purchaseRequirements", {"slotMonths": 1}),
    ("purchaseRequirements", {"quantityFixed": True}),
    ("purchaseRequirements", {"allowedMonths": "1,3"}),
    ("purchaseRequirements", {"allowedMonths": [True]}),
    ("purchaseRequirements", {"allowedMonths": [1.0]}),
])
def test_strict_product_shapes(settings, product, path, value):
    change(product, path, value)
    client, transport = client_for(settings, Reply(200, {"success": True, "products": [product]}))
    with pytest.raises(CanbosoError):
        asyncio.run(client.products())
    assert len(transport.calls) == 1


@pytest.mark.parametrize("field", ["productId", "name", "productType", "price", "availability", "promotions"])
def test_required_product_fields(settings, product, field):
    del product[field]
    client, _ = client_for(settings, Reply(200, {"success": True, "products": [product]}))
    with pytest.raises(CanbosoError):
        asyncio.run(client.products())


def test_catalog_duplicate_ids_rejected(settings, product):
    client, _ = client_for(settings, Reply(200, {"success": True, "products": [product, product]}))
    with pytest.raises(CanbosoError, match="duplicate_product_id"):
        asyncio.run(client.products())


@pytest.mark.parametrize("product_type", ["account", "slot", "upgrade_account"])
def test_documented_catalog_types_and_nullable_stock(settings, product, product_type):
    product.update(productType=product_type, availability={"available": None, "sold": 0})
    client, _ = client_for(settings, Reply(200, {"success": True, "products": [product]}))
    assert asyncio.run(client.products())["products"] == [product]


@pytest.mark.parametrize("body", [None, [], {"success": 1}, {"success": True}, {"success": True, "products": {}}, {"success": True, "products": [None]}])
def test_malformed_product_envelope(settings, body):
    client, _ = client_for(settings, Reply(200, body))
    with pytest.raises(CanbosoError):
        asyncio.run(client.products())


@pytest.mark.parametrize("field,value", [
    ("balance", True), ("balance", "10"), ("balance", None), ("balance", -1),
    ("balance", []), ("balance", float("nan")), ("walletCurrency", []),
    ("walletCurrency", "EUR"), ("usdtBalance", None), ("balanceVnd", True),
])
def test_strict_balance_fields(settings, field, value):
    body = {"success": True, "walletCurrency": "USD", "balance": 12.5, field: value}
    client, _ = client_for(settings, Reply(200, body))
    with pytest.raises(CanbosoError):
        asyncio.run(client.balance())


class FakeResponse:
    def __init__(self, raw, status=200, headers=None, failure=None):
        self.chunks = [raw] if isinstance(raw, bytes) else raw
        self.status = status
        self.headers = headers or {}
        self.failure = failure
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure


def http_client(settings, response=None, failure=None):
    session = Mock()
    session.request = Mock(return_value=response, side_effect=failure)
    transport = HttpTransport(session)
    return CanbosoClient(settings, transport, "production"), session


def test_http_wire_contract_and_redirects_disabled(settings, account_reply):
    response = FakeResponse(json.dumps(account_reply).encode())
    client, session = http_client(settings, response)
    body = request_body()
    assert purchase(client, body).status == "completed"
    args, kwargs = session.request.call_args
    assert args == ("POST", BASE_URL + PURCHASE_PATH)
    assert kwargs["params"] is None
    assert kwargs["json"] is body
    assert kwargs["headers"] == {"Idempotency-Key": IDEMPOTENCY}
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 25
    assert session.request.call_count == 1


@pytest.mark.parametrize("path,method", [(PRODUCTS_PATH, "products"), (BALANCE_PATH, "balance")])
def test_http_get_key_only_in_query(settings, path, method):
    body = {"success": True, "products": [], "balance": 0, "walletCurrency": "VND"}
    client, session = http_client(settings, FakeResponse(json.dumps(body).encode()))
    asyncio.run(getattr(client, method)())
    args, kwargs = session.request.call_args
    assert args == ("GET", BASE_URL + path)
    assert kwargs["params"] == {"key": KEY}
    assert kwargs["json"] is None
    assert kwargs["headers"] is None


@pytest.mark.parametrize("raw", [
    b'{"success":true,"password":"private-password",', b'<html>private-password</html>',
    b'\xff\xfe\x00private-password', b'[]', b'null', b'true', b'1', b'"private-password"', b'',
    b'{"success":true,"success":false}', b'{"success":true,"amount":NaN}',
    b'{"success":true,"amount":Infinity}', b'[' * 1200,
])
def test_raw_http_malformed_evidence_lossless_private_serializable(settings, raw, caplog):
    client, session = http_client(settings, FakeResponse(raw))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    evidence = caught.value.raw
    assert base64.b64decode(evidence["bodyBase64"]) == raw
    assert evidence["status"] == 200
    assert evidence["truncated"] is False
    json.dumps(evidence)
    assert_private(caught.value, "private-password", KEY)
    assert "private-password" not in caplog.text
    assert KEY not in caplog.text
    assert session.request.call_count == 1


def test_raw_http_object_preserved_for_parser_failure(settings):
    body = {"success": True, "order": {"private": "private-password"}}
    client, session = http_client(settings, FakeResponse(json.dumps(body).encode()))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw == body
    assert_private(caught.value, "private-password")
    assert session.request.call_count == 1


@pytest.mark.parametrize("status", [302, 409, 500, 503])
def test_http_unconfirmed_raw_preserved(settings, status):
    raw = b'{"success":false,"message":"private-message"}'
    client, session = http_client(settings, FakeResponse(raw, status=status))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw == json.loads(raw)
    assert_private(caught.value, "private-message")
    assert session.request.call_count == 1


@pytest.mark.parametrize("error_type", [aiohttp.ClientConnectionError, TimeoutError, OSError])
def test_http_partial_response_survives_network_failure(settings, error_type):
    raw = b'{"private-password":"partial'
    response = FakeResponse([raw], failure=error_type(KEY))
    client, session = http_client(settings, response)
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert base64.b64decode(caught.value.raw["bodyBase64"]) == raw
    assert caught.value.raw["truncated"] is True
    assert_private(caught.value, KEY, "private-password")
    assert session.request.call_count == 1


def test_http_failure_before_response_has_no_invented_evidence(settings):
    client, session = http_client(settings, failure=aiohttp.ClientConnectionError(KEY))
    with pytest.raises(PurchaseUncertain) as caught:
        purchase(client)
    assert caught.value.raw is None
    assert_private(caught.value, KEY)
    assert session.request.call_count == 1


def test_oversized_response_preserves_bounded_evidence(settings):
    raw = b'x' * 2_000_001
    client, session = http_client(settings, FakeResponse(raw))
    with pytest.raises(PurchaseUncertain, match="oversized_supplier_response") as caught:
        purchase(client)
    assert base64.b64decode(caught.value.raw["bodyBase64"]) == raw[:2_000_000]
    assert caught.value.raw["truncated"] is True
    assert session.request.call_count == 1


def test_reply_repr_suppresses_body_headers_and_evidence():
    reply = Reply(500, {"password": "private-password"}, {"key": KEY}, {"partial": "private-body"})
    assert repr(reply) == "Reply(status=500)"
    assert reply.review_raw == {"partial": "private-body"}


@pytest.mark.parametrize("method,path", [
    ("GET", PURCHASE_PATH), ("POST", PRODUCTS_PATH), ("POST", BALANCE_PATH),
    ("GET", "/api/v2/telegram-buyer/order-status"), ("POST", "/api/v2/telegram-buyer/refund"),
])
def test_no_invented_endpoints(method, path):
    session = Mock()
    with pytest.raises(CanbosoError, match="unsupported_endpoint"):
        asyncio.run(HttpTransport(session).request(method, path))
    session.request.assert_not_called()


def test_http_throttles_across_endpoints_without_retries(settings, monkeypatch):
    response = FakeResponse(b'{"success":true}')
    session = Mock(request=Mock(return_value=response))
    transport = HttpTransport(session)
    monkeypatch.setattr("shop.canboso.time.monotonic", lambda: 100.0)
    sleep = AsyncMock()
    monkeypatch.setattr("shop.canboso.asyncio.sleep", sleep)

    async def run():
        await transport.request("GET", PRODUCTS_PATH, query={"key": KEY})
        await transport.request("GET", BALANCE_PATH, query={"key": KEY})

    asyncio.run(run())
    sleep.assert_awaited_once()
    assert sleep.call_args.args[0] == pytest.approx(2.2)
    assert session.request.call_count == 2


@pytest.mark.parametrize("value", [True, None, "NaN", "Infinity", -1, [], {}])
def test_money_rejects_invalid_amounts(value):
    with pytest.raises(CanbosoError):
        money(value)


def test_configuration_money_strings_remain_supported():
    assert money("18.259259") == Decimal("18.259259")
    assert money(0) == 0
    with pytest.raises(CanbosoError):
        money(0, positive=True)


def test_email_validation_keeps_existing_normalization():
    assert valid_email(" buyer+slot@example.com ") == "buyer+slot@example.com"
