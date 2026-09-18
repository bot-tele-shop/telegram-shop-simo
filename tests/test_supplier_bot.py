"""UI contract tests use a local supplier double; no supplier network is contacted."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.methods import RefundStarPayment, SendDocument, SendInvoice, SendMessage
from aiogram.types import BufferedInputFile, PreCheckoutQuery, SuccessfulPayment
from test_bot import RecordingSession, callback, feed, message, user

from shop.bot import build_dispatcher
from shop.config import CanbosoSettings, Settings
from shop.delivery import DeliveryWorker, send_delivery
from shop.store import PaymentResult, ShopError, Store
from shop.supplier_store import SupplierState


@pytest.fixture
def supplier_ui():
    settings = Settings(
        bot_token="12345678:TEST_ONLY_NOT_A_REAL_TELEGRAM_TOKEN",
        admin_ids=frozenset({999}),
        environment="production",
        enable_sales=True,
        terms_text="Fictional terms for local UI tests.",
        privacy_text="Fictional privacy notice for local UI tests.",
        support_contact="merchant@example.invalid",
        canboso=CanbosoSettings(
            enabled=True,
            api_key="TEST_ONLY_BUYER_SECRET",
            allow_purchases=True,
            resale_authorized=True,
            acknowledge_price_race=True,
            budget_currency="USD",
            spend_budget="100",
        ),
    )
    products = {
        sku: {
            "sku": sku,
            "title": f"Supplier {kind}",
            "description": "Fictional product used by an offline test.",
            "price_stars": 73,
            "source": "supplier",
            "active": True,
            "available": 0,
        }
        for sku, kind in (("account", "account"), ("slot", "slot"))
    }
    mappings = {"account": {"product_type": "account"},
                "slot": {"product_type": "slot", "slot_months": 3}}
    orders, inputs, infos = {}, {}, {}
    accepted = {(101, settings.terms_version)}
    store = Mock(spec=Store)
    store.supplier = Mock(spec=SupplierState)
    store.supplier.mapping.side_effect = lambda sku: mappings[sku].copy()
    store.supplier.mapping_many.side_effect = (
        lambda skus: {sku: mappings[sku].copy() for sku in skus if sku in mappings}
    )
    store.supplier.info.side_effect = lambda order_id: infos.get(order_id)
    store.supplier.info_many.side_effect = (
        lambda ids: {order_id: infos[order_id] for order_id in ids if order_id in infos}
    )
    store.supplier.review.return_value = []
    store.get_product.side_effect = lambda sku: products[sku].copy()
    store.list_products.side_effect = lambda: list(products.values())
    store.has_accepted_terms.side_effect = lambda uid, version: (uid, version) in accepted
    store.accept_terms.side_effect = lambda uid, version: accepted.add((uid, version))
    store.user_orders.side_effect = lambda uid: [o for o in orders.values() if o["user_id"] == uid]

    def get_order(order_id, *, user_id=None):
        order = orders.get(order_id)
        if not order or (user_id is not None and order["user_id"] != user_id):
            raise ShopError("Order not found")
        return order.copy()

    def create_order(uid, sku, version, customer_email=None):
        order_id = f"{len(orders) + 1:032x}"
        order = {**products[sku], "id": order_id, "user_id": uid,
                 "terms_version": version, "state": "invoice"}
        orders[order_id] = order
        infos[order_id] = {"state": "draft", "product_type": mappings[sku]["product_type"],
                           "supplier_reference": "", "hold_reason": None}
        inputs[order_id] = {}
        if customer_email is not None:
            inputs[order_id] = {"customer_email": customer_email}
            if "slot_months" in mappings[sku]:
                inputs[order_id]["slot_months"] = mappings[sku]["slot_months"]
        return order.copy()

    def preview_input(order_id, uid):
        get_order(order_id, user_id=uid)
        return inputs[order_id].copy()

    store.get_order.side_effect = get_order
    store.create_order.side_effect = create_order
    store.supplier.preview_input.side_effect = preview_input
    session = RecordingSession()
    bot = Bot(settings.bot_token, session=session)
    worker = DeliveryWorker(store, bot, settings.admin_ids)
    return SimpleNamespace(
        settings=settings, store=store, session=session, bot=bot, worker=worker,
        dp=build_dispatcher(settings, store, worker), orders=orders, inputs=inputs,
        infos=infos, mappings=mappings,
    )


def draft(h, email="buyer@example.invalid"):
    feed(h, callback_query=callback("buy:slot"))
    feed(h, message=message(email))
    return next(reversed(h.orders.values()))


def invoices(h):
    return [call for call in h.session.calls if isinstance(call, SendInvoice)]


def texts(h):
    return "\n".join(call.text for call in h.session.calls if isinstance(call, SendMessage))


def test_dispatcher_configures_supplier_and_catalog_uses_manual_stars(supplier_ui):
    h = supplier_ui
    h.store.supplier.configure.assert_called_once_with(h.settings.canboso)
    feed(h, message=message("/shop"))
    catalog = h.session.calls[-1]
    labels = [b.text for row in catalog.reply_markup.inline_keyboard for b in row]
    assert any("73 Stars" in label and "3 months" in label for label in labels)
    assert not any("sold out" in label for label in labels)
    feed(h, callback_query=callback("p:slot"))
    detail = h.session.calls[-1]
    assert "3 months (fixed)" in detail.text
    assert "Available units: 0" not in detail.text
    assert detail.reply_markup.inline_keyboard[0][0].callback_data == "buy:slot"


def test_supplier_account_sends_invoice_without_email(supplier_ui):
    h = supplier_ui
    feed(h, callback_query=callback("buy:account"))
    h.store.create_order.assert_called_once_with(101, "account", h.settings.terms_version)
    invoice = invoices(h)[0]
    assert invoice.chat_id == 101
    assert invoice.currency == "XTR" and invoice.provider_token == ""
    assert invoice.prices[0].amount == 73 and len(invoice.prices) == 1
    assert invoice.protect_content is True
    h.store.supplier.preview_input.assert_not_called()


@pytest.mark.parametrize("invalid", ["not-an-email", "a@", "a@example.com\nother@example.com"])
def test_slot_disclosure_then_validated_draft_before_invoice(supplier_ui, invalid):
    h = supplier_ui
    feed(h, callback_query=callback("buy:slot"))
    assert "shared with Canboso, the supplier" in texts(h)
    assert "3 months (fixed)" in texts(h)
    assert not invoices(h)
    h.store.create_order.assert_not_called()
    feed(h, message=message(invalid))
    h.store.create_order.assert_not_called()
    assert "valid email" in h.session.calls[-1].text
    feed(h, message=message(" buyer@example.invalid "))
    h.store.create_order.assert_called_once_with(
        101, "slot", h.settings.terms_version, customer_email="buyer@example.invalid"
    )
    confirmation = h.session.calls[-1]
    assert "Email: buyer@example.invalid" in confirmation.text
    assert "Duration: 3 months (fixed)" in confirmation.text
    assert confirmation.protect_content is True
    assert not invoices(h)
    data = confirmation.reply_markup.inline_keyboard[0][0].callback_data
    assert data.startswith("confirm:")
    assert len(data.encode()) <= 64 and "buyer@" not in data
    feed(h, callback_query=callback(data))
    invoice = invoices(h)[0]
    assert invoice.payload == data[8:]
    assert "buyer@example.invalid" in invoice.description
    assert "3 months (fixed)" in invoice.description
    assert h.store.create_order.call_count == 1


def test_slot_without_month_variant_does_not_invent_duration(supplier_ui):
    h = supplier_ui
    h.mappings["slot"].pop("slot_months")
    order = draft(h)
    assert "no month selection" in h.session.calls[-1].text
    assert "months (fixed)" not in h.session.calls[-1].text
    feed(h, callback_query=callback(f"confirm:{order['id']}"))
    assert "no month selection" in invoices(h)[0].description


def test_confirmation_recovers_saved_email_and_months_after_restart(supplier_ui):
    h = supplier_ui
    order = draft(h)
    h.mappings["slot"]["slot_months"] = 12
    h.dp = build_dispatcher(h.settings, h.store, h.worker)
    feed(h, message=message("/orders"))
    feed(h, callback_query=callback(f"o:{order['id']}"))
    confirmation = h.session.calls[-1]
    assert "buyer@example.invalid" in confirmation.text
    assert "3 months (fixed)" in confirmation.text
    assert "12 months" not in confirmation.text
    feed(h, callback_query=callback(confirmation.reply_markup.inline_keyboard[0][0].callback_data))
    assert invoices(h)[0].payload == order["id"]
    assert "3 months (fixed)" in invoices(h)[0].description
    assert h.store.create_order.call_count == 1


def test_changed_email_is_passed_to_core_without_reusing_ui_order(supplier_ui):
    h = supplier_ui
    first = draft(h, "first@example.invalid")
    second = draft(h, "second@example.invalid")
    assert first["id"] != second["id"]
    assert h.store.create_order.call_args.kwargs == {"customer_email": "second@example.invalid"}
    feed(h, callback_query=callback(f"confirm:{first['id']}"))
    assert "first@example.invalid" in invoices(h)[0].description
    assert "second@example.invalid" not in invoices(h)[0].description


def test_confirmation_and_order_preview_are_owner_bound(supplier_ui):
    h = supplier_ui
    order = draft(h)
    h.store.supplier.preview_input.reset_mock()
    for prefix in ("o:", "confirm:"):
        feed(h, callback_query=callback(prefix + order["id"], user_id=102))
        assert h.session.calls[-1].text == "Order not found"
    h.store.supplier.preview_input.assert_not_called()
    assert not invoices(h)


@pytest.mark.parametrize("gate", ["paused", "terms"])
def test_buy_and_confirm_recheck_sales_and_terms(supplier_ui, gate):
    h = supplier_ui
    order = draft(h)
    if gate == "paused":
        h.settings = replace(h.settings, enable_sales=False)
    else:
        h.settings = replace(h.settings, terms_text="Changed fictional terms.")
    h.dp = build_dispatcher(h.settings, h.store, h.worker)
    h.store.create_order.reset_mock()
    feed(h, callback_query=callback("buy:account"))
    feed(h, callback_query=callback(f"confirm:{order['id']}"))
    assert not invoices(h)
    h.store.create_order.assert_not_called()


def test_old_draft_terms_cannot_be_confirmed_after_accepting_new_terms(supplier_ui):
    h = supplier_ui
    order = draft(h)
    h.settings = replace(h.settings, terms_text="Changed fictional terms.")
    h.store.accept_terms(101, h.settings.terms_version)
    h.dp = build_dispatcher(h.settings, h.store, h.worker)
    feed(h, callback_query=callback(f"confirm:{order['id']}"))
    assert "terms changed" in h.session.calls[-1].text
    assert not invoices(h)


@pytest.mark.parametrize("state", ["expired", "checkout", "cancelled", "paid", "refunded"])
def test_confirm_rejects_non_invoice_states(supplier_ui, state):
    h = supplier_ui
    order = draft(h)
    order["state"] = state
    feed(h, callback_query=callback(f"confirm:{order['id']}"))
    assert not invoices(h)


def test_email_entry_expires_without_creating_order(supplier_ui, monkeypatch):
    h = supplier_ui
    now = [100.0]
    monkeypatch.setattr("shop.bot.time", SimpleNamespace(monotonic=lambda: now[0]))
    feed(h, callback_query=callback("buy:slot"))
    now[0] += 601
    feed(h, message=message("buyer@example.invalid"))
    h.store.create_order.assert_not_called()
    assert "expired" in h.session.calls[-1].text


def test_cancel_and_commands_do_not_become_email(supplier_ui):
    h = supplier_ui
    feed(h, callback_query=callback("buy:slot"))
    feed(h, message=message("/support"))
    h.store.create_order.assert_not_called()
    feed(h, message=message("/cancel"))
    feed(h, message=message("buyer@example.invalid"))
    h.store.create_order.assert_not_called()
    assert not invoices(h)


def test_failed_slot_invoice_send_preserves_draft_for_recovery(supplier_ui, monkeypatch):
    h = supplier_ui
    order = draft(h)
    monkeypatch.setattr(h.bot, "send_invoice", AsyncMock(side_effect=OSError("private transport")))
    feed(h, callback_query=callback(f"confirm:{order['id']}"))
    h.store.cancel_invoice.assert_not_called()
    assert order["state"] == "invoice"
    assert "/orders" in h.session.calls[-1].text


def test_precheckout_only_calls_local_approval(supplier_ui):
    h = supplier_ui
    order = draft(h)
    h.store.supplier.reset_mock()
    feed(h, pre_checkout_query=PreCheckoutQuery(
        id="checkout", from_user=user(), currency="XTR", total_amount=73,
        invoice_payload=order["id"],
    ))
    assert h.session.calls[-1].ok is True
    h.store.approve_checkout.assert_called_once_with(
        order["id"], 101, "XTR", 73, "checkout", h.settings.terms_version
    )
    assert not h.store.supplier.mock_calls


def test_successful_payment_persists_before_notification_without_delivery(supplier_ui, monkeypatch):
    h = supplier_ui
    order = draft(h)
    recorded = []

    def record(*args):
        duplicate = bool(recorded)
        recorded.append(args)
        order["state"] = "paid"
        h.infos[order["id"]]["state"] = "queued"
        return PaymentResult("event", order["id"], "accepted", duplicate)

    async def notify(bot, method, timeout=None):
        assert recorded and order["state"] == "paid"
        assert isinstance(method, SendMessage)
        assert "queued" in method.text
        assert "not delivery confirmation" in method.text
        raise OSError("PRIVATE_TRANSPORT_DETAIL")

    h.store.record_payment.side_effect = record
    sender = AsyncMock(side_effect=notify)
    monkeypatch.setattr(h.session, "make_request", sender)
    payment = SuccessfulPayment(
        currency="XTR", total_amount=73, invoice_payload=order["id"],
        telegram_payment_charge_id="test-charge", provider_payment_charge_id="",
    )
    feed(h, message=message(successful_payment=payment))
    feed(h, message=message(successful_payment=payment))
    assert sender.await_count == 1
    h.store.claim_delivery.assert_not_called()


@pytest.mark.parametrize("state", ["queued", "processing", "pending", "uncertain", "blocked",
                                   "failed", "retry_wait", "completed", "resolved_for_refund"])
def test_orders_show_supplier_states_and_support(supplier_ui, state):
    h = supplier_ui
    order = draft(h)
    order["state"] = "paid"
    h.infos[order["id"]].update(state=state, supplier_reference="supplier-ref",
                              hold_reason="operator_review")
    feed(h, callback_query=callback(f"o:{order['id']}"))
    response = h.session.calls[-1]
    assert state.replace("_", " ") in response.text
    assert "supplier-ref" in response.text and "operator_review" in response.text
    assert "/paysupport" in response.text and h.settings.support_contact in response.text
    assert "View my delivery" not in str(response.reply_markup)
    if state == "pending":
        assert "waiting for seller" in response.text
        feed(h, message=message("/orders"))
        assert "waiting for seller" in str(h.session.calls[-1].reply_markup)


def test_supplierreview_is_admin_only_and_does_not_echo_raw_fields(supplier_ui):
    h = supplier_ui
    feed(h, message=message("/supplierreview"))
    h.store.supplier.review.assert_not_called()
    h.store.supplier.review.return_value = [{
        "order_id": "order-ref", "state": "uncertain", "supplier_reference": "supplier-ref",
        "hold_reason": "process_interrupted", "request_ciphertext": "PRIVATE_REQUEST",
        "raw": {"key": h.settings.canboso.api_key, "email": "PRIVATE_EMAIL"},
    }]
    feed(h, message=message("/supplierreview", user_id=999))
    output = texts(h)
    assert "supplier-resolve" in output and "evidence" in output
    assert "Stars refund does not refund or cancel the supplier order" in output
    assert "process_interrupted" in output and "supplier-ref" in output
    assert "PRIVATE_REQUEST" not in output and "PRIVATE_EMAIL" not in output
    assert h.settings.canboso.api_key not in output
    assert not any(isinstance(c, RefundStarPayment) for c in h.session.calls)


def test_refund_prompt_warns_about_separate_supplier_order(supplier_ui):
    h = supplier_ui
    event = {"id": "event", "order_id": "order-ref", "user_id": 101,
             "amount": 73, "currency": "XTR", "status": "accepted", "reason": None}
    h.store.payment_for_order.return_value = event
    h.store.get_payment.return_value = event
    feed(h, message=message("/refund order-ref", user_id=999))
    assert "Stars refund does not refund or cancel the supplier order" in texts(h)
    assert not any(isinstance(c, RefundStarPayment) for c in h.session.calls)


@pytest.mark.parametrize("payload", ["x" * 3501, "\U0001d11e" * 1751, "line\n" * 900])
def test_large_delivery_is_one_protected_utf8_file(payload):
    bot = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                          send_message=AsyncMock())
    sent = asyncio.run(send_delivery(bot, 101, "../unsafe\\order:name", "Example", payload))
    assert sent.message_id == 42
    bot.send_message.assert_not_called()
    assert bot.send_document.await_count == 1
    args = bot.send_document.call_args.kwargs
    assert isinstance(args["document"], BufferedInputFile)
    assert args["document"].data.decode("utf-8") == payload
    assert args["document"].filename.endswith(".txt")
    assert not any(c in args["document"].filename for c in "/\\:")
    assert args["protect_content"] is True and args["parse_mode"] is None


def test_delivery_threshold_counts_full_message_utf16_units():
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    prefix = "Delivery for order abc\n\n"
    payload = "x" * (3500 - len(prefix))
    asyncio.run(send_delivery(bot, 101, "abc", "", payload))
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["text"] == prefix + payload
    asyncio.run(send_delivery(bot, 101, "abc", "", payload + "x"))
    bot.send_document.assert_awaited_once()


def test_worker_retries_complete_document_and_finishes_with_document_id(store, paid, clock):
    payload = "private delivery\n" * 400
    with store.transaction() as db:
        db.execute("UPDATE stock SET ciphertext=? WHERE order_id=?",
                   (store.cipher.encrypt(payload.encode()).decode(), paid["id"]))
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock(
        side_effect=[OSError("PRIVATE_PAYLOAD"), SimpleNamespace(message_id=42)]
    ))
    worker = DeliveryWorker(store, bot, frozenset())
    assert asyncio.run(worker.deliver(paid["id"])) is False
    assert store.get_order(paid["id"])["state"] == "paid"
    clock[0] += 100
    assert asyncio.run(worker.deliver(paid["id"])) is True
    assert store.get_order(paid["id"])["message_id"] == 42
    assert store.get_order(paid["id"])["state"] == "delivered"
    documents = [call.kwargs["document"] for call in bot.send_document.call_args_list]
    assert documents[0].data == documents[1].data == payload.encode()
    assert documents[0].filename == documents[1].filename
    bot.send_message.assert_not_called()


def test_get_callback_uses_shared_document_delivery_and_owner_check(supplier_ui):
    h = supplier_ui
    payload = "private\n" * 600

    def retrieve(order_id, uid):
        if uid != 101:
            raise ShopError("Delivery is unavailable")
        return payload

    h.store.retrieve_delivery.side_effect = retrieve
    feed(h, callback_query=callback("get:order-ref", user_id=102))
    assert not any(isinstance(c, SendDocument) for c in h.session.calls)
    feed(h, callback_query=callback("get:order-ref"))
    documents = [c for c in h.session.calls if isinstance(c, SendDocument)]
    assert len(documents) == 1
    assert documents[0].document.data == payload.encode()
    assert documents[0].protect_content is True
    assert payload not in texts(h)
