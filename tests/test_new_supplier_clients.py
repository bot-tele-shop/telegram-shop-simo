"""Jaha Digital, Elite Digital Emporium and Acczone client behavior:
normalization into the shared internal shapes, documented error mapping,
idempotency discipline and read-only recovery. No live network, ever."""
import asyncio
from collections import deque
from datetime import datetime, timezone

import aiohttp
import pytest

from shop import acczone, elite_emporium, jaha_digital, providers
from shop.canboso import (
    PurchaseRejected,
    PurchaseUncertain,
    RateLimited,
    Reply,
)
from shop.config import SupplierSettings
from shop.errors import ShopError

IDEMPOTENCY = "ds-order-0001-test"


def settings_for(name):
    return SupplierSettings(
        provider=name, enabled=True, api_key=f"TEST_ONLY_{name.upper()}_KEY_1234",
        allow_purchases=True, resale_authorized=True, acknowledge_price_race=True,
        budget_currency="USD", spend_budget="100",
    )


class FakeTransport:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.calls = []

    async def request(self, method, path, *, query=None, body=None, headers=None, **extra):
        self.calls.append({"method": method, "path": path, "query": query,
                           "body": body, "headers": headers, **extra})
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(coro):
    return asyncio.run(coro)


# --- Jaha Digital -----------------------------------------------------------

def jaha_product(code="jaha_001", *, status="available", available=5, buyer_input=None,
                 price="8.5000", min_quantity=1):
    return {
        "code": code, "title": "Gemini Advanced", "variant": "1 month",
        "description": "Activation", "period": "1m", "category": "AI",
        "subcategory": None, "price_usdt": price, "available": available,
        "min_quantity": min_quantity, "max_quantity": 10, "delivery_type": "automatic",
        "buyer_input": buyer_input if buyer_input is not None else {"required": False, "fields": []},
        "status": status, "updated_at": None,
    }


def jaha_order(number="JD-1001", *, status="completed", code="jaha_001",
               external="ds-order1", delivered=1, delivery="CODE-XYZ", total="8.5000"):
    return {"order": {
        "order_number": number, "external_order_id": external, "product_code": code,
        "status": status, "quantity": 1, "delivered_quantity": delivered,
        "unit_price_usdt": total, "total_usdt": total, "refunded_usdt": "0",
        "delivery": delivery, "instructions": None, "buyer_input_received": False,
        "created_at": None, "updated_at": None,
    }}


def jaha_client(*replies):
    transport = FakeTransport(*replies)
    return jaha_digital.JahaClient(settings_for("jaha_digital"), transport, "production"), transport


def test_jaha_products_normalize_and_paginate():
    page1 = Reply(200, {"products": [jaha_product("a1")], "next_cursor": "cursor-abc"})
    page2 = Reply(200, {"products": [jaha_product("b2", status="out_of_stock", available=7)],
                        "next_cursor": None})
    client, transport = jaha_client(page1, page2)
    result = run(client.products())
    by_id = {p["productId"]: p for p in result["products"]}
    assert set(by_id) == {"a1", "b2"}
    assert by_id["a1"]["price"] == {"amount": 8.5, "currency": "USD", "text": "USDT 8.5000"} or \
        str(by_id["a1"]["price"]["amount"]) == "8.5000"
    assert by_id["a1"]["price"]["currency"] == "USD"  # USDT normalized at the boundary
    assert by_id["a1"]["availability"]["available"] == 5
    assert by_id["b2"]["availability"]["available"] == 0  # out_of_stock overrides the count
    assert by_id["a1"]["purchaseRequirements"] == {"quantityFixed": 1}
    assert transport.calls[0]["query"] == {"limit": 100, "current_only": "true"}
    assert transport.calls[1]["query"]["cursor"] == "cursor-abc"
    assert transport.calls[0]["headers"]["Authorization"].startswith("Bearer ")


