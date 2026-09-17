"""Payment/delivery invariant tests for the webhook Worker flow.

The database is mocked at the RPC boundary; each test drives the real
worker/src/flow.py code and asserts which RPCs and Telegram calls happen.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def worker_modules(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "worker/src"
    monkeypatch.setitem(sys.modules, "httpclient", SimpleNamespace(
        request=AsyncMock(side_effect=AssertionError("Network access forbidden in tests"))))
    monkeypatch.setitem(sys.modules, "fernet", SimpleNamespace(Fernet=object))
    modules = {}
    for name in ("storefront", "db", "telegram", "flow"):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return SimpleNamespace(**modules)


@pytest.fixture
def ctx(worker_modules):
    return SimpleNamespace(
        flow=worker_modules.flow,
        tg=SimpleNamespace(send_message=AsyncMock(), answer_callback=AsyncMock(),
                           send_invoice=AsyncMock(), answer_pre_checkout=AsyncMock()),
        db=SimpleNamespace(select=AsyncMock(return_value=[]), rpc=AsyncMock(),
                           select_one=AsyncMock(return_value=None), insert=AsyncMock(),
                           upsert=AsyncMock()),
        fernet=SimpleNamespace(decrypt=AsyncMock(return_value="CODE-123")),
        shop_name="Shop", support="@support", terms="T", privacy="P",
        terms_version="v1", admins=[7],
        notify_admins=AsyncMock(),
        checkout_paused=AsyncMock(return_value=False),
    )


def payment(order_id="ord_1", charge="chg_1", user_id=101, amount=50, currency="XTR"):
    return {
        "chat": {"id": 101, "type": "private"},
        "from": {"id": 101},
        "successful_payment": {
            "invoice_payload": order_id,
            "telegram_payment_charge_id": charge,
            "total_amount": amount,
            "currency": currency,
        },
    }


def test_payment_binds_payer_amount_currency_and_charge(ctx):
    ctx.db.rpc.return_value = {"ok": True, "duplicate": False}
    ctx.db.select_one.return_value = {"ciphertext": "ct"}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    call = ctx.db.rpc.call_args_list[0]
    assert call.args[0] == "fulfill_order"
    assert call.args[1] == {"p_order_id": "ord_1", "p_charge_id": "chg_1",
                            "p_user_id": 101, "p_amount": 50, "p_currency": "XTR"}


def test_successful_payment_delivers_then_confirms(ctx):
    ctx.db.rpc.return_value = {"ok": True, "duplicate": False}
    ctx.db.select_one.return_value = {"ciphertext": "ct"}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    ctx.tg.send_message.assert_awaited_once()
    assert "CODE-123" in ctx.tg.send_message.call_args.args[1]
    rpc_names = [c.args[0] for c in ctx.db.rpc.call_args_list]
    assert rpc_names == ["fulfill_order", "confirm_delivery"]


def test_failed_telegram_send_records_failure_and_never_confirms(ctx):
    ctx.db.rpc.return_value = {"ok": True, "duplicate": False}
    ctx.db.select_one.return_value = {"ciphertext": "ct"}
    ctx.tg.send_message.side_effect = RuntimeError("telegram down")
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    rpc_names = [c.args[0] for c in ctx.db.rpc.call_args_list]
    assert "record_delivery_failure" in rpc_names
    assert "confirm_delivery" not in rpc_names
    ctx.notify_admins.assert_awaited()


def test_duplicate_payment_resends_same_assigned_code(ctx):
    ctx.db.rpc.return_value = {"ok": True, "duplicate": True}
    ctx.db.select_one.return_value = {"ciphertext": "ct"}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    # stock is fetched by order_id — the existing assignment, never a new one
    assert ctx.db.select_one.call_args.args[0] == "stock"
    assert ctx.db.select_one.call_args.args[1]["order_id"] == "eq.ord_1"
    ctx.tg.send_message.assert_awaited_once()


def test_out_of_stock_notifies_refund_path(ctx):
    ctx.db.rpc.return_value = {"ok": False, "reason": "out_of_stock"}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    text = ctx.tg.send_message.call_args.args[1]
    assert "refund" in text.lower()
    ctx.notify_admins.assert_awaited()


@pytest.mark.parametrize("reason", ["order_not_found", "payment_mismatch",
                                    "charge_conflict", "bad_state"])
def test_unexpected_paid_event_is_parked_for_review_never_dropped(ctx, reason):
    ctx.db.rpc.return_value = {"ok": False, "reason": reason}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    ctx.notify_admins.assert_awaited_once()
    assert "REVIEW" in ctx.notify_admins.call_args.args[0]
    # user is told their Stars are safe; nothing is delivered
    assert "manual review" in ctx.tg.send_message.call_args.args[1]


def test_supplier_payment_gets_refund_path_not_waiting_state(ctx):
    ctx.db.rpc.return_value = {"ok": False, "reason": "supplier_unavailable"}
    asyncio.run(ctx.flow.handle_payment(ctx, payment()))
    assert "refund" in ctx.tg.send_message.call_args.args[1].lower()
    ctx.notify_admins.assert_awaited()


def test_pre_checkout_validates_server_side(ctx):
    ctx.db.rpc.return_value = {"ok": True}
    query = {"id": "q1", "invoice_payload": "ord_1", "from": {"id": 101},
             "total_amount": 50, "currency": "XTR"}
    asyncio.run(ctx.flow.handle_pre_checkout(ctx, query))
    call = ctx.db.rpc.call_args
    assert call.args[0] == "pre_checkout_validate"
    assert call.args[1]["p_amount"] == 50 and call.args[1]["p_currency"] == "XTR"
    ctx.tg.answer_pre_checkout.assert_awaited_with("q1", True)


@pytest.mark.parametrize("reason", list([
    "not_found", "not_payable", "expired", "amount_mismatch", "terms",
    "inactive", "supplier_unavailable", "out_of_stock",
]))
def test_pre_checkout_rejections_answer_with_message(ctx, reason):
    ctx.db.rpc.return_value = {"ok": False, "reason": reason}
    query = {"id": "q1", "invoice_payload": "ord_1", "from": {"id": 101},
             "total_amount": 50, "currency": "XTR"}
    asyncio.run(ctx.flow.handle_pre_checkout(ctx, query))
    args = ctx.tg.answer_pre_checkout.call_args.args
    assert args[1] is False and args[2]


def test_checkout_pause_blocks_invoice_creation(ctx):
    ctx.checkout_paused.return_value = True
    cb = {"id": "cb", "from": {"id": 101}, "data": "buy:SKU1",
          "message": {"chat": {"id": 101, "type": "private"}}}
    asyncio.run(ctx.flow.handle_callback(ctx, cb))
    ctx.db.insert.assert_not_awaited()
    ctx.tg.send_invoice.assert_not_awaited()
    assert "paused" in ctx.tg.answer_callback.call_args.args[1]


def test_supplier_product_rejected_before_payment(ctx):
    ctx.db.select_one.side_effect = [
        {"user_id": 101},  # terms acceptance
        {"sku": "SKU1", "active": True, "source": "supplier", "title": "T",
         "description": "", "price_stars": 50},
    ]
    cb = {"id": "cb", "from": {"id": 101}, "data": "buy:SKU1",
          "message": {"chat": {"id": 101, "type": "private"}}}
    asyncio.run(ctx.flow.handle_callback(ctx, cb))
    ctx.db.insert.assert_not_awaited()
    ctx.tg.send_invoice.assert_not_awaited()


def test_unmatched_refund_is_flagged_for_review(ctx):
    ctx.db.rpc.return_value = {"ok": True, "matched": False}
    message = {
        "chat": {"id": 101, "type": "private"}, "from": {"id": 101},
        "refunded_payment": {"telegram_payment_charge_id": "chg_x", "total_amount": 50},
    }
    asyncio.run(ctx.flow.handle_refund(ctx, message))
    ctx.notify_admins.assert_awaited_once()
    assert "UNMATCHED REFUND" in ctx.notify_admins.call_args.args[0]


@pytest.fixture
def patched_flow(worker_modules, ctx, monkeypatch):
    """handle_update builds Ctx(env); patch it to inject our mocked ctx."""
    monkeypatch.setattr(worker_modules.flow, "Ctx", lambda env: ctx)
    return worker_modules.flow


def _update(update_id, kind="message"):
    u = {"update_id": update_id}
    u[kind] = {"chat": {"id": 101, "type": "private"}, "from": {"id": 101}, "text": "/start"} \
        if kind == "message" else {}
    return u


def test_update_claimed_before_processing_and_marked_done(patched_flow, ctx):
    ctx.db.rpc.side_effect = ["claimed", None, True]
    asyncio.run(patched_flow.handle_update(_update(1), env=None))
    rpc_names = [c.args[0] for c in ctx.db.rpc.call_args_list]
    assert rpc_names[0] == "claim_update"
    assert rpc_names[-1] == "finish_update"
    assert ctx.db.rpc.call_args_list[-1].args[1]["p_ok"] is True


def test_done_update_is_not_reprocessed(patched_flow, ctx):
    ctx.db.rpc.return_value = "done"
    asyncio.run(patched_flow.handle_update(_update(1), env=None))
    ctx.tg.send_message.assert_not_awaited()
    assert ctx.db.rpc.await_count == 1


def test_busy_update_raises_retryable_error(patched_flow, ctx):
    ctx.db.rpc.return_value = "busy"
    with pytest.raises(patched_flow.TransientError):
        asyncio.run(patched_flow.handle_update(_update(1), env=None))


def test_failed_processing_is_recorded_then_reraised(patched_flow, ctx):
    ctx.db.rpc.side_effect = ["claimed", None]
    ctx.tg.send_message.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        asyncio.run(patched_flow.handle_update(_update(1), env=None))
    rpc_names = [c.args[0] for c in ctx.db.rpc.call_args_list]
    assert rpc_names == ["claim_update", "finish_update"]
    assert ctx.db.rpc.call_args_list[-1].args[1]["p_ok"] is False
