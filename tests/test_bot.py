import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    RefundStarPayment,
    SendInvoice,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)

from shop.bot import build_dispatcher
from shop.config import Settings
from shop.delivery import DeliveryWorker
from shop.polling import update_kind


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendInvoice)):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


def user(user_id=101):
    return User(id=user_id, is_bot=False, first_name="Test customer")


def message(text=None, user_id=101, **kwargs):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        from_user=user(user_id),
        chat=Chat(id=user_id, type="private"),
        text=text,
        **kwargs,
    )


def callback(data, user_id=101):
    return CallbackQuery(
        id="callback-test",
        from_user=user(user_id),
        chat_instance="test-chat",
        data=data,
        message=message("Shop buttons", user_id=user_id),
    )


def feed(h, **kwargs):
    return asyncio.run(
        h.dp.feed_update(h.bot, Update(update_id=len(h.session.calls) + 1, **kwargs))
    )


@pytest.fixture
def harness(store, key):
    settings = Settings(
        bot_token="12345678:TEST_ONLY_NOT_A_REAL_TELEGRAM_TOKEN",
        admin_ids=frozenset({999}),
        stock_encryption_key=key,
        database_path=store.path,
        enable_sales=True,
        support_contact="merchant@example.invalid",
        terms_text="Test-only terms, not real merchant terms.",
        privacy_text="Test-only privacy notice with fictional content.",
    )
    store.accept_terms(101, settings.terms_version)
    session = RecordingSession()
    bot = Bot(token=settings.bot_token, session=session)
    worker = DeliveryWorker(store, bot, settings.admin_ids)
    return SimpleNamespace(
        settings=settings,
        store=store,
        session=session,
        bot=bot,
        worker=worker,
        dp=build_dispatcher(settings, store, worker),
    )


def test_catalog_has_products_and_order_buttons(harness):
    feed(harness, message=message("/shop"))
    response = harness.session.calls[-1]
    assert isinstance(response, SendMessage)
    assert "TEST ENVIRONMENT" in response.text
    assert any(
        "sample-key" in b.callback_data
        for row in response.reply_markup.inline_keyboard
        for b in row
    )


def test_invoice_is_stars_single_price_bound_to_buyer(harness):
    feed(harness, callback_query=callback("buy:sample-key"))
    invoice = next(c for c in harness.session.calls if isinstance(c, SendInvoice))
    assert invoice.currency == "XTR"
    assert invoice.provider_token == ""
    assert len(invoice.prices) == 1 and invoice.prices[0].amount == 25
    assert invoice.chat_id == 101
    assert invoice.start_parameter
    assert harness.store.get_order(invoice.payload)["user_id"] == 101
    assert "LICENSE" not in invoice.description


def test_buy_requires_consent(harness):
    feed(harness, callback_query=callback("buy:sample-key", user_id=102))
    assert not any(isinstance(c, SendInvoice) for c in harness.session.calls)
    assert any(
        isinstance(c, SendMessage) and "Privacy notice" in c.text for c in harness.session.calls
    )


def test_paused_shop_cannot_issue_invoice(harness):
    settings = replace(harness.settings, enable_sales=False)
    harness.dp = build_dispatcher(settings, harness.store, harness.worker)
    feed(harness, callback_query=callback("buy:sample-key"))
    assert not any(isinstance(c, SendInvoice) for c in harness.session.calls)
    assert "paused" in harness.session.calls[-1].text


def test_precheckout_rejects_wrong_amount(harness):
    order = harness.store.create_order(101, "sample-key", harness.settings.terms_version)
    query = PreCheckoutQuery(
        id="q", from_user=user(), currency="XTR", total_amount=1, invoice_payload=order["id"]
    )
    feed(harness, pre_checkout_query=query)
    answer = harness.session.calls[-1]
    assert isinstance(answer, AnswerPreCheckoutQuery)
    assert not answer.ok


