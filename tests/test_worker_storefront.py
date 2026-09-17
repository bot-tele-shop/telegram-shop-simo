"""Cloud UI contract tests with no network or Cloudflare credentials."""

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
    # Replace only runtime-specific dependencies, never the flow or Telegram client.
    monkeypatch.setitem(
        sys.modules,
        "httpclient",
        SimpleNamespace(
            request=AsyncMock(side_effect=AssertionError("Network access forbidden in UI tests"))
        ),
    )
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
def cloud(worker_modules):
    return SimpleNamespace(
        flow=worker_modules.flow,
        tg=SimpleNamespace(
            send_message=AsyncMock(), answer_callback=AsyncMock(), send_invoice=AsyncMock()
        ),
        db=SimpleNamespace(
            select=AsyncMock(return_value=[]),
            rpc=AsyncMock(return_value=[]),
            select_one=AsyncMock(return_value=None),
            insert=AsyncMock(),
            upsert=AsyncMock(),
        ),
        shop_name="My Shop <&>",
        support="merchant@example.invalid",
        terms="Test terms",
        privacy="Test privacy",
        terms_version="v1",
        checkout_paused=AsyncMock(return_value=False),
    )


def callback(data, *, user_id=101, chat_id=101, chat_type="private"):
    return {
        "id": "cb-test",
        "from": {"id": user_id},
        "data": data,
        "message": {"chat": {"id": chat_id, "type": chat_type}},
    }


def test_cloud_start_renders_menu_without_catalog_query(cloud):
    asyncio.run(
        cloud.flow.handle_message(
            cloud, {"chat": {"id": 101, "type": "private"}, "from": {"id": 101}, "text": "/start"}
        )
    )
    call = cloud.tg.send_message.call_args
    assert "<blockquote>" in call.args[1] and "&lt;&amp;&gt;" in call.args[1]
    assert call.kwargs["parse_mode"] == "HTML"
    assert [len(row) for row in call.kwargs["keyboard"]] == [1, 1, 2, 2, 2]
    assert all(b["style"] == "success" for row in call.kwargs["keyboard"] for b in row)
    quick = cloud.tg.send_message.await_args_list[0]
    assert [[button["text"] for button in row] for row in quick.kwargs["reply_keyboard"]] == [
        ["🛍 Products", "🛟 Support"],
        ["⭐ Payments"],
        ["🔗 API"],
    ]
    cloud.db.rpc.assert_not_awaited()
    cloud.db.insert.assert_not_awaited()
    cloud.tg.send_invoice.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    ["home", "cat:0", "offers", "profile", "orders", "payments", "referrals", "support", "api"],
)
def test_cloud_all_main_menu_callbacks_work(cloud, data):
    asyncio.run(cloud.flow.handle_callback(cloud, callback(data)))
    cloud.tg.answer_callback.assert_awaited_once()
    cloud.tg.send_message.assert_awaited()
    assert cloud.tg.send_message.call_args.kwargs["keyboard"]
    cloud.db.insert.assert_not_awaited()
    cloud.tg.send_invoice.assert_not_awaited()


@pytest.mark.parametrize("chat_id,chat_type", [(-99, "group"), (202, "private")])
def test_cloud_rejects_foreign_chat_callbacks(cloud, chat_id, chat_type):
    asyncio.run(
        cloud.flow.handle_callback(cloud, callback("orders", chat_id=chat_id, chat_type=chat_type))
    )
    cloud.tg.answer_callback.assert_awaited_once()
    cloud.tg.send_message.assert_not_awaited()
    cloud.db.select.assert_not_awaited()


def test_cloud_inline_callback_without_message_is_rejected(cloud):
    event = callback("profile")
    del event["message"]
    asyncio.run(cloud.flow.handle_callback(cloud, event))
    cloud.tg.send_message.assert_not_awaited()
    cloud.tg.answer_callback.assert_awaited_once()


def test_cloud_orders_query_is_buyer_scoped(cloud):
    asyncio.run(cloud.flow.handle_callback(cloud, callback("orders")))
    assert cloud.db.select.call_args.args[1]["user_id"] == "eq.101"


def test_cloud_terms_keyboard_contains_objects_not_tuples(cloud):
    asyncio.run(cloud.flow.show_terms(cloud, 101))
    button = cloud.tg.send_message.call_args.kwargs["keyboard"][0][0]
    assert button == {
        "text": "I accept these terms and privacy notice",
        "callback_data": "accept:v1",
    }


