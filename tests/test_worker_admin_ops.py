"""Admin ops endpoints: failed-update inspection/retry, health, audit list.

The PostgREST boundary and Telegram are mocked; the real worker/src/admin.py
code drives every assertion.
"""

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
    monkeypatch.setitem(sys.modules, "fernet", SimpleNamespace(
        Fernet=lambda *a, **k: SimpleNamespace()))
    modules = {}
    for name in ("db", "telegram", "admin"):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["admin"]


@pytest.fixture
def adm(admin_module, monkeypatch):
    env = SimpleNamespace(
        SUPABASE_URL="https://example.invalid",
        SUPABASE_SERVICE_ROLE_KEY="key",
        STOCK_FERNET_KEY="key",
        TELEGRAM_BOT_TOKEN="token",
        OWNER_EMAILS="owner@example.com",
        DASHBOARD_ORIGIN="",
    )
    instance = admin_module.Admin(env)
    instance.db = SimpleNamespace(
        select=AsyncMock(return_value=[]),
        select_one=AsyncMock(return_value=None),
        rpc=AsyncMock(return_value=True),
        insert=AsyncMock(),
    )
    instance.tg = SimpleNamespace(call=AsyncMock(return_value={
        "url": "https://worker.example/webhook/abc",
        "pending_update_count": 0,
    }))
    flow = SimpleNamespace(handle_update=AsyncMock())
    monkeypatch.setitem(sys.modules, "flow", flow)
    instance.flow = flow
    return instance


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_failed_updates_never_select_payloads(adm):
    run(adm.list_failed_updates("owner@example.com"))
    params = adm.db.select.call_args.args[1]
    assert params["state"] == "eq.failed"
    assert "payload" not in params["select"]


def test_retry_rejects_bad_ids(adm):
    for bad in (None, "1", 0, -5, 1.5, True):
        with pytest.raises(adm.__class__.__module__ and Exception) as err:
            run(adm.retry_update("owner@example.com", {"update_id": bad}))
        assert "positive integer" in str(err.value)


def test_retry_requires_failed_state(adm):
    adm.db.select_one.return_value = {"update_id": 5, "state": "done", "payload": {}}
    with pytest.raises(Exception) as err:
        run(adm.retry_update("owner@example.com", {"update_id": 5}))
    assert "only failed updates" in str(err.value)
    adm.flow.handle_update.assert_not_called()


def test_retry_requires_stored_payload(adm):
    adm.db.select_one.return_value = {"update_id": 5, "state": "failed", "payload": None}
    with pytest.raises(Exception) as err:
        run(adm.retry_update("owner@example.com", {"update_id": 5}))
    assert "no stored payload" in str(err.value)
    adm.flow.handle_update.assert_not_called()


def test_retry_rearms_and_reports_final_state(adm):
    payload = {"update_id": 5, "message": {"text": "/start"}}
    adm.db.select_one.side_effect = [
        {"update_id": 5, "state": "failed", "payload": payload},
        {"update_id": 5, "kind": "message", "state": "done", "attempts": 2,
         "last_error": None, "updated_at": "2026-09-18T00:00:00Z"},
    ]
    result = run(adm.retry_update("owner@example.com", {"update_id": 5}))
    assert result["state"] == "done"
    adm.db.rpc.assert_any_call("rearm_failed_update", {"p_update_id": 5})
    adm.flow.handle_update.assert_called_once()
    assert adm.flow.handle_update.call_args.args[0] == payload
    audit = adm.db.insert.call_args.args
    assert audit[0] == "admin_audit" and audit[1]["action"] == "update_retry"


def test_health_reports_webhook_and_stuck_work(adm):
    adm.db.select.side_effect = [
        [{"update_id": 9, "kind": "message", "attempts": 3, "updated_at": "x"}],
        [],
    ]
    result = run(adm.health("owner@example.com"))
    assert result["webhook"]["url"].startswith("https://")
    assert len(result["stuck_updates"]) == 1
    assert result["stuck_deliveries"] == []


def test_health_survives_telegram_outage(adm):
    adm.tg.call.side_effect = RuntimeError("telegram down")
    result = run(adm.health("owner@example.com"))
    assert result["webhook"] is None
    assert result["db"] == "ok"


def test_audit_list_is_bounded(adm):
    run(adm.list_audit("owner@example.com"))
    assert adm.db.select.call_args.args[0] == "admin_audit"
    assert adm.db.select.call_args.kwargs["limit"] == 100
