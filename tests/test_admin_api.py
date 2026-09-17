"""Authorization and validation tests for the Worker's private admin API.

The Supabase boundary is mocked at the DB layer; every test drives the real
worker/src/admin.py request handlers.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def admin_module(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "worker/src"
    monkeypatch.setitem(sys.modules, "httpclient", SimpleNamespace(
        request=AsyncMock(side_effect=AssertionError("Network access forbidden in tests"))))
    monkeypatch.setitem(sys.modules, "fernet", SimpleNamespace(Fernet=object))
    for name in ("storefront", "db", "telegram", "admin"):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        if name == "admin":
            return module


def make_admin(admin_module, *, owner_emails="owner@example.com"):
    admin = admin_module.Admin.__new__(admin_module.Admin)
    admin.db = SimpleNamespace(
        rpc=AsyncMock(return_value={}), select=AsyncMock(return_value=[]),
        select_one=AsyncMock(return_value=None), insert=AsyncMock(),
        update=AsyncMock(), upsert=AsyncMock(),
        request_auth_user=AsyncMock(return_value={"email": "owner@example.com"}),
    )
    admin.fernet = SimpleNamespace(
        encrypt=AsyncMock(side_effect=lambda x: f"ct:{x}"),
        fingerprint=lambda p: f"fp:{p}",
    )
    admin.tg = SimpleNamespace(send_message=AsyncMock(), call=AsyncMock())
    admin.owner_emails = {owner_emails}
    admin.env = SimpleNamespace()
    return admin


def request(token="tok", method="POST", body=None, content_length=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: headers.get(k.lower(), d)),
        method=method,
        json=AsyncMock(return_value=body),
    )


# ---- authorization ----

def test_missing_token_rejected(admin_module):
    admin = make_admin(admin_module)
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.authorize(request(token=None)))
    assert e.value.status == 401


def test_invalid_session_rejected(admin_module):
    admin = make_admin(admin_module)
    admin.db.request_auth_user.return_value = None
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.authorize(request()))
    assert e.value.status == 401


def test_non_owner_authenticated_user_rejected(admin_module):
    admin = make_admin(admin_module)
    admin.db.request_auth_user.return_value = {"email": "buyer@example.com"}
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.authorize(request()))
    assert e.value.status == 403


def test_owner_accepted(admin_module):
    admin = make_admin(admin_module)
    assert asyncio.run(admin.authorize(request())) == "owner@example.com"


def test_oversized_body_rejected(admin_module):
    admin = make_admin(admin_module)
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.body(request(content_length=10_000_000)))
    assert e.value.status == 413


# ---- products ----

def test_create_product_defaults_inactive_and_validates(admin_module):
    admin = make_admin(admin_module)
    result = asyncio.run(admin.create_product("owner@example.com", {
        "sku": "win11-key", "title": "Windows 11 Key", "description": "d",
        "category": "software", "price_stars": 250,
    }))
    row = [c.args[1] for c in admin.db.insert.call_args_list if c.args[0] == "products"][0]
    assert row["active"] is False and row["source"] == "stock"
    assert result["ok"] is True
    # audit was written
    assert any(c.args[0] == "admin_audit" for c in admin.db.insert.call_args_list)


@pytest.mark.parametrize("bad", [
    {"sku": "!!", "title": "t", "price_stars": 10},
    {"sku": "ok-sku", "title": "", "price_stars": 10},
    {"sku": "ok-sku", "title": "t", "price_stars": 0},
    {"sku": "ok-sku", "title": "t", "price_stars": 12.5},
    {"sku": "ok-sku", "title": "t", "price_stars": True},
])
def test_create_product_rejects_bad_input(admin_module, bad):
    admin = make_admin(admin_module)
    with pytest.raises(admin_module.AdminError):
        asyncio.run(admin.create_product("owner@example.com", bad))


def test_create_product_rejects_duplicate_sku(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.return_value = {"sku": "taken"}
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.create_product("owner@example.com", {
            "sku": "taken", "title": "t", "price_stars": 10}))
    assert e.value.status == 409


# ---- stock ----

def test_stock_upload_encrypts_and_skips_duplicates(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.return_value = {"sku": "s1", "source": "stock"}
    admin.db.select.return_value = [{"fingerprint": "fp:dup"}]  # already stored
    result = asyncio.run(admin.upload_stock("owner@example.com", {
        "sku": "s1", "lines": ["new-code-1", "dup", "new-code-2", "dup"],
    }))
    assert result == {"ok": True, "accepted": 2, "duplicates": 2, "rejected": 0}
    inserted = [c.args[1] for c in admin.db.insert.call_args_list if c.args[0] == "stock"]
    assert len(inserted) == 2
    assert all(r["state"] == "available" and r["ciphertext"].startswith("ct:") for r in inserted)
    # plaintext never reaches the audit log
    audit = [c.args[1] for c in admin.db.insert.call_args_list if c.args[0] == "admin_audit"][0]
    assert "new-code-1" not in str(audit)


def test_stock_upload_rejects_control_characters(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.return_value = {"sku": "s1", "source": "stock"}
    with pytest.raises(admin_module.AdminError):
        asyncio.run(admin.upload_stock("owner@example.com", {
            "sku": "s1", "lines": ["bad\x00code"]}))


# ---- orders ----

def test_resend_uses_existing_assignment_only(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.side_effect = [
        {"id": "ord_1", "user_id": 101, "state": "delivery_failed"},
        {"ciphertext": "ct"},
    ]
    admin.fernet.decrypt = AsyncMock(return_value="THE-CODE")
    result = asyncio.run(admin.resend_order("owner@example.com", {"order_id": "ord_1"}))
    assert result["ok"] is True
    assert "THE-CODE" in admin.tg.send_message.call_args.args[1]
    assert admin.db.rpc.call_args.args[0] == "confirm_delivery"


def test_resend_without_assignment_rejected(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.side_effect = [{"id": "ord_1", "user_id": 101}, None]
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.resend_order("owner@example.com", {"order_id": "ord_1"}))
    assert e.value.status == 409


def test_refund_requires_double_confirmation_and_charge(admin_module):
    admin = make_admin(admin_module)
    with pytest.raises(admin_module.AdminError):
        asyncio.run(admin.refund_order("owner@example.com", {"order_id": "o", "confirm": False}))
    admin.db.select_one.return_value = {"id": "o", "state": "delivered", "charge_id": None}
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.refund_order("owner@example.com", {"order_id": "o", "confirm": True}))
    assert e.value.status == 409


def test_refund_calls_telegram_then_records_and_audits(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.return_value = {
        "id": "ord_1", "state": "delivered", "charge_id": "chg_1",
        "user_id": 101, "price_stars": 50,
    }
    result = asyncio.run(admin.refund_order("owner@example.com",
                                            {"order_id": "ord_1", "confirm": True}))
    assert result["state"] == "refunded"
    assert admin.tg.call.call_args.args[0] == "refundStarPayment"
    assert admin.db.rpc.call_args.args[0] == "record_refund"


def test_failed_telegram_refund_is_tracked_not_recorded(admin_module):
    admin = make_admin(admin_module)
    admin.db.select_one.return_value = {
        "id": "ord_1", "state": "delivered", "charge_id": "chg_1",
        "user_id": 101, "price_stars": 50,
    }
    admin.tg.call.side_effect = RuntimeError("telegram rejected")
    with pytest.raises(admin_module.AdminError) as e:
        asyncio.run(admin.refund_order("owner@example.com",
                                       {"order_id": "ord_1", "confirm": True}))
    assert e.value.status == 502
    assert admin.db.rpc.await_count == 0  # record_refund never called
    audit = [c.args[1] for c in admin.db.insert.call_args_list if c.args[0] == "admin_audit"][0]
    assert audit["action"] == "order.refund_failed"


# ---- settings ----

def test_settings_update_upserts_and_flags_terms_change(admin_module):
    admin = make_admin(admin_module)
    result = asyncio.run(admin.update_settings("owner@example.com", {
        "shop_name": "New Name", "terms_text": "new terms", "checkout_paused": True,
    }))
    keys = {c.args[1]["key"] for c in admin.db.upsert.call_args_list}
    assert keys == {"shop_name", "terms_text", "checkout_paused"}
    assert result["terms_changed"] is True and result["note"]


def test_settings_rejects_unknown_keys(admin_module):
    admin = make_admin(admin_module)
    with pytest.raises(admin_module.AdminError):
        asyncio.run(admin.update_settings("owner@example.com", {"service_key": "nope"}))
