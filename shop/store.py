"""Transactional shop state; no network calls inside database transactions.

SQLite is appropriate for one bot process on a persistent local disk. Inventory is
allocated once per order, not once per Telegram message. Transport delivery is
at-least-once: after an ambiguous network failure the same item may be resent.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken

from .errors import ShopError
from .pricing import PRICING_SCHEMA
from .router import ROUTER_SCHEMA
from .supplier_store import SUPPLIER_SCHEMA, SupplierState
from .supplier_store import migrate as migrate_supplier_schema


@dataclass(frozen=True)
class PaymentResult:
    event_id: str
    order_id: str | None
    status: str
    duplicate: bool = False


@dataclass(frozen=True)
class Delivery:
    order_id: str
    user_id: int
    title: str
    payload: str
    lease_token: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
    category TEXT NOT NULL, price_stars INTEGER NOT NULL CHECK(price_stars > 0),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    is_demo INTEGER NOT NULL CHECK(is_demo IN (0,1)),
    source TEXT NOT NULL CHECK(source IN ('stock','supplier'))
);
CREATE TABLE IF NOT EXISTS terms_acceptances (
    user_id INTEGER NOT NULL, version TEXT NOT NULL, accepted_at REAL NOT NULL,
    PRIMARY KEY(user_id, version)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, sku TEXT NOT NULL REFERENCES products(sku),
    title TEXT NOT NULL, price_stars INTEGER NOT NULL, terms_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('invoice','checkout','expired','cancelled','paid',
        'delivering','delivered','delivery_failed','needs_refund','refund_pending','refunded')),
    charge_id TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    expires_at REAL NOT NULL, checkout_query_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
    lease_token TEXT, message_id INTEGER, error_code TEXT
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orders_delivery ON orders(state, next_attempt_at);
CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL REFERENCES products(sku), fingerprint TEXT NOT NULL UNIQUE,
    ciphertext TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('available','reserved','sold','quarantined')),
    order_id TEXT UNIQUE REFERENCES orders(id), created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stock_available ON stock(sku, state, id);
CREATE TABLE IF NOT EXISTS payment_events (
    id TEXT PRIMARY KEY, charge_id TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
    payload TEXT NOT NULL, currency TEXT NOT NULL, amount INTEGER NOT NULL,
    order_id TEXT REFERENCES orders(id),
    status TEXT NOT NULL CHECK(status IN ('accepted','review','refund_pending','refunded')),
    reason TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS payment_review ON payment_events(status, created_at);
CREATE TABLE IF NOT EXISTS refund_receipts (
    charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, currency TEXT NOT NULL,
    amount INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS update_inbox (
    update_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ciphertext TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, error_code TEXT
);
CREATE INDEX IF NOT EXISTS inbox_pending ON update_inbox(kind,state,next_attempt_at);
"""


