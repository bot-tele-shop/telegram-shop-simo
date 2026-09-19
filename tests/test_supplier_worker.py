import asyncio
import copy

import pytest
from test_supplier_store import (
    BUYER_KEY,
    EMAIL,
    EVIDENCE,
    PASSWORD,
    PRODUCT_IDS,
    STARS,
    purchase_reply,
)
from test_supplier_store import supplier_shop as supplier_shop

from shop.canboso import BALANCE_PATH, PRODUCTS_PATH, PURCHASE_PATH, PurchaseResult, Reply, money
from shop.store import ShopError


def assert_no_delivery(h, order):
    assert order["id"] not in h.store.due_deliveries()
    assert h.store.claim_delivery(order["id"]) is None
    assert not h.rows("stock")
    h.bot.send_document.assert_not_awaited()
    with pytest.raises(ShopError):
        h.store.retrieve_delivery(order["id"], order["user_id"])


def test_synchronization_persists_validated_encrypted_snapshots_without_spending(supplier_shop):
    h = supplier_shop
    snapshots = {row["name"]: row for row in h.rows("supplier_cache")}
    assert set(snapshots) == {"canboso:products", "canboso:balance"}
    for name, expected in (("canboso:products", h.transport.catalog),
                           ("canboso:balance", h.transport.wallet)):
        row = snapshots[name]
        assert row["key_hash"] == h.settings.key_fingerprint
        assert row["fetched_at"] == h.clock[0]
        assert h.store.supplier.decrypt(row["ciphertext"]) == expected
        assert "walletCurrency" not in row["ciphertext"]
    assert [(method, path) for method, path, _ in h.transport.calls] == [
        ("GET", PRODUCTS_PATH), ("GET", BALANCE_PATH),
    ]
    assert not h.transport.posts
    h.bot.send_message.assert_not_awaited()
    h.bot.refund_star_payment.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["product_name", "numeric_balance"])
def test_failed_synchronization_does_not_partially_replace_cache(supplier_shop, invalid):
    h = supplier_shop
    before = h.rows("supplier_cache")
    h.clock[0] += 1
    if invalid == "product_name":
        del h.transport.catalog["products"][0]["name"]
    else:
        h.transport.catalog["products"][0]["price"]["amount"] = 9
        h.transport.wallet["balance"] = "100"
    assert asyncio.run(h.worker.synchronize()) is False
    assert h.rows("supplier_cache") == before
    assert h.store.supplier.cooldown_until() > h.clock[0]
    assert not h.transport.posts
    h.restart()
    assert h.rows("supplier_cache") == before
    assert asyncio.run(h.worker.synchronize()) is False