def test_payment_not_checkout_triggers_fulfillment(harness):
    order = harness.store.create_order(101, "sample-key", harness.settings.terms_version)
    query = PreCheckoutQuery(
        id="q", from_user=user(), currency="XTR", total_amount=25, invoice_payload=order["id"]
    )
    feed(harness, pre_checkout_query=query)
    assert harness.session.calls[-1].ok
    assert asyncio.run(harness.worker.deliver(order["id"])) is False
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=25,
        invoice_payload=order["id"],
        telegram_payment_charge_id="telegram-charge",
        provider_payment_charge_id="",
    )
    feed(harness, message=message(successful_payment=payment))
    assert asyncio.run(harness.worker.deliver(order["id"])) is True
    feed(harness, message=message(successful_payment=payment))
    assert asyncio.run(harness.worker.deliver(order["id"])) is False
    deliveries = [
        c for c in harness.session.calls if isinstance(c, SendMessage) and "LICENSE-001" in c.text
    ]
    assert len(deliveries) == 1
    assert deliveries[0].protect_content is True and deliveries[0].parse_mode is None


def test_non_admin_cannot_refund(harness, paid):
    event = harness.store.payment_for_order(paid["id"])
    feed(harness, callback_query=callback(f"rf:{event['id']}"))
    assert not any(isinstance(c, RefundStarPayment) for c in harness.session.calls)
    assert harness.store.get_order(paid["id"])["state"] == "paid"


def test_admin_refund_requires_confirmation(harness, paid):
    feed(harness, message=message(f"/refund {paid['id']}", user_id=999))
    assert not any(isinstance(c, RefundStarPayment) for c in harness.session.calls)
    prompt = harness.session.calls[-1]
    assert "Confirm full refund" in prompt.text
    confirm = prompt.reply_markup.inline_keyboard[0][0].callback_data
    feed(harness, callback_query=callback(confirm, user_id=999))
    assert sum(isinstance(c, RefundStarPayment) for c in harness.session.calls) == 1
    assert harness.store.get_order(paid["id"])["state"] == "refunded"


def test_customer_cannot_read_someone_elses_order(harness, paid):
    feed(harness, callback_query=callback(f"o:{paid['id']}", user_id=102))
    assert isinstance(harness.session.calls[-1], AnswerCallbackQuery)
    assert harness.session.calls[-1].text == "Order not found"


def test_paysupport_identifies_merchant(harness):
    feed(harness, message=message("/paysupport"))
    assert harness.settings.support_contact in harness.session.calls[-1].text
    assert "merchant" in harness.session.calls[-1].text.lower()


def test_delivery_transport_failure_retries_same_key(store, paid, clock):
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[OSError("network failure"), SimpleNamespace(message_id=42)]
        )
    )
    worker = DeliveryWorker(store, bot, frozenset())
    assert asyncio.run(worker.deliver(paid["id"])) is False
    assert store.get_order(paid["id"])["state"] == "paid"
    clock[0] += 100
    assert asyncio.run(worker.deliver(paid["id"])) is True
    assert (
        bot.send_message.call_args_list[0].kwargs["text"]
        == bot.send_message.call_args_list[1].kwargs["text"]
    )
    assert store.stats()["stock"]["sold"] == 1


def test_private_inventory_markup_is_literal(store, order):
    # Stock values are plain data, not HTML or executable content.
    store.record_payment(order["id"], 101, "XTR", 25, "charge")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
    worker = DeliveryWorker(store, bot, frozenset())
    asyncio.run(worker.deliver(order["id"]))
    assert bot.send_message.call_args.kwargs["parse_mode"] is None
    assert bot.send_message.call_args.kwargs["link_preview_options"] == {"is_disabled": True}


def test_payment_lane_does_not_share_ui_queue():
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=25,
        invoice_payload="order",
        telegram_payment_charge_id="charge",
        provider_payment_charge_id="",
    )
    assert (
        update_kind(Update(update_id=1, message=message(successful_payment=payment))) == "payment"
    )
    assert update_kind(Update(update_id=2, message=message("/shop"))) == "ui"