class Store:
    def __init__(
        self,
        path: Path,
        encryption_key: str,
        environment: str = "test",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.environment = environment
        self.clock = clock
        self.cipher = Fernet(encryption_key.encode())
        self.fingerprint_key = hashlib.sha256(encryption_key.encode()).digest()
        self.supplier = SupplierState(self)
        # One persistent connection per worker thread; opening a connection and
        # re-applying pragmas on every query dominated per-message latency.
        self._local = threading.local()
        self._offset_lock = threading.Lock()
        self._offset_cache: int | None = None
        self._offset_cached = False
        if environment not in {"test", "production"}:
            raise ShopError("Unknown database environment")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=1.5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=1500")
            # WAL (set once in initialize) makes NORMAL crash-safe; FULL only adds
            # protection against OS/power failure and costs an fsync per commit.
            db.execute("PRAGMA synchronous=NORMAL")
            self._local.db = db
        yield db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA + SUPPLIER_SCHEMA + PRICING_SCHEMA + ROUTER_SCHEMA)
            migrate_supplier_schema(db)
        with self.transaction() as db:
            meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM metadata")}
            if meta and meta.get("schema") != "1":
                raise ShopError("Unsupported schema; migration is required")
            if meta.get("environment", self.environment) != self.environment:
                raise ShopError("Test and production require separate databases")
            if "key_check" in meta:
                try:
                    decoded = self.cipher.decrypt(meta["key_check"].encode())
                    if decoded != b"digital-shelf-key-check-v1":
                        raise InvalidToken
                except InvalidToken as exc:
                    raise ShopError(
                        "Wrong stock encryption key; preserve the original key"
                    ) from exc
            else:
                values = {
                    "schema": "1",
                    "environment": self.environment,
                    "key_check": self.cipher.encrypt(b"digital-shelf-key-check-v1").decode(),
                }
                db.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", values.items())

    def upsert_product(self, product: dict) -> None:
        sku = product.get("sku", "")
        title = product.get("title", "")
        description = product.get("description", "")
        category = product.get("category", "Digital products")
        price = product.get("price_stars")
        source = product.get("source", "stock")
        active = product.get("active", True)
        demo = product.get("is_demo", False)
        if not isinstance(sku, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", sku):
            raise ShopError(
                "SKU must contain 2-40 lowercase letters, digits, hyphens or underscores"
            )
        for value, name, limit in (
            (title, "title", 64),
            (description, "description", 800),
            (category, "category", 40),
        ):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                raise ShopError(f"Product {name} must contain 1-{limit} characters")
            if any(ord(c) < 32 and c not in "\n\t" for c in value):
                raise ShopError("Product text contains control characters")
        if type(price) is not int or not 1 <= price <= 100000:
            raise ShopError("price_stars must be an integer from 1 to 100000")
        if (
            source not in {"stock", "supplier"}
            or type(active) is not bool
            or type(demo) is not bool
        ):
            raise ShopError("Invalid product source or boolean flags")
        if self.environment == "production" and demo:
            raise ShopError("Demo products cannot be imported into production")
        with self.transaction() as db:
            old = db.execute("SELECT source FROM products WHERE sku=?", (sku,)).fetchone()
            if old and old["source"] != source:
                raise ShopError("Use a new SKU when changing the fulfillment source")
            db.execute(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET "
                "title=excluded.title,description=excluded.description,category=excluded.category,"
                "price_stars=excluded.price_stars,active=excluded.active,is_demo=excluded.is_demo",
                (
                    sku,
                    title.strip(),
                    description.strip(),
                    category.strip(),
                    price,
                    active,
                    demo,
                    source,
                ),
            )
            if source == "supplier" and product.get("supplier") is not None:
                self.supplier.set_mapping(db, sku, product["supplier"])

    def import_stock(self, sku: str, payloads: list[str]) -> tuple[int, int]:
        if not 1 <= len(payloads) <= 500:
            raise ShopError("Import 1-500 inventory lines at a time")
        prepared = []
        for raw in payloads:
            payload = raw.strip()
            if not 1 <= len(payload) <= 1500 or any(ord(c) < 32 for c in payload):
                raise ShopError("Each inventory line must contain 1-1500 printable characters")
            digest = hmac.new(self.fingerprint_key, payload.encode(), hashlib.sha256).hexdigest()
            prepared.append((digest, self.cipher.encrypt(payload.encode()).decode()))
        with self.transaction() as db:
            product = db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
            if not product or product["source"] != "stock":
                raise ShopError("Choose an existing stock-backed product")
            inserted = 0
            for digest, ciphertext in prepared:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO stock(sku,fingerprint,ciphertext,state,created_at) "
                    "VALUES (?,?,?,'available',?)",
                    (sku, digest, ciphertext, self.clock()),
                )
                inserted += cursor.rowcount
        return inserted, len(prepared) - inserted

    def list_products(self, *, include_inactive: bool = False) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT p.*,sum(CASE WHEN s.state='available' THEN 1 ELSE 0 END) AS available "
                "FROM products p LEFT JOIN stock s ON p.sku=s.sku WHERE (?=1 OR p.active=1) "
                "AND (?='test' OR p.is_demo=0) GROUP BY p.sku ORDER BY p.category,p.title,p.sku",
                (include_inactive, self.environment),
            )
            return [dict(row) for row in rows]

    def get_product(self, sku: str) -> dict:
        result = next(
            (p for p in self.list_products(include_inactive=True) if p["sku"] == sku), None
        )
        if result is None:
            raise ShopError("Product not found")
        return result

    def accept_terms(self, user_id: int, version: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO terms_acceptances VALUES (?,?,?)",
                (user_id, version, self.clock()),
            )

    def has_accepted_terms(self, user_id: int, version: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM terms_acceptances WHERE user_id=? AND version=?",
                    (user_id, version),
                ).fetchone()
                is not None
            )

    def _expire(self, db: sqlite3.Connection) -> None:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM orders WHERE state IN ('invoice','checkout') AND expires_at<=?",
                (self.clock(),),
            )
        ]
        for order_id in ids:
            db.execute(
                "UPDATE stock SET state='available',order_id=NULL "
                "WHERE order_id=? AND state='reserved'",
                (order_id,),
            )
            db.execute(
                "UPDATE orders SET state='expired',updated_at=? WHERE id=?",
                (self.clock(), order_id),
            )

    def expire_orders(self) -> None:
        with self.transaction() as db:
            self._expire(db)

    def create_order(
        self,
        user_id: int,
        sku: str,
        terms_version: str,
        *,
        customer_email: str | None = None,
    ) -> dict:
        if type(user_id) is not int or user_id <= 0:
            raise ShopError("Invalid customer")
        with self.transaction() as db:
            self._expire(db)
            if not db.execute(
                "SELECT 1 FROM terms_acceptances WHERE user_id=? AND version=?",
                (user_id, terms_version),
            ).fetchone():
                raise ShopError("Please read and accept the terms and privacy notice first")
            product = db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
            self._validate_sellable(product)
            existing = db.execute(
                "SELECT * FROM orders WHERE user_id=? AND sku=? AND state='invoice' "
                "AND terms_version=? ORDER BY created_at DESC LIMIT 1",
                (user_id, sku, terms_version),
            ).fetchone()
            if existing:
                if product["source"] == "stock":
                    return dict(existing)
                intent = db.execute(
                    "SELECT * FROM supplier_intents WHERE order_id=?", (existing["id"],)
                ).fetchone()
                if intent:
                    request = self.supplier.decrypt(intent["request_ciphertext"])
                    mapping = self.supplier.mapping(sku, db)
                    if (
                        request.get("customer_email") == customer_email
                        and intent["product_id"] == mapping["product_id"]
                        and intent["product_type"] == mapping["product_type"]
                        and request.get("slot_months") == mapping.get("slot_months")
                    ):
                        self.supplier.checkout(db, existing["id"])
                        return dict(existing)
            if product["source"] == "stock" and not db.execute(
                "SELECT 1 FROM stock WHERE sku=? AND state='available' LIMIT 1", (sku,)
            ).fetchone():
                raise ShopError("This product is temporarily out of stock")
            pending = db.execute(
                "SELECT count(*) FROM orders WHERE user_id=? AND state IN ('invoice','checkout')",
                (user_id,),
            ).fetchone()[0]
            if pending >= 3:
                raise ShopError(
                    "You already have three pending checkouts; finish or wait for expiry"
                )
            order_id = uuid.uuid4().hex
            now = self.clock()
            db.execute(
                "INSERT INTO orders(id,user_id,sku,title,price_stars,terms_version,state,"
                "created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,'invoice',?,?,?)",
                (
                    order_id,
                    user_id,
                    sku,
                    product["title"],
                    product["price_stars"],
                    terms_version,
                    now,
                    now,
                    now + 600,
                ),
            )
            if product["source"] == "supplier":
                self.supplier.prepare(db, order_id, sku, customer_email)
            return dict(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def _validate_sellable(self, product: sqlite3.Row | None) -> None:
        if product is None or not product["active"]:
            raise ShopError("This product is not available")
        if product["source"] == "supplier":
            try:
                provider = self.supplier.mapping(product["sku"]).get("provider", "canboso")
            except ShopError:
                provider = "canboso"
            self.supplier.assert_enabled(provider)
        if product["is_demo"] and self.environment != "test":
            raise ShopError("Demo products cannot be sold in production")

    def approve_checkout(
        self,
        order_id: str,
        user_id: int,
        currency: str,
        amount: int,
        query_id: str,
        terms_version: str,
    ) -> None:
        with self.transaction() as db:
            self._expire(db)
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["user_id"] != user_id:
                raise ShopError("This invoice does not belong to you; open your own checkout")
            if currency != "XTR" or type(amount) is not int or amount != order["price_stars"]:
                raise ShopError("Payment amount or currency does not match the order")
            if order["terms_version"] != terms_version:
                raise ShopError("The shop terms changed; please start a new checkout")
            if order["state"] == "checkout" and order["checkout_query_id"] == query_id:
                return
            if order["state"] != "invoice":
                raise ShopError("This invoice expired or is already being processed")
            product = db.execute("SELECT * FROM products WHERE sku=?", (order["sku"],)).fetchone()
            self._validate_sellable(product)
            if product["source"] == "supplier":
                self.supplier.checkout(db, order_id)
            elif product["source"] == "stock":
                stock = db.execute(
                    "SELECT id FROM stock WHERE sku=? AND state='available' ORDER BY id LIMIT 1",
                    (order["sku"],),
                ).fetchone()
                if stock is None:
                    raise ShopError("Out of stock. No payment has been approved")
                db.execute(
                    "UPDATE stock SET state='reserved',order_id=? WHERE id=?",
                    (order_id, stock["id"]),
                )
            db.execute(
                "UPDATE orders SET state='checkout',checkout_query_id=?,expires_at=?,"
                "updated_at=? WHERE id=?",
                (query_id, self.clock() + 900, self.clock(), order_id),
            )

    def cancel_invoice(self, order_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE orders SET state='cancelled',updated_at=? WHERE id=? AND state='invoice'",
                (self.clock(), order_id),
            )

    def record_payment(
        self,
        order_id: str,
        user_id: int,
        currency: str,
        amount: int,
        charge_id: str,
    ) -> PaymentResult:
        if not charge_id or len(charge_id) > 512:
            raise ShopError("Invalid payment charge ID")
        with self.transaction() as db:
            previous = db.execute(
                "SELECT * FROM payment_events WHERE charge_id=?", (charge_id,)
            ).fetchone()
            if previous:
                if (
                    previous["user_id"],
                    previous["payload"],
                    previous["currency"],
                    previous["amount"],
                ) != (user_id, order_id, currency, amount):
                    raise ShopError("Conflicting duplicate payment event")
                return PaymentResult(previous["id"], previous["order_id"], previous["status"], True)
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            reason = None
            if not order:
                reason = "unknown_order"
            elif order["user_id"] != user_id:
                reason = "wrong_customer"
            elif currency != "XTR" or type(amount) is not int or amount != order["price_stars"]:
                reason = "amount_or_currency_mismatch"
            elif order["charge_id"]:
                reason = "extra_charge_for_order"
            event_id = uuid.uuid4().hex
            status = "review" if reason else "accepted"
            bound_order = None if reason else order_id
            db.execute(
                "INSERT INTO payment_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    charge_id,
                    user_id,
                    order_id,
                    currency,
                    amount,
                    bound_order,
                    status,
                    reason,
                    self.clock(),
                ),
            )
            if reason:
                return PaymentResult(event_id, None, status)
            refund = db.execute(
                "SELECT * FROM refund_receipts WHERE charge_id=?", (charge_id,)
            ).fetchone()
            if refund and (refund["user_id"], refund["currency"], refund["amount"]) == (
                user_id,
                currency,
                amount,
            ):
                db.execute(
                    "UPDATE orders SET state='refunded',charge_id=?,updated_at=? WHERE id=?",
                    (charge_id, self.clock(), order_id),
                )
                self._finish_refund(db, event_id)
                return PaymentResult(event_id, order_id, "refunded")
            if self.supplier.info(order_id, db):
                db.execute(
                    "UPDATE orders SET charge_id=?,updated_at=? WHERE id=?",
                    (charge_id, self.clock(), order_id),
                )
                self.supplier.queue_paid(db, order_id)
                return PaymentResult(event_id, order_id, "accepted")
            item = db.execute("SELECT id FROM stock WHERE order_id=?", (order_id,)).fetchone()
            if item is None:
                item = db.execute(
                    "SELECT id FROM stock WHERE sku=? AND state='available' ORDER BY id LIMIT 1",
                    (order["sku"],),
                ).fetchone()
            state = "paid" if item else "needs_refund"
            if item:
                db.execute(
                    "UPDATE stock SET state='sold',order_id=? WHERE id=?", (order_id, item["id"])
                )
            db.execute(
                "UPDATE orders SET state=?,charge_id=?,updated_at=?,error_code=? WHERE id=?",
                (state, charge_id, self.clock(), None if item else "stock_unavailable", order_id),
            )
            return PaymentResult(event_id, order_id, status)

    def get_order(self, order_id: str, *, user_id: int | None = None) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row or (user_id is not None and row["user_id"] != user_id):
                raise ShopError("Order not found")
            return dict(row)

    def user_orders(self, user_id: int, limit: int = 10) -> list[dict]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC,id LIMIT ?",
                    (user_id, limit),
                )
            ]

    def due_deliveries(self) -> list[str]:
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT id FROM orders WHERE ((state='paid' AND next_attempt_at<=?) "
                    "OR (state='delivering' AND lease_until<=?)) AND EXISTS "
                    "(SELECT 1 FROM stock WHERE stock.order_id=orders.id AND stock.state='sold') "
                    "ORDER BY created_at LIMIT 20",
                    (self.clock(), self.clock()),
                )
            ]

    def claim_delivery(self, order_id: str) -> Delivery | None:
        with self.transaction() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return None
            due = order["state"] == "paid" and order["next_attempt_at"] <= self.clock()
            stale = order["state"] == "delivering" and order["lease_until"] <= self.clock()
            if not (due or stale):
                return None
            item = db.execute(
                "SELECT ciphertext FROM stock WHERE order_id=? AND state='sold'", (order_id,)
            ).fetchone()
            if not item:
                if self.supplier.info(order_id, db):
                    return None
                raise ShopError("Paid order has no allocated inventory; operator review required")
            payload = self.cipher.decrypt(item["ciphertext"].encode()).decode()
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE orders SET state='delivering',lease_token=?,lease_until=?,"
                "attempts=attempts+1,updated_at=? WHERE id=?",
                (token, self.clock() + 180, self.clock(), order_id),
            )
            return Delivery(order_id, order["user_id"], order["title"], payload, token)

    def finish_delivery(self, delivery: Delivery, message_id: int) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE orders SET state='delivered',message_id=?,lease_token=NULL,lease_until=0,"
                "error_code=NULL,updated_at=? WHERE id=? AND state='delivering' AND lease_token=?",
                (message_id, self.clock(), delivery.order_id, delivery.lease_token),
            )
            return cursor.rowcount == 1

    def fail_delivery(self, delivery: Delivery, error_code: str) -> None:
        with self.transaction() as db:
            order = db.execute(
                "SELECT attempts FROM orders WHERE id=? AND state='delivering' AND lease_token=?",
                (delivery.order_id, delivery.lease_token),
            ).fetchone()
            if not order:
                return
            attempts = order["attempts"]
            state = "delivery_failed" if attempts >= 8 else "paid"
            retry_at = self.clock() + min(3600, 5 * 2 ** min(attempts, 10))
            db.execute(
                "UPDATE orders SET state=?,next_attempt_at=?,error_code=?,lease_token=NULL,"
                "lease_until=0,updated_at=? WHERE id=?",
                (state, retry_at, error_code[:80], self.clock(), delivery.order_id),
            )

    def retrieve_delivery(self, order_id: str, user_id: int) -> str:
        with self.connection() as db:
            row = db.execute(
                "SELECT s.ciphertext FROM orders o JOIN stock s ON s.order_id=o.id "
                "WHERE o.id=? AND o.user_id=? AND o.state='delivered' AND s.state='sold'",
                (order_id, user_id),
            ).fetchone()
            if not row:
                raise ShopError(
                    "Delivery is unavailable; check order status or contact /paysupport"
                )
            return self.cipher.decrypt(row["ciphertext"].encode()).decode()

    def retry_order(self, order_id: str) -> None:
        with self.transaction() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["state"] not in {"paid", "delivery_failed", "needs_refund"}:
                raise ShopError("This order is not eligible for a delivery retry")
            intent = self.supplier.info(order_id, db)
            if intent and intent["state"] != "completed":
                raise ShopError("Resolve the supplier purchase before retrying delivery")
            if order["state"] == "needs_refund":
                item = db.execute(
                    "SELECT id FROM stock WHERE sku=? AND state='available' ORDER BY id LIMIT 1",
                    (order["sku"],),
                ).fetchone()
                if not item:
                    raise ShopError("Still out of stock; import stock or refund this payment")
                db.execute(
                    "UPDATE stock SET state='sold',order_id=? WHERE id=?", (order_id, item["id"])
                )
            db.execute(
                "UPDATE orders SET state='paid',attempts=0,next_attempt_at=0,"
                "error_code=NULL,updated_at=? WHERE id=?",
                (self.clock(), order_id),
            )

    def payment_for_order(self, order_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM payment_events WHERE order_id=?", (order_id,)
            ).fetchone()
            if not row:
                raise ShopError("No confirmed payment for this order")
            return dict(row)

    def get_payment(self, event_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM payment_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise ShopError("Payment not found")
            return dict(row)

    def begin_refund(self, event_id: str) -> dict:
        with self.transaction() as db:
            event = db.execute("SELECT * FROM payment_events WHERE id=?", (event_id,)).fetchone()
            if not event:
                raise ShopError("Payment not found")
            if event["currency"] != "XTR":
                raise ShopError("Only Telegram Stars payments can be refunded by this bot")
            if event["status"] == "refunded":
                raise ShopError("Payment is already marked refunded")
            if event["order_id"]:
                order = db.execute(
                    "SELECT * FROM orders WHERE id=?", (event["order_id"],)
                ).fetchone()
                if order["state"] == "delivering":
                    raise ShopError("A delivery attempt is in flight; wait before refunding")
                self.supplier.guard_refund(db, event["order_id"])
                db.execute(
                    "UPDATE orders SET state='refund_pending',updated_at=? WHERE id=?",
                    (self.clock(), event["order_id"]),
                )
            db.execute("UPDATE payment_events SET status='refund_pending' WHERE id=?", (event_id,))
            return dict(event)

    def _finish_refund(self, db: sqlite3.Connection, event_id: str) -> None:
        event = db.execute("SELECT * FROM payment_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            raise ShopError("Payment not found")
        db.execute("UPDATE payment_events SET status='refunded' WHERE id=?", (event_id,))
        if event["order_id"]:
            self.supplier.external_refund(db, event["order_id"])
            db.execute(
                "UPDATE orders SET state='refunded',lease_token=NULL,lease_until=0,"
                "updated_at=? WHERE id=?",
                (self.clock(), event["order_id"]),
            )
            db.execute(
                "UPDATE stock SET state='quarantined' WHERE order_id=?", (event["order_id"],)
            )

    def finish_refund(self, event_id: str) -> None:
        with self.transaction() as db:
            self._finish_refund(db, event_id)

    def record_refund(self, charge_id: str, user_id: int, currency: str, amount: int) -> None:
        with self.transaction() as db:
            event = db.execute(
                "SELECT * FROM payment_events WHERE charge_id=?", (charge_id,)
            ).fetchone()
            if event and (event["user_id"], event["currency"], event["amount"]) != (
                user_id,
                currency,
                amount,
            ):
                raise ShopError("Refund does not match the recorded payment")
            db.execute(
                "INSERT OR IGNORE INTO refund_receipts VALUES (?,?,?,?,?)",
                (charge_id, user_id, currency, amount, self.clock()),
            )
            if event:
                self._finish_refund(db, event["id"])

    def review_payments(self) -> list[dict]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT p.* FROM payment_events p LEFT JOIN orders o ON p.order_id=o.id "
                    "WHERE p.status IN ('review','refund_pending') OR o.state IN "
                    "('needs_refund','delivery_failed') ORDER BY p.created_at LIMIT 20",
                )
            ]

    def bind_bot(self, bot_id: int) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='bot_id'").fetchone()
            if row and row[0] != str(bot_id):
                raise ShopError("This database belongs to another bot; use a separate database")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('bot_id',?)", (str(bot_id),))

    def polling_offset(self) -> int | None:
        # The single-process lock means only save_updates can advance the offset,
        # so an in-memory cache stays authoritative after the first read.
        with self._offset_lock:
            if self._offset_cached:
                return self._offset_cache
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='polling_offset'").fetchone()
            value = int(row[0]) if row else None
        with self._offset_lock:
            self._offset_cache = value
            self._offset_cached = True
        return value

    def save_updates(self, updates: list[tuple[int, str, str]]) -> None:
        if not updates:
            return
        with self.transaction() as db:
            for update_id, kind, body in updates:
                db.execute(
                    "INSERT OR IGNORE INTO update_inbox(update_id,kind,ciphertext,created_at) "
                    "VALUES (?,?,?,?)",
                    (update_id, kind, self.cipher.encrypt(body.encode()).decode(), self.clock()),
                )
            # Telegram update IDs can jump after a week without updates. Use the
            # last update in this returned batch rather than deriving an ID.
            db.execute(
                "INSERT INTO metadata VALUES ('polling_offset',?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (str(updates[-1][0] + 1),),
            )
        with self._offset_lock:
            self._offset_cache = updates[-1][0] + 1
            self._offset_cached = True

    def claim_update(self, kind: str) -> tuple[int, str] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM update_inbox WHERE kind=? AND "
                "((state='pending' AND next_attempt_at<=?) OR (state='working' AND lease_until<=?)) "
                "ORDER BY created_at,update_id LIMIT 1",
                (kind, self.clock(), self.clock()),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE update_inbox SET state='working',attempts=attempts+1,lease_until=? "
                "WHERE update_id=?",
                (self.clock() + 180, row["update_id"]),
            )
            return row["update_id"], self.cipher.decrypt(row["ciphertext"].encode()).decode()

    def finish_update(self, update_id: int) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE update_inbox SET state='done',ciphertext='',lease_until=0,error_code=NULL "
                "WHERE update_id=?",
                (update_id,),
            )

    def retry_update(self, update_id: int, error_code: str) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts,kind FROM update_inbox WHERE update_id=?", (update_id,)
            ).fetchone()
            if not row:
                return
            # Financial events never dead-letter automatically. UI events do, so
            # a stale callback cannot monopolize the queue indefinitely.
            state = "failed" if row["kind"] != "payment" and row["attempts"] >= 3 else "pending"
            delay = min(60, 2 ** min(row["attempts"], 6))
            db.execute(
                "UPDATE update_inbox SET state=?,next_attempt_at=?,lease_until=0,error_code=? "
                "WHERE update_id=?",
                (state, self.clock() + delay, error_code[:80], update_id),
            )

    def stats(self) -> dict:
        with self.connection() as db:
            stock = {
                r["state"]: r["n"]
                for r in db.execute("SELECT state,count(*) AS n FROM stock GROUP BY state")
            }
            orders = {
                r["state"]: r["n"]
                for r in db.execute("SELECT state,count(*) AS n FROM orders GROUP BY state")
            }
            sales = db.execute(
                "SELECT coalesce(sum(amount),0) FROM payment_events WHERE status='accepted'"
            ).fetchone()[0]
            return {"stock": stock, "orders": orders, "accepted_stars": sales}