def test_jaha_buyer_input_maps_to_email_collection_flow():
    email_input = {"required": True, "type": "email", "scope": "per_unit",
                   "prompt": "Enter email", "max_total_length": 400,
                   "fields": [{"name": "email", "type": "email", "required": True, "max_length": 320}]}
    client, _ = jaha_client(Reply(200, {"products": [jaha_product(buyer_input=email_input)],
                                        "next_cursor": None}))
    product = run(client.products())["products"][0]
    assert product["productType"] == "slot"
    assert product["purchaseRequirements"]["customerEmail"] is True


def test_jaha_unsupported_buyer_input_is_flagged_not_guessed():
    text_input = {"required": True, "type": "text", "scope": "per_order", "prompt": "x",
                  "max_total_length": 100, "fields": [{"name": "text", "type": "text",
                                                       "required": True, "max_length": 100}]}
    client, _ = jaha_client(Reply(200, {"products": [jaha_product(buyer_input=text_input)],
                                        "next_cursor": None}))
    product = run(client.products())["products"][0]
    assert product["productType"] == "account"
    assert product["purchaseRequirements"]["buyerInput"] is True


def test_jaha_balance_normalizes_usdt():
    client, _ = jaha_client(Reply(200, {"account": {
        "client_id": "c1", "balance_usdt": "451.8800", "currency": "USDT",
        "status": "active", "language": "en", "terms_version": "v1",
        "purchase_amount_limit_usdt": None, "daily_turnover_limit_usdt": None}}))
    balance = run(client.balance())
    assert str(balance["balance"]) == "451.8800"
    assert balance["walletCurrency"] == "USD"


def test_jaha_purchase_sends_idempotency_key_and_price_cap():
    client, transport = jaha_client(
        Reply(200, {"products": [jaha_product()], "next_cursor": None}),
        Reply(201, jaha_order()))
    run(client.products())
    spec = {"product_id": "jaha_001", "product_type": "account", "max_cost": "10"}
    body = jaha_digital.build_purchase_body(client.settings, spec, None, order_id="order1")
    result = run(client.purchase(body, IDEMPOTENCY))
    call = transport.calls[1]
    assert call["method"] == "POST" and call["path"] == "/v1/orders"
    assert call["headers"]["Idempotency-Key"] == IDEMPOTENCY
    assert call["body"] == {"product_code": "jaha_001", "quantity": 1,
                            "max_unit_price_usdt": "10", "external_order_id": "ds-order1"}
    assert result.status == "completed"
    assert result.reference == "JD-1001"
    assert result.payload == "CODE-XYZ"
    assert str(result.amount) == "8.5000"
    assert result.currency == "USD"


def test_jaha_processing_order_is_pending_not_completed():
    client, _ = jaha_client(Reply(201, jaha_order(status="processing", delivered=0,
                                                  delivery=None, external=None)))
    body = {"product_code": "jaha_001", "quantity": 1, "max_unit_price_usdt": "10"}
    result = run(client.purchase(body, IDEMPOTENCY))
    assert result.status == "pending"
    assert result.payload == ""


def test_jaha_error_mapping():
    # price_changed: documented rejection, a retry needs a new key and body.
    client, _ = jaha_client(Reply(409, {"status": 409, "code": "price_changed",
                                        "detail": "x", "request_id": "r1"}))
    with pytest.raises(PurchaseRejected, match="price_changed"):
        run(client.purchase({"product_code": "jaha_001", "quantity": 1,
                             "max_unit_price_usdt": "10"}, IDEMPOTENCY))
    # request_in_progress: the first attempt may still debit; uncertain.
    client, _ = jaha_client(Reply(409, {"status": 409, "code": "request_in_progress",
                                        "detail": "x", "request_id": "r2"}))
    with pytest.raises(PurchaseUncertain):
        run(client.purchase({"product_code": "jaha_001", "quantity": 1,
                             "max_unit_price_usdt": "10"}, IDEMPOTENCY))
    # Validation happens before any debit.
    client, _ = jaha_client(Reply(422, {"status": 422, "code": "validation_error",
                                        "detail": "x", "request_id": "r3"}))
    with pytest.raises(PurchaseRejected):
        run(client.purchase({"product_code": "jaha_001", "quantity": 1,
                             "max_unit_price_usdt": "10"}, IDEMPOTENCY))
    # Rate limit carries Retry-After.
    client, _ = jaha_client(Reply(429, {"detail": "x"}, {"Retry-After": "17"}))
    with pytest.raises(RateLimited) as caught:
        run(client.products())
    assert caught.value.retry_after == 17
    # A timeout mid-purchase is uncertain, never auto-retried.
    client, _ = jaha_client(TimeoutError())
    with pytest.raises(PurchaseUncertain, match="uncertain"):
        run(client.purchase({"product_code": "jaha_001", "quantity": 1,
                             "max_unit_price_usdt": "10"}, IDEMPOTENCY))