def test_cloud_telegram_client_passes_html_only_when_requested(worker_modules):
    client = worker_modules.telegram.Telegram("TEST-NOT-A-TOKEN")
    client.call = AsyncMock(return_value={})
    asyncio.run(client.send_message(101, "<b>Shop</b>", parse_mode="HTML"))
    assert client.call.call_args.kwargs["parse_mode"] == "HTML"
    asyncio.run(client.send_message(101, "<product-key>"))
    assert "parse_mode" not in client.call.call_args.kwargs


def test_cloud_product_button_uses_short_stars_checkout_route(cloud):
    cloud.db.rpc.return_value = [
        {
            "source": "stock",
            "available": 2,
            "title": "Demo",
            "price_stars": 25,
            "description": "Test only",
            "sku": "sample-key",
        }
    ]
    asyncio.run(cloud.flow.handle_callback(cloud, callback("cat:0")))
    keyboard = cloud.tg.send_message.call_args.kwargs["keyboard"]
    assert keyboard[0][0]["callback_data"].startswith("buyh:")
    assert len(keyboard[0][0]["callback_data"].encode()) <= 64
    assert "25 Stars" in keyboard[0][0]["text"]


def test_cloud_long_sku_still_fits_telegram_callback_limit(cloud):
    cloud.db.rpc.return_value = [
        {
            "source": "stock",
            "available": 1,
            "title": "Long SKU",
            "price_stars": 25,
            "description": "Test only",
            "sku": "a" * 64,
        }
    ]
    asyncio.run(cloud.flow.handle_callback(cloud, callback("cat:0")))
    data = cloud.tg.send_message.call_args.kwargs["keyboard"][0][0]["callback_data"]
    assert data.startswith("buyh:")
    assert len(data.encode()) <= 64


def test_cloud_short_product_token_resolves_to_trusted_catalog_sku(cloud):
    product = {
        "source": "stock",
        "available": 1,
        "active": True,
        "title": "Licensed download",
        "price_stars": 25,
        "description": "Test only",
        "sku": "licensed-product-" + "x" * 40,
    }
    cloud.db.rpc.return_value = [product]
    cloud.db.select_one.side_effect = [{"user_id": 101}, product]
    cloud.db.select.return_value = [{"id": 1}]
    token = cloud.flow.product_callback(product["sku"])

    asyncio.run(cloud.flow.handle_callback(cloud, callback(token)))

    assert cloud.db.insert.await_args.args[1]["sku"] == product["sku"]
    cloud.tg.send_invoice.assert_awaited_once()


def test_cloud_catalog_paginates_real_available_products(cloud):
    cloud.db.rpc.return_value = [
        {
            "source": "stock",
            "available": 2,
            "title": f"Product {index}",
            "price_stars": 10 + index,
            "description": "Test only",
            "category": "Digital",
            "sku": f"product-{index}",
        }
        for index in range(9)
    ]
    asyncio.run(cloud.flow.handle_callback(cloud, callback("cat:0")))
    keyboard = cloud.tg.send_message.call_args.kwargs["keyboard"]
    assert keyboard[-3][-1]["callback_data"] == "cat:1"
    assert sum(row[0]["callback_data"].startswith("buyh:") for row in keyboard[:8]) == 8


def test_cloud_order_menu_reports_real_status_counts(cloud):
    cloud.db.select.return_value = [
        {
            "id": "one",
            "title": "First",
            "price_stars": 10,
            "state": "delivered",
            "created_at": "2026-01-01",
        },
        {
            "id": "two",
            "title": "Second",
            "price_stars": 20,
            "state": "delivery_failed",
            "created_at": "2026-01-02",
        },
    ]
    asyncio.run(cloud.flow.handle_callback(cloud, callback("orders")))
    keyboard = cloud.tg.send_message.call_args.kwargs["keyboard"]
    labels = [row[0]["text"] for row in keyboard]
    assert "✅ Completed (1)" in labels
    assert "⚠ Needs attention (1)" in labels


def test_cloud_profile_uses_real_order_history(cloud):
    cloud.db.select.return_value = [
        {"price_stars": 25, "state": "delivered"},
        {"price_stars": 40, "state": "invoice"},
    ]
    asyncio.run(cloud.flow.handle_callback(cloud, callback("profile")))
    text = cloud.tg.send_message.call_args.args[1]
    assert "Completed purchases: 1" in text
    assert "Stars spent on delivered orders: 25" in text