def test_background_tick_synchronizes_before_post_and_leaves_delivery_to_real_worker(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(purchase_reply())
    h.worker.last_sync = float("-inf")
    start = len(h.transport.calls)
    asyncio.run(h.worker.tick())
    assert [(method, path) for method, path, _ in h.transport.calls[start:]] == [
        ("GET", PRODUCTS_PATH), ("GET", BALANCE_PATH), ("POST", PURCHASE_PATH),
    ]
    assert h.intent(order)["state"] == "completed"
    assert h.store.get_order(order["id"])["state"] == "paid"
    h.bot.send_message.assert_not_awaited()
    assert h.store.due_deliveries() == [order["id"]]
    asyncio.run(h.delivery.tick())
    assert h.store.get_order(order["id"])["state"] == "delivered"
    assert h.store.get_order(order["id"])["message_id"] == 4242
    h.bot.send_message.assert_awaited_once()
    sent = h.bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 101 and sent["protect_content"] is True
    assert PASSWORD in sent["text"] and BUYER_KEY not in sent["text"]
    assert "Login: offline-login@example.invalid" in h.store.retrieve_delivery(order["id"], 101)
    with pytest.raises(ShopError):
        h.store.retrieve_delivery(order["id"], 102)
    asyncio.run(h.delivery.tick())
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1
    assert len(h.rows("stock")) == 1
    h.bot.send_message.assert_awaited_once()


def test_ticks_before_payment_never_purchase_even_after_precheckout(supplier_shop):
    h = supplier_shop
    order = h.new_order()
    asyncio.run(h.worker.tick())
    h.approve(order)
    asyncio.run(h.worker.tick())
    assert not h.transport.posts
    assert h.intent(order)["state"] == "draft"
    assert_no_delivery(h, order)


def test_duplicate_payments_before_and_after_restart_make_one_purchase(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(purchase_reply())
    for _ in range(3):
        assert h.pay(order).duplicate is True
    asyncio.run(h.worker.tick())
    h.restart()
    assert h.pay(order).duplicate is True
    asyncio.run(h.worker.tick())
    asyncio.run(h.delivery.tick())
    assert len(h.transport.posts) == 1
    assert len(h.rows("supplier_intents")) == len(h.rows("stock")) == 1
    assert len(h.rows("payment_events")) == 1
    assert h.intent(order)["attempts"] == 1
    assert h.store.get_order(order["id"])["state"] == "delivered"


@pytest.mark.parametrize("sku", ["slot", "business"])
def test_slot_exact_email_body_and_idempotency_key_survive_restart(supplier_shop, sku):
    h = supplier_shop
    order = h.paid_order(sku, email=EMAIL)
    saved = h.intent(order)
    h.restart()
    h.transport.outcomes.append(purchase_reply(sku))
    asyncio.run(h.worker.tick())
    expected = {"key": BUYER_KEY, "product_id": PRODUCT_IDS[sku], "quantity": 1,
                "customer_email": EMAIL}
    if sku == "business":
        expected["slot_months"] = 3
    assert h.transport.posts == [("POST", PURCHASE_PATH, {
        "query": None, "body": expected, "headers": {"Idempotency-Key": saved["idempotency_key"]},
    })]
    assert h.intent(order)["state"] == "completed"
    assert asyncio.run(h.delivery.deliver(order["id"])) is True
    assert EMAIL in h.store.retrieve_delivery(order["id"], 101)
    assert h.store.get_order(order["id"])["state"] == "delivered"


def test_pending_slot_never_replays_delivers_or_refunds_without_operator_evidence(supplier_shop):
    h = supplier_shop
    order = h.paid_order("slot", email=EMAIL)
    h.transport.outcomes.append(purchase_reply("slot", pending=True))
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "pending"
    assert h.intent(order)["supplier_reference"] == "offline-reference-slot"
    event = h.store.payment_for_order(order["id"])
    for _ in range(3):
        assert_no_delivery(h, order)
        with pytest.raises(ShopError, match="pending|uncertain"):
            h.store.begin_refund(event["id"])
        with pytest.raises(ShopError, match="pending"):
            h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
        h.clock[0] += 3600
        h.restart()
        asyncio.run(h.worker.tick())
        asyncio.run(h.delivery.tick())
    assert len(h.transport.posts) == 1
    assert h.intent(order)["state"] == "pending"
    assert h.intent(order)["attempts"] == 1
    assert h.store.get_payment(event["id"])["status"] == "accepted"
    assert h.store.get_order(order["id"])["state"] == "paid"
    h.bot.refund_star_payment.assert_not_awaited()
    for call in h.bot.send_message.call_args_list:
        assert call.args[0] == 999
        assert EMAIL not in str(call) and BUYER_KEY not in str(call)


def test_pending_slot_can_be_fulfilled_only_with_audited_manual_confirmation(supplier_shop):
    h = supplier_shop
    order = h.paid_order("slot", email=EMAIL)
    h.transport.outcomes.append(purchase_reply("slot", pending=True))
    asyncio.run(h.worker.tick())
    h.store.supplier.resolve(order["id"], "fulfill", "Offline supplier confirmed invitation received.",
                             "offline-operator", delivery="Verified invitation for " + EMAIL)
    assert asyncio.run(h.delivery.deliver(order["id"])) is True
    assert h.store.retrieve_delivery(order["id"], 101) == "Verified invitation for " + EMAIL
    assert len(h.rows("supplier_audit")) == 1
    assert len(h.transport.posts) == 1
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1


@pytest.mark.parametrize("failure", ["timeout", "server_error", "idempotency_conflict"])
def test_uncertain_purchase_is_held_across_ticks_and_restart(supplier_shop, failure):
    h = supplier_shop
    order = h.paid_order()
    outcomes = {
        "timeout": TimeoutError("TEST_ONLY_PRIVATE_TRANSPORT_DETAIL"),
        "server_error": Reply(503, {"success": False, "message": "private-supplier-detail"}),
        "idempotency_conflict": Reply(409, {"success": False, "message": "purchase may be in progress"}),
    }
    h.transport.outcomes.append(outcomes[failure])
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "uncertain"
    assert h.intent(order)["budget_held"] == 1
    assert_no_delivery(h, order)
    with pytest.raises(ShopError):
        h.store.retry_order(order["id"])
    with pytest.raises(ShopError):
        h.store.begin_refund(h.store.payment_for_order(order["id"])["id"])
    h.clock[0] += 86400
    h.restart()
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1
    assert h.intent(order)["attempts"] == 1
    assert h.store.get_order(order["id"])["state"] == "paid"


def test_held_recovery_reports_hold_and_backs_off(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(TimeoutError("TEST_ONLY_PRIVATE_TRANSPORT_DETAIL"))
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "uncertain"
    # A lookup result priced above the cap must be held for an operator, and
    # a held intent must back off instead of re-recovering every pass.
    over_cap = money(h.intent(order)["max_cost"]) + money("1")
    result = PurchaseResult("offline-reference-account", "completed", over_cap, "USD",
                            "CODE-XYZ", {"order": {"orderCode": "offline-reference-account"}},
                            product_type="account")
    hold = h.store.supplier.complete_recovered(order["id"], result)
    assert hold == "supplier_price_or_currency_changed"
    row = h.intent(order)
    assert row["state"] == "uncertain" and row["hold_reason"] == hold
    assert row["next_attempt_at"] > h.clock[0]
    assert_no_delivery(h, order)


def test_cancelled_inflight_post_recovers_as_uncertain_without_replay(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    saved = h.intent(order)
    h.transport.outcomes.append(purchase_reply())

    async def interrupt_after_post(body, headers):
        assert h.intent(order)["state"] == "processing"
        assert h.intent(order)["attempts"] == 1
        assert h.intent(order)["lease_until"] > h.clock[0]
        raise asyncio.CancelledError()

    h.transport.on_post = interrupt_after_post
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(h.worker.purchase_one())
    h.transport.on_post = None
    assert len(h.transport.posts) == 1
    h.restart()
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "processing"
    h.clock[0] += 181
    h.restart()
    asyncio.run(h.worker.tick())
    row = h.intent(order)
    assert row["state"] == "uncertain" and row["hold_reason"] == "process_interrupted"
    assert row["attempts"] == 1 and row["budget_held"] == 1
    assert row["request_ciphertext"] == saved["request_ciphertext"]
    assert row["idempotency_key"] == saved["idempotency_key"]
    assert_no_delivery(h, order)
    assert h.pay(order).duplicate is True
    assert len(h.transport.posts) == 1
    with pytest.raises(ShopError):
        h.store.begin_refund(h.store.payment_for_order(order["id"])["id"])


@pytest.mark.parametrize("malformed", [False, True])
def test_raw_response_request_and_delivery_are_encrypted_on_disk(supplier_shop, malformed, caplog):
    h = supplier_shop
    order = h.paid_order()
    reply = purchase_reply()
    if malformed:
        reply.body["payment"]["amount"] = "8"
        reply.body["privateEvidence"] = "TEST_ONLY_PRIVATE_RAW_EVIDENCE"
    raw = copy.deepcopy(reply.body)
    h.transport.outcomes.append(reply)
    asyncio.run(h.worker.tick())
    row = h.intent(order)
    assert row["state"] == ("uncertain" if malformed else "completed")
    assert h.store.supplier.decrypt(row["response_ciphertext"]) == raw
    assert h.store.supplier.inspect(order["id"])["response"] == raw
    secrets = [BUYER_KEY, PASSWORD, "offline-login@example.invalid", "TEST_ONLY_PRIVATE_RAW_EVIDENCE"]
    persisted = str(h.rows("supplier_intents") + h.rows("stock") + h.rows("supplier_cache"))
    for secret in secrets:
        assert secret not in persisted
        assert secret not in caplog.text
        for path in h.store.path.parent.glob("supplier.sqlite3*"):
            assert secret.encode() not in path.read_bytes()
    if malformed:
        assert_no_delivery(h, order)
    else:
        stock, = h.rows("stock")
        assert PASSWORD in h.store.cipher.decrypt(stock["ciphertext"].encode()).decode()
    h.restart()
    assert h.store.supplier.inspect(order["id"])["response"] == raw


def test_queued_local_refund_prevents_worker_post(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.store.begin_refund(h.store.payment_for_order(order["id"])["id"])
    asyncio.run(h.worker.tick())
    assert not h.transport.posts
    assert h.intent(order)["state"] == "cancelled"
    assert_no_delivery(h, order)


def test_external_refund_during_post_prevents_delivery_even_on_completed_response(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    charge = h.store.get_order(order["id"])["charge_id"]
    h.transport.outcomes.append(purchase_reply())

    async def external_refund(body, headers):
        assert h.intent(order)["state"] == "processing"
        h.store.record_refund(charge, 101, "XTR", STARS)
        assert h.store.get_order(order["id"])["state"] == "refunded"

    h.transport.on_post = external_refund
    asyncio.run(h.worker.tick())
    assert h.store.get_order(order["id"])["state"] == "refunded"
    assert h.intent(order)["state"] == "uncertain"
    assert h.intent(order)["hold_reason"] == "customer_payment_no_longer_payable"
    assert h.intent(order)["actual_cost"] == "8"
    assert h.intent(order)["budget_held"] == 1
    assert_no_delivery(h, order)
    asyncio.run(h.delivery.tick())
    h.clock[0] += 181
    h.restart()
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1
    with pytest.raises(ShopError):
        h.store.supplier.resolve(order["id"], "fulfill", EVIDENCE, "offline-operator", delivery="late item")


@pytest.mark.parametrize("amount,currency", [(10.01, "USD"), (8, "VND")])
def test_actual_supplier_cost_or_currency_breach_is_held_not_delivered(supplier_shop, amount, currency):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(purchase_reply(amount=amount, currency=currency))
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "uncertain"
    assert h.intent(order)["hold_reason"] == "supplier_price_or_currency_changed"
    assert h.intent(order)["actual_cost"] == str(amount)
    assert h.intent(order)["budget_held"] == 1
    assert_no_delivery(h, order)
    with pytest.raises(ShopError):
        h.store.begin_refund(h.store.payment_for_order(order["id"])["id"])
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1


def test_fresh_price_increase_blocks_paid_order_before_supplier_debit(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.catalog["products"][0]["price"]["amount"] = 11
    h.worker.last_sync = float("-inf")
    asyncio.run(h.worker.tick())
    assert not h.transport.posts
    assert h.intent(order)["state"] == "blocked"
    assert h.intent(order)["attempts"] == 0
    assert_no_delivery(h, order)
    event = h.store.payment_for_order(order["id"])
    assert h.store.get_payment(event["id"])["status"] == "accepted"
    h.store.begin_refund(event["id"])
    assert h.intent(order)["state"] == "cancelled"


@pytest.mark.parametrize("limit", ["wallet", "budget"])
def test_completed_purchase_still_counts_against_old_wallet_or_cumulative_budget(supplier_shop, limit):
    h = supplier_shop
    if limit == "wallet":
        h.transport.wallet["balance"] = 15
    else:
        h.configure(spend_budget="15")
    h.sync()
    first = h.new_order()
    second = h.new_order(user=102)
    h.approve(first)
    h.pay(first)
    h.transport.outcomes.append(purchase_reply())
    assert asyncio.run(h.worker.purchase_one()) is True
    assert h.intent(first)["actual_cost"] == "8"
    if limit == "budget":
        h.clock[0] += 1
        h.sync()
    with pytest.raises(ShopError, match=limit):
        h.approve(second)
    assert h.intent(second)["attempts"] == 0
    assert len(h.transport.posts) == 1


def test_operator_retry_reuses_exact_saved_body_and_key_despite_mapping_change(supplier_shop):
    h = supplier_shop
    order = h.paid_order("business", email=EMAIL)
    h.transport.outcomes.append(TimeoutError("mock POST interruption"))
    asyncio.run(h.worker.tick())
    saved = h.intent(order)
    first_post = copy.deepcopy(h.transport.posts[0])
    product = h.product("business")
    product["supplier"]["slot_months"] = 12
    product["price_stars"] = 99
    h.store.upsert_product(product)
    h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
    h.restart()
    h.transport.outcomes.append(purchase_reply("business"))
    asyncio.run(h.worker.tick())
    assert h.transport.posts == [first_post, first_post]
    assert h.intent(order)["request_ciphertext"] == saved["request_ciphertext"]
    assert h.intent(order)["idempotency_key"] == saved["idempotency_key"]
    assert h.intent(order)["attempts"] == 2
    assert h.intent(order)["state"] == "completed"
    assert h.store.get_order(order["id"])["price_stars"] == STARS
    audit, = h.rows("supplier_audit")
    assert h.store.supplier.decrypt(audit["evidence_ciphertext"]) == EVIDENCE


@pytest.mark.parametrize("limit", ["lowered_budget", "other_committed_order"])
def test_operator_retry_cannot_bypass_hard_spending_budget(supplier_shop, limit):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(Reply(400, {"success": False, "message": "Explicit offline rejection"}))
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "failed"
    assert h.intent(order)["budget_held"] == 0
    if limit == "lowered_budget":
        h.configure(spend_budget="9")
    else:
        h.configure(spend_budget="10")
        h.clock[0] += 1
        h.paid_order(user=102)
    post_count = len(h.transport.posts)
    try:
        h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
    except ShopError as exc:
        assert "budget" in str(exc).lower()
    else:
        h.transport.outcomes.append(purchase_reply())
        # Claim the older approved retry without purchasing the other queued order.
        asyncio.run(h.worker.purchase_one())
    assert len(h.transport.posts) == post_count, "Operator approval must not bypass the hard supplier budget"
    assert h.intent(order)["attempts"] == 1
    assert h.intent(order)["state"] not in {"processing", "completed"}


def test_post_rate_limit_cooldown_never_authorizes_automatic_retry(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(Reply(429, {"rateLimit": {"retryAfter": 120}}, {"Retry-After": "60"}))
    asyncio.run(h.worker.tick())
    until = h.clock[0] + 120
    assert h.store.supplier.cooldown_until() == until
    assert h.intent(order)["state"] == "uncertain"
    count = len(h.transport.calls)
    h.clock[0] += 119
    asyncio.run(h.worker.tick())
    assert len(h.transport.calls) == count
    h.clock[0] += 2
    h.restart()
    asyncio.run(h.worker.tick())
    h.clock[0] += 3600
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1
    assert h.intent(order)["attempts"] == 1
    assert_no_delivery(h, order)


def test_explicitly_approved_retry_still_waits_for_persisted_cooldown(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(Reply(429, {"rateLimit": {"retryAfter": 60}}))
    asyncio.run(h.worker.tick())
    first_post = copy.deepcopy(h.transport.posts[0])
    h.store.supplier.resolve(order["id"], "retry_same_request", EVIDENCE, "offline-operator")
    h.transport.outcomes.append(purchase_reply())
    h.restart()
    h.clock[0] += 59
    asyncio.run(h.worker.tick())
    assert h.transport.posts == [first_post]
    h.clock[0] += 1
    asyncio.run(h.worker.tick())
    assert h.transport.posts == [first_post, first_post]
    assert h.intent(order)["state"] == "completed"


@pytest.mark.parametrize("endpoint", ["products", "balance"])
def test_synchronization_failure_blocks_purchase_and_respects_cooldown(supplier_shop, endpoint):
    h = supplier_shop
    order = h.paid_order()
    if endpoint == "balance":
        h.transport.read_outcomes.append(Reply(200, copy.deepcopy(h.transport.catalog)))
    h.transport.read_outcomes.append(Reply(429, {"rateLimit": {"retryAfter": 90}}))
    h.worker.last_sync = float("-inf")
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "queued"
    assert h.store.supplier.cooldown_until() == h.clock[0] + 90
    assert not h.transport.posts
    count = len(h.transport.calls)
    asyncio.run(h.worker.tick())
    assert len(h.transport.calls) == count
    h.clock[0] += 90
    h.transport.outcomes.append(purchase_reply())
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "completed"
    assert len(h.transport.posts) == 1


@pytest.mark.parametrize("status", [400, 401, 404])
def test_permanent_explicit_rejection_allows_safe_refund_without_automatic_retry(supplier_shop, status):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(Reply(status, {"success": False, "message": "Offline permanent rejection"}))
    asyncio.run(h.worker.tick())
    assert h.intent(order)["state"] == "failed"
    assert h.intent(order)["hold_reason"] == f"supplier_rejected_{status}"
    assert h.intent(order)["budget_held"] == 0
    assert_no_delivery(h, order)
    h.clock[0] += 3600
    h.restart()
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1
    event = h.store.payment_for_order(order["id"])
    h.store.begin_refund(event["id"])
    assert h.intent(order)["state"] == "cancelled"
    h.store.finish_refund(event["id"])
    assert h.store.get_order(order["id"])["state"] == "refunded"
    assert h.store.get_payment(event["id"])["status"] == "refunded"
    asyncio.run(h.worker.tick())
    assert len(h.transport.posts) == 1


def test_delivery_network_retry_reuses_allocated_account_without_supplier_purchase(supplier_shop):
    h = supplier_shop
    order = h.paid_order()
    h.transport.outcomes.append(purchase_reply())
    asyncio.run(h.worker.tick())
    h.bot.send_message.side_effect = [OSError("mock Telegram failure"), h.bot.send_message.return_value]
    assert asyncio.run(h.delivery.deliver(order["id"])) is False
    assert h.store.get_order(order["id"])["state"] == "paid"
    h.clock[0] += 100
    assert asyncio.run(h.delivery.deliver(order["id"])) is True
    assert h.store.get_order(order["id"])["state"] == "delivered"
    assert h.bot.send_message.call_args_list[0] == h.bot.send_message.call_args_list[1]
    assert len(h.transport.posts) == 1
    assert len(h.rows("stock")) == 1