def test_jaha_recovers_an_uncertain_purchase_from_history():
    history = Reply(200, {"orders": [jaha_order("JD-9", external=IDEMPOTENCY)["order"]],
                          "next_cursor": None})
    detail = Reply(200, jaha_order("JD-9", external=IDEMPOTENCY))
    client, _ = jaha_client(history, detail)
    intent = {"supplier_reference": "", "idempotency_key": IDEMPOTENCY,
              "product_id": "jaha_001"}
    result = run(client.recover_uncertain(intent))
    assert result is not None and result.reference == "JD-9"
    assert result.status == "completed"


def test_jaha_recovery_confirms_failed_orders_and_ignores_unknown_keys():
    failed = jaha_order("JD-10", status="failed", delivered=0, delivery=None,
                        external=IDEMPOTENCY)["order"]
    client, _ = jaha_client(Reply(200, {"orders": [failed], "next_cursor": None}))
    with pytest.raises(PurchaseRejected, match="not_fulfilled"):
        run(client.recover_uncertain({"supplier_reference": "", "idempotency_key": IDEMPOTENCY}))
    client, _ = jaha_client(Reply(200, {"orders": [], "next_cursor": None}))
    assert run(client.recover_uncertain({"supplier_reference": "",
                                         "idempotency_key": IDEMPOTENCY})) is None


def test_jaha_mapping_rules():
    providers.validate_spec({"provider": "jaha_digital", "product_id": "abc",
                             "product_type": "account"})
    with pytest.raises(ShopError, match="slot_months"):
        providers.validate_spec({"provider": "jaha_digital", "product_id": "abc",
                                 "product_type": "account", "slot_months": 3})


# --- Elite Digital Emporium ---------------------------------------------------

def elite_product(pid=7, *, price="4.25", stock=12, name="ChatGPT Plus"):
    return {"id": pid, "name": name, "price": price, "stock": stock}


def elite_order(pid=7, *, credentials=None, total="4.25"):
    return {"order": {"id": 551, "product_id": pid, "quantity": 1, "total": total,
                      "credentials": credentials if credentials is not None else
                      [{"login": "user@example.com", "password": "secret"}],
                      "status": "completed"}}


def elite_client(*replies):
    transport = FakeTransport(*replies)
    return elite_emporium.EliteClient(settings_for("elite_emporium"), transport, "production"), transport


def test_elite_products_normalize_bare_and_enveloped_lists():
    client, _ = elite_client(Reply(200, [elite_product(7), elite_product(8, stock=0)]))
    result = run(client.products())
    by_id = {p["productId"]: p for p in result["products"]}
    assert by_id["7"]["availability"]["available"] == 12
    assert by_id["8"]["availability"]["available"] == 0
    assert by_id["7"]["price"]["currency"] == "USD"
    client, _ = elite_client(Reply(200, {"data": [elite_product(9)]}))
    assert run(client.products())["products"][0]["productId"] == "9"


def test_elite_balance():
    client, _ = elite_client(Reply(200, {"balance": "120.50", "credit_limit": "50",
                                         "amount_owed": "0"}))
    balance = run(client.balance())
    assert str(balance["balance"]) == "120.50"
    assert balance["walletCurrency"] == "USD"


