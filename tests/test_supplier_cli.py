import asyncio
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import aiohttp
import pytest
from test_supplier_store import BUYER_KEY, EMAIL, EVIDENCE, PASSWORD
from test_supplier_store import supplier_shop as supplier_shop

from shop import __main__ as cli
from shop.canboso import BALANCE_PATH, PRODUCTS_PATH, CanbosoError, HttpTransport, Reply
from shop.config import CanbosoSettings, Settings
from shop.store import ShopError, Store
from shop.supplier_store import SupplierState


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Live network access is forbidden in supplier CLI tests")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", blocked)
    monkeypatch.setattr(HttpTransport, "request", blocked)
    monkeypatch.setattr(cli.Bot, "__call__", blocked)


@pytest.fixture
def cli_env(tmp_path, key, monkeypatch):
    env = SimpleNamespace(settings=Settings(
        database_path=tmp_path / "cli.sqlite3", stock_encryption_key=key,
        admin_ids=frozenset({999}),
    ))
    env.load = Mock(side_effect=lambda path: env.settings)
    monkeypatch.setattr(cli, "load_settings", env.load)
    env.session_factory = Mock(side_effect=AssertionError("Unexpected supplier session"))
    monkeypatch.setattr(cli.aiohttp, "ClientSession", env.session_factory)

    def invoke(*args):
        monkeypatch.setattr(sys, "argv", [
            "shop", "--config", str(tmp_path / "local.json"), *map(str, args),
        ])
        return cli.main()

    env.invoke = invoke
    return env


@pytest.fixture
def supplier_http(cli_env, monkeypatch):
    cli_env.settings = replace(cli_env.settings, canboso=CanbosoSettings(
        enabled=True, api_key=BUYER_KEY,
    ))
    session = MagicMock()
    session.__aenter__.return_value = session
    factory = Mock(return_value=session)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", factory)
    network = SimpleNamespace(
        session=session, factory=factory, calls=[],
        products={"success": True, "products": [], "walletCurrency": "USD"},
        balance={"success": True, "balance": 42, "walletCurrency": "USD"},
        error=None, error_path=PRODUCTS_PATH, check=lambda: None,
    )

    async def request(transport, method, path, **kwargs):
        network.check()
        network.calls.append((transport, method, path, kwargs))
        assert method == "GET"
        assert kwargs == {"query": {"key": BUYER_KEY}}
        assert path in {PRODUCTS_PATH, BALANCE_PATH}
        if network.error is not None and path == network.error_path:
            raise network.error
        return Reply(200, network.products if path == PRODUCTS_PATH else network.balance)

    monkeypatch.setattr(HttpTransport, "request", request)
    return network


def open_store(settings, clock=lambda: 100.0):
    store = Store(settings.database_path, settings.stock_encryption_key, settings.environment, clock)
    store.initialize()
    store.supplier.configure(settings.canboso)
    return store


def attach_supplier_shop(env, shop, *, purchasing=False):
    env.settings = replace(
        env.settings, database_path=shop.store.path, environment="production",
        production_acknowledged=True,
        canboso=shop.settings if purchasing else CanbosoSettings(),
    )


