"""Run with python -m shop. Only 'run' and read-only 'supplier-sync' use the network."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TEST
from aiogram.types import BotCommand
from cryptography.fernet import Fernet

from .bot import build_dispatcher
from .canboso import CanbosoClient, CanbosoError, HttpTransport
from .config import PROJECT_ROOT, CanbosoSettings, initialize_config, load_settings
from .delivery import DeliveryWorker
from .polling import DurablePolling
from .store import ShopError, Store
from .supplier_worker import SupplierWorker


@contextmanager
def process_lock(path: Path):
    """An OS lock prevents two local polling processes for the same database."""
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ShopError("Another bot process is already using this database") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


async def run_bot(settings, store: Store) -> None:
    session = AiohttpSession(api=TEST if settings.environment == "test" else PRODUCTION, timeout=30)
    async with Bot(token=settings.bot_token, session=session) as bot:
        me = await bot.get_me()
        store.bind_bot(me.id)
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ShopError(
                "This bot has an active webhook. Disable it deliberately before using "
                "polling; this program will not remove it or drop pending updates"
            )
        await bot.set_my_commands(
            [
                BotCommand(command="shop", description="Browse digital products"),
                BotCommand(command="orders", description="Your orders and deliveries"),
                BotCommand(command="paysupport", description="Purchase and refund support"),
                BotCommand(command="terms", description="Shop terms and consent"),
                BotCommand(command="privacy", description="Privacy notice"),
                BotCommand(command="whoami", description="Your Telegram user ID"),
            ]
        )
        worker = DeliveryWorker(store, bot, settings.admin_ids)
        dispatcher = build_dispatcher(settings, store, worker)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(dispatcher.storage.close)
            supplier = None
            if settings.canboso.enabled:
                supplier_session = await resources.enter_async_context(
                    aiohttp.ClientSession(trust_env=False)
                )
                transport = HttpTransport(supplier_session)
                client = CanbosoClient(settings.canboso, transport, settings.environment)
                supplier = SupplierWorker(store, client, worker)
                worker.also_wake.append(supplier.kick)
            tasks = []
            try:
                tasks.append(asyncio.create_task(worker.run()))
                if supplier is not None:
                    tasks.append(asyncio.create_task(supplier.run()))
                print(
                    f"Shop running in {settings.environment}; "
                    f"sales {'enabled' if settings.enable_sales else 'paused'}."
                )
                await DurablePolling(bot, dispatcher, store).run()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def offline_demo() -> dict:
    """Exercise real shop logic with a fake transport, no Telegram account needed."""
    folder = PROJECT_ROOT / "data" / "demo" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    store = Store(folder / "shop.sqlite3", Fernet.generate_key().decode())
    store.initialize()
    store.supplier.configure(CanbosoSettings())
    product = json.loads((PROJECT_ROOT / "sample" / "catalog.json").read_text(encoding="utf-8"))[0]
    store.upsert_product(product)
    store.import_stock(product["sku"], ["DEMO-NOT-A-REAL-LICENSE-001"])
    store.accept_terms(101, "demo-v1")
    order = store.create_order(101, product["sku"], "demo-v1")
    store.approve_checkout(order["id"], 101, "XTR", product["price_stars"], "demo-query", "demo-v1")
    payment = store.record_payment(order["id"], 101, "XTR", product["price_stars"], "demo-charge")
    repeated = store.record_payment(order["id"], 101, "XTR", product["price_stars"], "demo-charge")

    class FakeTransport:
        def __init__(self):
            self.messages = []

        async def send_message(self, *args, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(message_id=len(self.messages))

    transport = FakeTransport()
    worker = DeliveryWorker(store, transport, frozenset())
    await worker.deliver(order["id"])
    await worker.deliver(order["id"])
    delivered = store.get_order(order["id"])["state"] == "delivered"
    store.begin_refund(payment.event_id)
    store.finish_refund(payment.event_id)
    result = {
        "mode": "offline simulation",
        "network_calls": 0,
        "checkout_currency": "XTR",
        "payment_recorded": payment.status == "accepted",
        "duplicate_payment_detected": repeated.duplicate,
        "delivered_after_payment": delivered,
        "delivery_messages": len(transport.messages),
        "refund_state_tested": store.get_order(order["id"])["state"] == "refunded",
        "refunded_stock_quarantined": store.stats()["stock"].get("quarantined") == 1,
        "real_payment_or_refund": False,
    }
    if not all(
        (
            result["payment_recorded"],
            result["duplicate_payment_detected"],
            result["delivered_after_payment"],
            result["delivery_messages"] == 1,
            result["refund_state_tested"],
            result["refunded_stock_quarantined"],
        )
    ):
        raise RuntimeError("Offline demonstration invariant failed")
    return result


def private_json(path: Path, result: dict) -> None:
    if not path.parent.is_dir():
        raise ShopError("Output parent directory must exist")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


async def sync_supplier(settings, store: Store) -> dict:
    if store.supplier.cooldown_until() > store.clock():
        raise ShopError("Supplier cooldown active; wait before synchronizing again")
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            client = CanbosoClient(settings.canboso, HttpTransport(session), settings.environment)
            products, balance = await client.products(), await client.balance()
            await asyncio.to_thread(store.supplier.cache_snapshot, products, balance)
            return {"products": products["products"], "walletCurrency": balance["walletCurrency"],
                    "balance": balance["balance"], "mode": "read-only; no purchases"}
    except CanbosoError as exc:
        store.supplier.defer_network(exc.retry_after or 60)
        raise ShopError(exc.code) from None


def read_private_text(path: Path, limit: int, label: str) -> str:
    if path.stat().st_size > limit:
        raise ShopError(f"{label} file exceeds {limit} bytes")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ShopError(f"{label} file exceeds {limit} bytes")
    try:
        return content.decode("utf-8-sig")
    except UnicodeError:
        raise ShopError(f"{label} file must contain UTF-8 text") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Digital Shelf - Telegram Stars shop")
    parser.add_argument("--config", type=Path, help="Local JSON configuration path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create local configuration with a unique encryption key")
    sub.add_parser("doctor", help="Check local setup without contacting Telegram")
    demo = sub.add_parser("demo", help="Run an offline checkout, delivery and refund simulation")
    demo.add_argument("--output", type=Path, help="Optional JSON result path")
    sub.add_parser(
        "seed-demo", help="Load sample catalog and non-redeemable inventory in test mode"
    )
    catalog = sub.add_parser("import-catalog", help="Import your product definitions")
    catalog.add_argument("file", type=Path)
    stock = sub.add_parser("stock", help="Import UTF-8 inventory, one code or URL per line")
    stock.add_argument("--sku", required=True)
    stock.add_argument("--file", type=Path, required=True)
    sync = sub.add_parser(
        "supplier-sync",
        help="Export supplier products/balance using GET only; stop the bot first to share its quota",
    )
    sync.add_argument("--output", type=Path, required=True, help="New local JSON file (no overwrite)")
    sub.add_parser("supplier-review", help="Recover interrupted purchases and list safe local states")
    inspect = sub.add_parser("supplier-inspect", help="Export sensitive supplier evidence locally only")
    inspect.add_argument("order")
    inspect.add_argument("--output", type=Path, required=True, help="New private local JSON file")
    inspect.add_argument(
        "--confirm-sensitive-export", action="store_true", required=True,
        help="Acknowledge the export may contain raw responses, email addresses and passwords",
    )
    resolve = sub.add_parser("supplier-resolve", help="Record an admin-verified resolution offline")
    resolve.add_argument("order")
    resolve.add_argument("action", choices=("retry_same_request", "stop_for_refund", "fulfill"))
    resolve.add_argument(
        "--evidence-file", type=Path, required=True,
        help="Local UTF-8 verification evidence (max 10000 bytes); never put evidence in shell history",
    )
    resolve.add_argument("--operator-id", type=int, required=True, help="Configured Telegram admin ID")
    resolve.add_argument("--confirm", action="store_true", required=True)
    resolve.add_argument("--delivery-file", type=Path, help="Verified UTF-8 delivery for fulfill (max 1 MB)")
    sub.add_parser("run", help="Connect the configured bot and begin polling")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "init":
            path = initialize_config(args.config)
            print(f"Created {path}. Sales are disabled. Keep this file and its key private.")
            return 0
        if args.command == "demo":
            result = asyncio.run(offline_demo())
            output = json.dumps(result, indent=2) + "\n"
            if args.output:
                if not args.output.parent.is_dir():
                    raise ShopError("Output parent directory does not exist")
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(output)
            print(output, end="")
            return 0
        settings = load_settings(args.config)
        if args.command == "doctor":
            issues = settings.problems(require_bot=False)
            print(f"Environment: {settings.environment}; sales enabled: {settings.enable_sales}")
            print(f"Database: {settings.database_path}")
            if not issues:
                store = Store(
                    settings.database_path, settings.stock_encryption_key, settings.environment
                )
                store.initialize()
                store.supplier.configure(settings.canboso)
                print("Database environment and encryption key: OK")
            for issue in issues:
                print(f"NEEDS SETUP: {issue}")
            print("Supplier integration: installed (Canboso)")
            print(
                f"Supplier flags: enabled={settings.canboso.enabled}; "
                f"allow_purchases={settings.canboso.allow_purchases}; "
                f"resale_authorized={settings.canboso.resale_authorized}; "
                f"acknowledge_price_race={settings.canboso.acknowledge_price_race}"
            )
            print("Local checks only; no live verification of Telegram or supplier connectivity.")
            return 1 if issues else 0
        settings.validate(require_bot=args.command == "run")
        store = Store(settings.database_path, settings.stock_encryption_key, settings.environment)
        store.initialize()
        store.supplier.configure(settings.canboso)
        if args.command == "seed-demo":
            if settings.environment != "test":
                raise ShopError("Sample catalog is only allowed in the test environment")
            products = json.loads(
                (PROJECT_ROOT / "sample" / "catalog.json").read_text(encoding="utf-8")
            )
            for product in products:
                store.upsert_product(product)
                store.import_stock(
                    product["sku"],
                    [f"DEMO-{product['sku']}-{n:03d}-NOT-REDEEMABLE" for n in range(1, 6)],
                )
            print(
                "Loaded three demo products with sample stock. Repeating this command does not duplicate stock."
            )
        elif args.command == "import-catalog":
            products = json.loads(args.file.read_text(encoding="utf-8"))
            if not isinstance(products, list) or not 1 <= len(products) <= 500:
                raise ShopError("Catalog must be a JSON array with 1-500 products")
            for product in products:
                store.upsert_product(product)
            print(f"Imported {len(products)} catalog entries. Inventory is imported separately.")
        elif args.command == "stock":
            if args.file.stat().st_size > 262144:
                raise ShopError("Stock file exceeds 256 KiB")
            payloads = [
                x.strip()
                for x in args.file.read_text(encoding="utf-8-sig").splitlines()
                if x.strip()
            ]
            added, skipped = store.import_stock(args.sku, payloads)
            print(f"Imported {added} unique inventory items; skipped {skipped} duplicates.")
        elif args.command == "supplier-sync":
            if not settings.canboso.enabled:
                raise ShopError("Supplier integration is disabled")
            if not args.output.parent.is_dir():
                raise ShopError("Output parent directory must exist")
            if args.output.exists() or args.output.is_symlink():
                raise ShopError("Output file already exists; choose a new local JSON path")
            with process_lock(settings.database_path.with_suffix(".process.lock")):
                private_json(args.output, asyncio.run(sync_supplier(settings, store)))
            print("Read-only supplier products/balance exported locally. No purchases were made.")
        elif args.command == "supplier-review":
            store.supplier.recover_interrupted()
            print(json.dumps(store.supplier.review(), indent=2))
        elif args.command == "supplier-inspect":
            private_json(args.output, store.supplier.inspect(args.order))
            print("Sensitive supplier evidence exported locally. Keep the file private; do not share it in chat.")
        elif args.command == "supplier-resolve":
            if args.operator_id <= 0 or args.operator_id not in settings.admin_ids:
                raise ShopError("Operator ID must be a configured positive admin ID")
            if args.action == "fulfill" and args.delivery_file is None:
                raise ShopError("fulfill requires --delivery-file")
            if args.action != "fulfill" and args.delivery_file is not None:
                raise ShopError("--delivery-file is only allowed with fulfill")
            evidence = read_private_text(args.evidence_file, 10_000, "Evidence")
            delivery = (
                read_private_text(args.delivery_file, 1_000_000, "Delivery")
                if args.delivery_file is not None else ""
            )
            store.supplier.resolve(
                args.order, args.action, evidence, str(args.operator_id), delivery=delivery
            )
            print("Supplier resolution recorded locally. No network requests or refunds were made.")
        elif args.command == "run":
            with process_lock(settings.database_path.with_suffix(".process.lock")):
                asyncio.run(run_bot(settings, store))
        return 0
    except (ShopError, ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"Stopped safely ({type(exc).__name__}). Check local configuration and connectivity; "
            "secrets are not printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