def test_elite_purchase_carries_idempotency_and_normalizes_credentials():
    client, transport = elite_client(Reply(200, [elite_product()]), Reply(200, elite_order()))
    run(client.products())
    spec = {"product_id": "7", "product_type": "account", "max_cost": "10"}
    body = elite_emporium.build_purchase_body(client.settings, spec, None, order_id="order1")
    result = run(client.purchase(body, "ds-order1"))
    call = transport.calls[1]
    assert call["body"]["idempotency_key"] == "ds-order1"
    assert call["body"]["product_id"] == 7  # Documented as an integer
    assert result.status == "completed"
    assert result.reference == "551"
    assert "user@example.com" in result.payload
    assert str(result.amount) == "4.25"


def test_elite_purchase_refuses_a_rebuilt_idempotency_key():
    client, _ = elite_client(Reply(200, elite_order()))
    body = {"product_id": 7, "quantity": 1, "idempotency_key": "ds-order1"}
    with pytest.raises(Exception, match="idempotency_key_mismatch"):
        run(client.purchase(body, "ds-order2"))


def test_elite_error_mapping():
    client, _ = elite_client(Reply(402, {"message": "insufficient"}))
    with pytest.raises(PurchaseRejected):
        run(client.purchase({"product_id": 7, "quantity": 1,
                             "idempotency_key": IDEMPOTENCY}, IDEMPOTENCY))
    client, _ = elite_client(Reply(409, {"message": "out of stock"}))
    with pytest.raises(PurchaseRejected):
        run(client.purchase({"product_id": 7, "quantity": 1,
                             "idempotency_key": IDEMPOTENCY}, IDEMPOTENCY))
    client, _ = elite_client(aiohttp.ClientError("connection reset"))
    with pytest.raises(PurchaseUncertain):
        run(client.purchase({"product_id": 7, "quantity": 1,
                             "idempotency_key": IDEMPOTENCY}, IDEMPOTENCY))


def test_elite_order_lookup():
    client, _ = elite_client(Reply(200, elite_order()))
    result = run(client.lookup_order("551"))
    assert result is not None and result.reference == "551"
    client, _ = elite_client(Reply(404, {"message": "not found"}))
    assert run(client.lookup_order("999")) is None
    # Without a captured reference there is no documented history endpoint.
    client, _ = elite_client()
    assert run(client.recover_uncertain({"supplier_reference": ""})) is None


def test_elite_mapping_rules():
    providers.validate_spec({"provider": "elite_emporium", "product_id": "42",
                             "product_type": "account"})
    with pytest.raises(ShopError, match="numeric"):
        providers.validate_spec({"provider": "elite_emporium", "product_id": "abc",
                                 "product_type": "account"})


# --- Acczone -----------------------------------------------------------------

def acczone_service(key="gemini", *, price=0.4, active=1, name="Gemini Link"):
    return {"key": key, "name": name, "price": price, "is_active": active,
            "created_at": "2026-09-01 12:25:23"}


def acczone_record(rid=12345, *, key="gemini", code="ACTIVATION-CODE",
                   used_at="2026-09-19 04:00:00"):
    return {"id": rid, "service_key": key, "code_type": "text", "code_value": code,
            "is_used": 1, "used_by": 999, "used_at": used_at, "extracted_code": None}


def acczone_client(*replies):
    transport = FakeTransport(*replies)
    return acczone.AcczoneClient(settings_for("acczone"), transport, "production"), transport


def test_acczone_services_normalize_with_conservative_availability():
    client, _ = acczone_client(Reply(200, [acczone_service(), acczone_service("off", active=0)]))
    result = run(client.products())
    by_id = {p["productId"]: p for p in result["products"]}
    # No documented stock counter: active means one outstanding order at a time.
    assert by_id["gemini"]["availability"]["available"] == 1
    assert by_id["off"]["availability"]["available"] == 0
    assert str(by_id["gemini"]["price"]["amount"]) == "0.4"
    assert by_id["gemini"]["purchaseRequirements"] == {"quantityFixed": 1}