def test_private_json_exclusive_creation_and_private_mode(tmp_path, monkeypatch):
    path = tmp_path / "private.json"
    real_open = os.open
    opened = Mock(wraps=real_open)
    monkeypatch.setattr(cli.os, "open", opened)
    cli.private_json(path, {"password": PASSWORD})
    opened.assert_called_once_with(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    original = path.read_bytes()
    assert json.loads(original) == {"password": PASSWORD}
    with pytest.raises(FileExistsError):
        cli.private_json(path, {"replacement": True})
    assert path.read_bytes() == original
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_private_json_requires_existing_parent(tmp_path):
    path = tmp_path / "missing" / "private.json"
    with pytest.raises(ShopError, match="parent directory"):
        cli.private_json(path, {})
    assert not path.parent.exists()


def test_sync_uses_one_session_transport_and_get_only(cli_env, supplier_http):
    store = open_store(cli_env.settings)
    result = asyncio.run(cli.sync_supplier(cli_env.settings, store))
    assert result == {
        "products": [], "walletCurrency": "USD", "balance": 42,
        "mode": "read-only; no purchases",
    }
    network = supplier_http
    network.factory.assert_called_once_with(trust_env=False)
    network.session.__aexit__.assert_awaited_once()
    assert [(c[1], c[2]) for c in network.calls] == [
        ("GET", PRODUCTS_PATH), ("GET", BALANCE_PATH),
    ]
    assert network.calls[0][0] is network.calls[1][0]
    assert network.calls[0][0].session is network.session
    with store.connection() as db:
        rows = db.execute("SELECT * FROM supplier_cache ORDER BY name").fetchall()
    assert [row["name"] for row in rows] == ["balance", "products"]
    for row, expected in zip(rows, (network.balance, network.products)):
        assert row["key_hash"] == cli_env.settings.canboso.key_fingerprint
        assert store.supplier.decrypt(row["ciphertext"]) == expected
        assert "walletCurrency" not in row["ciphertext"]


@pytest.mark.parametrize("path", [PRODUCTS_PATH, BALANCE_PATH])
@pytest.mark.parametrize("retry_after", [0, 123])
def test_sync_failure_closes_session_and_persists_cooldown(
    cli_env, supplier_http, path, retry_after,
):
    store = open_store(cli_env.settings)
    supplier_http.error = CanbosoError("supplier_rate_limited", retry_after=retry_after)
    supplier_http.error_path = path
    with pytest.raises(ShopError, match="^supplier_rate_limited$"):
        asyncio.run(cli.sync_supplier(cli_env.settings, store))
    assert store.supplier.cooldown_until() == 100 + (retry_after or 60)
    supplier_http.session.__aexit__.assert_awaited_once()
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM supplier_cache").fetchone()[0] == 0


def test_sync_cooldown_prevents_session_creation(cli_env, supplier_http):
    store = open_store(cli_env.settings)
    store.supplier.defer_network(60)
    with pytest.raises(ShopError, match="cooldown"):
        asyncio.run(cli.sync_supplier(cli_env.settings, store))
    supplier_http.factory.assert_not_called()
    assert not supplier_http.calls


def test_sync_cancellation_closes_session(cli_env, supplier_http):
    store = open_store(cli_env.settings)
    supplier_http.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli.sync_supplier(cli_env.settings, store))
    supplier_http.session.__aexit__.assert_awaited_once()


def test_cli_sync_holds_run_lock_through_export(
    cli_env, supplier_http, tmp_path, monkeypatch, capsys,
):
    expected_lock = cli_env.settings.database_path.with_suffix(".process.lock")
    paths = []
    held = []
    original_lock = cli.process_lock
    original_export = cli.private_json

    @contextmanager
    def tracked_lock(path):
        paths.append(path)
        with original_lock(path):
            held.append(path)
            try:
                yield
            finally:
                held.pop()

    def check_lock():
        assert held == [expected_lock]

    def export(path, result):
        check_lock()
        original_export(path, result)

    monkeypatch.setattr(cli, "process_lock", tracked_lock)
    monkeypatch.setattr(cli, "private_json", export)
    supplier_http.check = check_lock
    output = tmp_path / "snapshot.json"
    assert cli_env.invoke("supplier-sync", "--output", output) == 0
    assert paths == [expected_lock]
    assert not held
    assert json.loads(output.read_text())["balance"] == 42
    text = capsys.readouterr()
    assert BUYER_KEY not in text.out + text.err
    assert "walletCurrency" not in text.out

    cli_env.settings = replace(
        cli_env.settings, bot_token="123456:" + "x" * 30,
        support_contact="offline support", terms_text="Offline test terms. " * 3,
        privacy_text="Offline test privacy. " * 3,
    )

    async def run(settings, store):
        check_lock()
        assert store.supplier.settings == settings.canboso

    run_bot = AsyncMock(side_effect=run)
    monkeypatch.setattr(cli, "run_bot", run_bot)
    assert cli_env.invoke("run") == 0
    run_bot.assert_awaited_once()
    assert paths == [expected_lock, expected_lock]
    assert not held


def test_cli_sync_refuses_competing_bot(cli_env, supplier_http, tmp_path, capsys):
    output = tmp_path / "snapshot.json"
    with cli.process_lock(cli_env.settings.database_path.with_suffix(".process.lock")):
        assert cli_env.invoke("supplier-sync", "--output", output) == 1
    supplier_http.factory.assert_not_called()
    assert not output.exists()
    assert "Another bot process" in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["existing", "missing_parent", "disabled"])
def test_cli_sync_rejects_before_network(cli_env, supplier_http, tmp_path, problem):
    output = tmp_path / "snapshot.json"
    if problem == "existing":
        output.write_text("keep this file", encoding="utf-8")
    elif problem == "missing_parent":
        output = tmp_path / "missing" / "snapshot.json"
    else:
        cli_env.settings = replace(cli_env.settings, canboso=CanbosoSettings())
    assert cli_env.invoke("supplier-sync", "--output", output) == 1
    supplier_http.factory.assert_not_called()
    if problem == "existing":
        assert output.read_text() == "keep this file"
    else:
        assert not output.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_doctor_reports_installed_flags_without_live_verification(
    cli_env, enabled, monkeypatch, capsys,
):
    cli_env.settings = replace(cli_env.settings, canboso=CanbosoSettings(
        enabled=enabled, api_key=BUYER_KEY if enabled else "",
    ))
    configured = []
    original = SupplierState.configure

    def configure(state, settings):
        with state.store.connection() as db:
            db.execute("SELECT * FROM supplier_intents")
        configured.append(settings)
        original(state, settings)

    monkeypatch.setattr(SupplierState, "configure", configure)
    assert cli_env.invoke("doctor") == 0
    text = capsys.readouterr().out
    assert "Supplier integration: installed (Canboso)" in text
    assert f"enabled={enabled}" in text
    assert "allow_purchases=False" in text
    assert "resale_authorized=False" in text
    assert "acknowledge_price_race=False" in text
    assert "no live verification" in text
    assert BUYER_KEY not in text
    assert configured == [cli_env.settings.canboso]
    cli_env.session_factory.assert_not_called()


def test_doctor_reports_invalid_local_flags(cli_env, capsys):
    cli_env.settings = replace(cli_env.settings, canboso=CanbosoSettings(allow_purchases=True))
    assert cli_env.invoke("doctor") == 1
    text = capsys.readouterr().out
    assert "NEEDS SETUP" in text
    assert "allow_purchases=True" in text
    assert "no live verification" in text
    cli_env.session_factory.assert_not_called()


def test_run_still_requires_bot_settings(cli_env, monkeypatch, capsys):
    run_bot = AsyncMock()
    monkeypatch.setattr(cli, "run_bot", run_bot)
    assert cli_env.invoke("run") == 1
    assert "bot_token" in capsys.readouterr().err
    run_bot.assert_not_awaited()
    assert not cli_env.settings.database_path.exists()


@pytest.mark.parametrize("arguments", [
    ["supplier-sync"],
    ["supplier-review", "unexpected"],
    ["supplier-inspect", "order", "--output", "unused.json"],
    ["supplier-inspect", "order", "--confirm-sensitive-export"],
    ["supplier-resolve", "order", "stop_for_refund", "--evidence-file", "evidence.txt",
     "--operator-id", "999"],
    ["supplier-resolve", "order", "stop_for_refund", "--operator-id", "999", "--confirm"],
    ["supplier-resolve", "order", "stop_for_refund", "--evidence-file", "evidence.txt",
     "--confirm"],
    ["supplier-resolve", "order", "invalid", "--evidence-file", "evidence.txt",
     "--operator-id", "999", "--confirm"],
    ["supplier-resolve", "order", "fulfill", "--evidence-file", "evidence.txt",
     "--operator-id", "not-an-int", "--confirm"],
])
def test_cli_parser_rejects_incomplete_or_invalid_arguments(cli_env, arguments):
    with pytest.raises(SystemExit) as error:
        cli_env.invoke(*arguments)
    assert error.value.code == 2
    cli_env.load.assert_not_called()
    assert not cli_env.settings.database_path.exists()


def test_cli_help_warns_to_stop_bot_for_sync(cli_env, capsys):
    with pytest.raises(SystemExit) as error:
        cli_env.invoke("--help")
    assert error.value.code == 0
    text = capsys.readouterr().out
    assert "stop the bot first" in " ".join(text.split())
    for command in ("supplier-sync", "supplier-review", "supplier-inspect", "supplier-resolve"):
        assert command in text