def test_acczone_balance():
    client, _ = acczone_client(Reply(200, {"user_id": 1, "username": "x", "balance": 451.88,
                                           "apikey": "KEY"}))
    balance = run(client.balance())
    assert str(balance["balance"]) == "451.88"
    assert balance["walletCurrency"] == "USD"


def test_acczone_purchase_sends_key_only_in_query_and_parses_record():
    client, transport = acczone_client(Reply(200, [acczone_service()]),
                                       Reply(200, [acczone_record()]))
    run(client.products())
    body = acczone.build_purchase_body(client.settings,
                                       {"product_id": "gemini", "product_type": "account",
                                        "max_cost": "5"}, None)
    assert "apikey" not in body and "key" not in body  # Stored bodies carry no credentials
    result = run(client.purchase(body, IDEMPOTENCY))
    call = transport.calls[1]
    assert call["path"] == "/buyCpn"
    assert call["query"]["service_key"] == "gemini"
    assert call["query"]["apikey"] == settings_for("acczone").api_key
    assert result.status == "completed"
    assert result.reference == "12345"
    assert result.payload == "ACTIVATION-CODE"
    assert str(result.amount) == "0.4"


def test_acczone_error_mapping():
    client, _ = acczone_client(Reply(400, {"detail": "out of stock"}))
    with pytest.raises(PurchaseRejected):
        run(client.purchase({"service_key": "gemini", "quantity": 1}, IDEMPOTENCY))
    client, _ = acczone_client(Reply(429, {"detail": "slow down"}))
    with pytest.raises(RateLimited) as caught:
        run(client.products())
    assert caught.value.retry_after >= 2
    client, _ = acczone_client(Reply(200, []))
    with pytest.raises(PurchaseUncertain):
        run(client.purchase({"service_key": "gemini", "quantity": 1}, IDEMPOTENCY))
    client, _ = acczone_client(TimeoutError())
    with pytest.raises(PurchaseUncertain):
        run(client.purchase({"service_key": "gemini", "quantity": 1}, IDEMPOTENCY))


def test_acczone_recovery_adopts_exactly_one_matching_history_record():
    used = datetime(2026, 9, 19, 4, 0, 0, tzinfo=timezone.utc).timestamp()
    intent = {"product_id": "gemini", "first_sent_at": used - 30,
              "created_at": used - 60, "supplier_reference": ""}
    client, _ = acczone_client(Reply(200, [acczone_service()]), Reply(200, [acczone_record()]))
    run(client.products())
    result = run(client.recover_uncertain(intent))
    assert result is not None and result.reference == "12345"
    assert result.payload == "ACTIVATION-CODE"
    # Zero matches: nothing to adopt.
    client, _ = acczone_client(Reply(200, [acczone_service()]), Reply(200, []))
    run(client.products())
    assert run(client.recover_uncertain(intent)) is None
    # Two matches: ambiguous, stays uncertain for an operator.
    client, _ = acczone_client(Reply(200, [acczone_service()]),
                               Reply(200, [acczone_record(1), acczone_record(2)]))
    run(client.products())
    assert run(client.recover_uncertain(intent)) is None
    # Records from before the purchase do not count.
    old = acczone_record(3, used_at="2026-09-18 04:00:00")
    client, _ = acczone_client(Reply(200, [acczone_service()]), Reply(200, [old]))
    run(client.products())
    assert run(client.recover_uncertain(intent)) is None


def test_acczone_mapping_rules():
    providers.validate_spec({"provider": "acczone", "product_id": "gemini",
                             "product_type": "account"})
    with pytest.raises(ShopError, match="service key"):
        providers.validate_spec({"provider": "acczone", "product_id": "BAD KEY",
                                 "product_type": "account"})


def test_provider_transports_allowlist_documented_endpoints_only():
    import shop.acczone as acc
    import shop.elite_emporium as elite
    import shop.jaha_digital as jaha
    for module, path in ((jaha, "/v1/admin"), (elite, "/api/telegram-buyer/admin"),
                         (acc, "/admin")):
        transport = module.HttpTransport(None)
        with pytest.raises(Exception, match="unsupported_endpoint"):
            run(transport.request("GET", path))