def test_review_recovers_interrupted_purchase_and_prints_only_safe_states(
    cli_env, supplier_shop, capsys,
):
    shop = supplier_shop
    order = shop.paid_order("slot", email=EMAIL)
    assert shop.store.supplier.claim()["order_id"] == order["id"]
    attach_supplier_shop(cli_env, shop)
    assert cli_env.invoke("supplier-review") == 0
    text = capsys.readouterr().out
    rows = json.loads(text)
    assert len(rows) == 1
    assert rows[0]["order_id"] == order["id"]
    assert rows[0]["state"] == "uncertain"
    assert rows[0]["hold_reason"] == "process_interrupted"
    assert set(rows[0]) == {
        "order_id", "state", "supplier_reference", "hold_reason", "resolution_version", "user_id",
    }
    assert EMAIL not in text and BUYER_KEY not in text
    assert shop.intent(order)["state"] == "uncertain"
    cli_env.session_factory.assert_not_called()


def test_inspect_exports_sensitive_evidence_only_to_new_local_file(
    cli_env, supplier_shop, tmp_path, capsys,
):
    shop = supplier_shop
    order = shop.paid_order("slot", email=EMAIL)
    raw = {"email": EMAIL, "password": PASSWORD, "raw": "private supplier evidence"}
    shop.store.supplier.fail(order["id"], "needs_review", raw=raw)
    attach_supplier_shop(cli_env, shop)
    output = tmp_path / "sensitive.json"
    args = ("supplier-inspect", order["id"], "--output", output, "--confirm-sensitive-export")
    assert cli_env.invoke(*args) == 0
    exported = json.loads(output.read_text())
    assert exported == shop.store.supplier.inspect(order["id"])
    assert exported["response"] == raw
    assert "key" not in exported["request"]
    original = output.read_bytes()
    assert cli_env.invoke(*args) == 1
    assert output.read_bytes() == original
    text = capsys.readouterr()
    for secret in (EMAIL, PASSWORD, BUYER_KEY, raw["raw"]):
        assert secret not in text.out + text.err
    cli_env.session_factory.assert_not_called()


@pytest.mark.parametrize("operator", [0, -1, 123])
def test_resolve_rejects_non_admin_before_reading_evidence(
    cli_env, tmp_path, operator, monkeypatch, capsys,
):
    resolve = Mock()
    read = Mock(side_effect=AssertionError("Evidence must not be read for non-admins"))
    monkeypatch.setattr(SupplierState, "resolve", resolve)
    monkeypatch.setattr(cli, "read_private_text", read)
    assert cli_env.invoke(
        "supplier-resolve", "order", "stop_for_refund", "--evidence-file", tmp_path / "missing",
        "--operator-id", operator, "--confirm",
    ) == 1
    resolve.assert_not_called()
    read.assert_not_called()
    assert "configured positive admin ID" in capsys.readouterr().err


@pytest.mark.parametrize("kind,limit", [("evidence", 10_000), ("delivery", 1_000_000)])
@pytest.mark.parametrize("oversized", [False, True])
def test_resolve_enforces_byte_limits_before_calling_store(
    cli_env, tmp_path, monkeypatch, capsys, kind, limit, oversized,
):
    evidence = tmp_path / "evidence.txt"
    delivery = tmp_path / "delivery.txt"
    evidence.write_text(EVIDENCE, encoding="utf-8")
    delivery.write_text(PASSWORD, encoding="utf-8")
    target = evidence if kind == "evidence" else delivery
    payload = ("\u00e9" * (limit // 2)).encode("utf-8") + (b"!" if oversized else b"")
    target.write_bytes(payload)
    resolve = Mock()
    monkeypatch.setattr(SupplierState, "resolve", resolve)
    result = cli_env.invoke(
        "supplier-resolve", "order", "fulfill", "--evidence-file", evidence,
        "--operator-id", "999", "--confirm", "--delivery-file", delivery,
    )
    assert result == int(oversized)
    if oversized:
        resolve.assert_not_called()
        assert f"exceeds {limit} bytes" in capsys.readouterr().err
    else:
        resolve.assert_called_once_with(
            "order", "fulfill", evidence.read_text(encoding="utf-8"), "999",
            delivery=delivery.read_text(encoding="utf-8"),
        )
    cli_env.session_factory.assert_not_called()


def test_bounded_read_catches_file_growth(tmp_path, monkeypatch):
    path = tmp_path / "evidence.txt"
    path.write_bytes(b"x" * 10_001)
    monkeypatch.setattr(Path, "stat", lambda self: SimpleNamespace(st_size=0))
    with pytest.raises(ShopError, match="10000 bytes"):
        cli.read_private_text(path, 10_000, "Evidence")


@pytest.mark.parametrize("kind", ["evidence", "delivery"])
def test_resolve_invalid_utf8_never_echoes_contents(
    cli_env, tmp_path, monkeypatch, capsys, kind,
):
    evidence, delivery = tmp_path / "evidence.txt", tmp_path / "delivery.txt"
    evidence.write_text(EVIDENCE, encoding="utf-8")
    delivery.write_text(PASSWORD, encoding="utf-8")
    (evidence if kind == "evidence" else delivery).write_bytes(PASSWORD.encode() + b"\xff")
    resolve = Mock()
    monkeypatch.setattr(SupplierState, "resolve", resolve)
    assert cli_env.invoke(
        "supplier-resolve", "order", "fulfill", "--evidence-file", evidence,
        "--operator-id", "999", "--confirm", "--delivery-file", delivery,
    ) == 1
    resolve.assert_not_called()
    text = capsys.readouterr()
    assert "UTF-8" in text.err
    assert PASSWORD not in text.out + text.err


@pytest.mark.parametrize("action,delivery", [("fulfill", False), ("stop_for_refund", True)])
def test_resolve_requires_delivery_only_for_fulfill(cli_env, tmp_path, action, delivery, capsys):
    args = ["supplier-resolve", "order", action, "--evidence-file", tmp_path / "missing",
            "--operator-id", "999", "--confirm"]
    if delivery:
        args += ["--delivery-file", tmp_path / "missing-delivery"]
    assert cli_env.invoke(*args) == 1
    assert "--delivery-file" in capsys.readouterr().err


@pytest.mark.parametrize("action,expected", [
    ("retry_same_request", "retry_approved"),
    ("stop_for_refund", "resolved_for_refund"),
    ("fulfill", "completed"),
])
def test_resolve_records_evidence_body_and_admin_offline(
    cli_env, supplier_shop, tmp_path, capsys, action, expected,
):
    shop = supplier_shop
    order = shop.paid_order()
    shop.store.supplier.fail(order["id"], "uncertain")
    attach_supplier_shop(cli_env, shop, purchasing=action == "retry_same_request")
    evidence, delivery = tmp_path / "evidence.txt", tmp_path / "delivery.txt"
    evidence.write_bytes(b"\xef\xbb\xbf" + EVIDENCE.encode())
    delivery.write_text(PASSWORD, encoding="utf-8")
    args = ["supplier-resolve", order["id"], action, "--evidence-file", evidence,
            "--operator-id", "999", "--confirm"]
    if action == "fulfill":
        args += ["--delivery-file", delivery]
    assert cli_env.invoke(*args) == 0
    assert shop.intent(order)["state"] == expected
    audit = shop.rows("supplier_audit")
    assert len(audit) == 1
    assert audit[0]["operator_id"] == "999"
    assert audit[0]["action"] == action
    assert shop.store.supplier.decrypt(audit[0]["evidence_ciphertext"]) == EVIDENCE
    assert EVIDENCE not in audit[0]["evidence_ciphertext"]
    text = capsys.readouterr()
    for secret in (EVIDENCE, PASSWORD, BUYER_KEY):
        assert secret not in text.out + text.err
    assert not shop.transport.posts
    shop.bot.refund_star_payment.assert_not_awaited()
    cli_env.session_factory.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("outcome", ["return", "error", "cancel", "setup_error"])
def test_run_lifecycle_closes_workers_session_and_dispatcher(
    cli_env, monkeypatch, enabled, outcome,
):
    settings = replace(cli_env.settings, canboso=CanbosoSettings(
        enabled=enabled, api_key=BUYER_KEY if enabled else "",
    ))
    store = Mock()
    events = []
    tasks = []
    bot = MagicMock()
    bot.__aenter__.return_value = bot
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=123456))
    bot.get_webhook_info = AsyncMock(return_value=SimpleNamespace(url=""))
    bot.set_my_commands = AsyncMock()
    bot_factory = Mock(return_value=bot)
    monkeypatch.setattr(cli, "Bot", bot_factory)
    telegram_session = Mock()
    monkeypatch.setattr(cli, "AiohttpSession", Mock(return_value=telegram_session))

    async def worker_run(name):
        tasks.append(asyncio.current_task())
        events.append(name + " started")
        try:
            await asyncio.Future()
        finally:
            events.append(name + " stopped")

    delivery = SimpleNamespace(run=lambda: worker_run("delivery"), also_wake=[])
    supplier = SimpleNamespace(run=lambda: worker_run("supplier"), kick=lambda: None)
    delivery_factory = Mock(return_value=delivery)
    supplier_factory = Mock(return_value=supplier)
    monkeypatch.setattr(cli, "DeliveryWorker", delivery_factory)
    monkeypatch.setattr(cli, "SupplierWorker", supplier_factory)
    dispatcher = SimpleNamespace(storage=SimpleNamespace(
        close=AsyncMock(side_effect=lambda: events.append("storage closed")),
    ))

    def build(settings, store, worker):
        store.supplier.configure(settings.canboso)
        return dispatcher

    build_dispatcher = Mock(side_effect=build)
    monkeypatch.setattr(cli, "build_dispatcher", build_dispatcher)
    session = MagicMock()
    session.__aenter__.return_value = session
    session.__aexit__.side_effect = lambda *args: events.append("session closed")
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(cli.aiohttp, "ClientSession", session_factory)
    transport = Mock()
    transport_factory = Mock(return_value=transport)
    monkeypatch.setattr(cli, "HttpTransport", transport_factory)
    client = Mock()
    client_factory = Mock(return_value=client)
    monkeypatch.setattr(cli, "CanbosoClient", client_factory)
    if enabled and outcome == "setup_error":
        supplier_factory.side_effect = RuntimeError("setup failed")

    async def poll():
        await asyncio.sleep(0)
        assert "delivery started" in events
        assert ("supplier started" in events) == enabled
        if outcome == "error":
            raise RuntimeError("polling failed")
        if outcome == "cancel":
            raise asyncio.CancelledError()

    polling = SimpleNamespace(run=AsyncMock(side_effect=poll))
    polling_factory = Mock(return_value=polling)
    monkeypatch.setattr(cli, "DurablePolling", polling_factory)

    async def exercise():
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await cli.run_bot(settings, store)
        elif outcome == "error" or (enabled and outcome == "setup_error"):
            with pytest.raises(RuntimeError):
                await cli.run_bot(settings, store)
        else:
            await cli.run_bot(settings, store)
        assert all(task.done() for task in tasks)
        assert not (asyncio.all_tasks() - {asyncio.current_task()})

    asyncio.run(exercise())
    delivery_factory.assert_called_once_with(store, bot, settings.admin_ids)
    build_dispatcher.assert_called_once_with(settings, store, delivery)
    store.supplier.configure.assert_called_once_with(settings.canboso)
    store.bind_bot.assert_called_once_with(123456)
    dispatcher.storage.close.assert_awaited_once()
    bot.__aexit__.assert_awaited_once()
    assert events[-1] == "storage closed"
    if enabled:
        session_factory.assert_called_once_with(trust_env=False)
        transport_factory.assert_called_once_with(session)
        client_factory.assert_called_once_with(settings.canboso, transport, settings.environment)
        supplier_factory.assert_called_once_with(store, client, delivery)
        session.__aexit__.assert_awaited_once()
        if outcome == "setup_error":
            polling_factory.assert_not_called()
            assert not tasks
        else:
            assert events.index("delivery stopped") < events.index("session closed")
            assert events.index("supplier stopped") < events.index("session closed")
    else:
        session_factory.assert_not_called()
        transport_factory.assert_not_called()
        client_factory.assert_not_called()
        supplier_factory.assert_not_called()
    if not (enabled and outcome == "setup_error"):
        assert polling_factory.call_args == call(bot, dispatcher, store)
        assert "delivery stopped" in events
