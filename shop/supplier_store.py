"""Supplier state is additive to v1 storage; existing inventory remains intact.

State is provider-agnostic: every snapshot, cooldown, budget and purchase
intent is scoped to one registered provider (see shop/providers.py), so
several suppliers can serve the shop without sharing wallets or quotas.

Where a provider documents an order-status or history endpoint, an uncertain
purchase is recovered read-only through it; where none exists, an uncertain
purchase never becomes a new purchase automatically. Approved manual resolutions
and cost-risk acknowledgments are durable and visible in the operator workflow.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING

from . import providers
from .canboso import CanbosoError, PurchaseResult, money
from .config import CanbosoSettings, SupplierSettings
from .errors import ShopError

if TYPE_CHECKING:
    from .store import Store

SUPPLIER_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_mappings (
    sku TEXT PRIMARY KEY REFERENCES products(sku), specification TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_cache (
    name TEXT PRIMARY KEY, key_hash TEXT NOT NULL, ciphertext TEXT NOT NULL, fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_intents (
    order_id TEXT PRIMARY KEY REFERENCES orders(id), idempotency_key TEXT NOT NULL UNIQUE,
    request_ciphertext TEXT NOT NULL, key_hash TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'canboso',
    product_id TEXT NOT NULL,
    product_type TEXT NOT NULL, max_cost TEXT NOT NULL, currency TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft', response_ciphertext TEXT NOT NULL DEFAULT '',
    delivery_ciphertext TEXT NOT NULL DEFAULT '', supplier_reference TEXT NOT NULL DEFAULT '',
    actual_cost TEXT, hold_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    first_sent_at REAL, next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
    budget_held INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    resolution_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS supplier_queue ON supplier_intents(state,next_attempt_at);
CREATE TABLE IF NOT EXISTS supplier_audit (
    id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), operator_id TEXT NOT NULL,
    action TEXT NOT NULL, evidence_ciphertext TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def migrate(db: sqlite3.Connection) -> None:
    """Bring pre-multi-provider databases forward. Existing intents were all
    Canboso purchases, so the added column's default is the correct value."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(supplier_intents)")}
    if columns and "provider" not in columns:
        db.execute("ALTER TABLE supplier_intents ADD COLUMN provider TEXT NOT NULL DEFAULT 'canboso'")
    # Snapshot rows are now named "<provider>:<kind>"; pre-rename rows fail the
    # freshness check within two minutes anyway, so just drop them.
    db.execute("DELETE FROM supplier_cache WHERE name NOT LIKE '%:%'")


class SupplierState:
    def __init__(self, store: Store):
        self.store = store
        self._settings: dict[str, SupplierSettings] = {}

    def configure(self, settings: SupplierSettings) -> None:
        self._settings[settings.provider] = settings

    def configure_many(self, all_settings) -> None:
        for settings in all_settings:
            self.configure(settings)

    @property
    def settings(self) -> SupplierSettings:
        """The Canboso configuration, for the original single-supplier callers."""
        return self.settings_for("canboso")

    def settings_for(self, provider: str = "canboso") -> SupplierSettings:
        configured = self._settings.get(provider)
        if configured is not None:
            return configured
        if provider == "canboso":
            return CanbosoSettings()
        return SupplierSettings(provider=provider)

    def _provider_names(self) -> set[str]:
        registry = providers.PROVIDERS
        names = set(self._settings)
        names.update(registry.keys() if isinstance(registry, dict) else registry)
        return names

    def purchases_allowed(self) -> bool:
        return any(s.allow_purchases for s in self._settings.values())

    def assert_enabled(self, provider: str = "canboso") -> None:
        s = self.settings_for(provider)
        if not s.enabled or not s.allow_purchases or s.problems(self.store.environment):
            raise ShopError("Supplier checkout is not connected yet or live purchasing is locked")

    def encrypt(self, body) -> str:
        return self.store.cipher.encrypt(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode()

    def decrypt(self, ciphertext: str):
        return json.loads(self.store.cipher.decrypt(ciphertext.encode()))

    def set_mapping(self, db: sqlite3.Connection, sku: str, specification: dict) -> None:
        if not isinstance(specification, dict) or not providers.registered(specification.get("provider", "")):
            raise ShopError("Supplier specification requires a registered provider")
        provider = specification["provider"]
        product_id = specification.get("product_id")
        product_type = specification.get("product_type")
        if not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", product_id):
            raise ShopError("Set a real supplier product_id from your read-only catalog")
        if product_type not in {"account", "slot"}:
            raise ShopError("Only instant account delivery and email-based slots are supported")
        currency = specification.get("currency")
        if currency not in {"VND", "USD"}:
            raise ShopError("Use the supplier's documented wallet currency, VND or USD")
        try:
            ceiling = money(specification.get("max_cost"), positive=True)
        except CanbosoError as exc:
            raise ShopError("Set a positive max_cost in supplier currency") from exc
        providers.validate_spec(specification)
        clean = {"provider": provider, "product_id": product_id,
                 "product_type": product_type, "currency": currency, "max_cost": str(ceiling)}
        months = specification.get("slot_months")
        if months is not None:
            clean["slot_months"] = months
        db.execute("INSERT INTO supplier_mappings VALUES (?,?) ON CONFLICT(sku) DO UPDATE SET "
                   "specification=excluded.specification", (sku, json.dumps(clean, sort_keys=True)))

    def mapping(self, sku: str, db: sqlite3.Connection | None = None) -> dict:
        if db is None:
            with self.store.connection() as conn:
                return self.mapping(sku, conn)
        row = db.execute("SELECT specification FROM supplier_mappings WHERE sku=?", (sku,)).fetchone()
        if not row:
            raise ShopError("This supplier product has not been mapped yet")
        return json.loads(row[0])

    def mapping_many(self, skus: list[str]) -> dict[str, dict]:
        """Catalog display helper: unmapped SKUs are absent, not an error."""
        if not skus:
            return {}
        placeholders = ",".join("?" * len(skus))
        with self.store.connection() as db:
            rows = db.execute(
                f"SELECT sku,specification FROM supplier_mappings WHERE sku IN ({placeholders})",
                skus,
            ).fetchall()
        return {row["sku"]: json.loads(row["specification"]) for row in rows}

    @staticmethod
    def _cache_name(provider: str, kind: str) -> str:
        return f"{provider}:{kind}"

    def cache_snapshot(self, provider: str, products: dict, balance: dict) -> None:
        fingerprint = self.settings_for(provider).key_fingerprint
        with self.store.transaction() as db:
            for kind, body in (("products", products), ("balance", balance)):
                db.execute("INSERT INTO supplier_cache VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                           "key_hash=excluded.key_hash,ciphertext=excluded.ciphertext,fetched_at=excluded.fetched_at",
                           (self._cache_name(provider, kind), fingerprint,
                            self.encrypt(body), self.store.clock()))

    def cached_products(self, provider: str) -> tuple[dict, float, str]:
        """Decrypted cached catalog for CLI dry runs; no network. Returns the
        snapshot, its age in seconds and the owning key fingerprint."""
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM supplier_cache WHERE name=?",
                (self._cache_name(provider, "products"),),
            ).fetchone()
        if not row:
            raise ShopError(
                f"No supplier snapshot cached for {providers.display(provider)}; "
                "run supplier-sync first"
            )
        return self.decrypt(row["ciphertext"]), self.store.clock() - row["fetched_at"], row["key_hash"]

    def cooldown_until(self, provider: str = "canboso",
                       db: sqlite3.Connection | None = None) -> float:
        if db is None:
            with self.store.connection() as conn:
                return self.cooldown_until(provider, conn)
        row = db.execute("SELECT value FROM metadata WHERE key=?", (f"{provider}_not_before",)).fetchone()
        return float(row[0]) if row else 0

    def defer_network(self, seconds: int, provider: str = "canboso") -> None:
        with self.store.transaction() as db:
            until = max(self.cooldown_until(provider, db), self.store.clock() + max(1, seconds))
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", (f"{provider}_not_before", str(until)))

    def _snapshot(self, db: sqlite3.Connection, provider: str, kind: str) -> tuple[dict, float]:
        row = db.execute("SELECT * FROM supplier_cache WHERE name=?",
                         (self._cache_name(provider, kind),)).fetchone()
        if not row or row["key_hash"] != self.settings_for(provider).key_fingerprint:
            raise ShopError("Supplier catalog/balance has not been synchronized for this buyer key")
        if not 0 <= self.store.clock() - row["fetched_at"] <= 120:
            raise ShopError("Supplier information is stale. Please try again after synchronization")
        return self.decrypt(row["ciphertext"]), row["fetched_at"]

    def _check_quote(self, db: sqlite3.Connection, spec: dict, *, exclude_order: str = "") -> None:
        provider = spec["provider"]
        settings = self.settings_for(provider)
        self.assert_enabled(provider)
        if self.cooldown_until(provider, db) > self.store.clock():
            raise ShopError("Supplier rate limit or connectivity cooldown is active. Please try later")
        products, _ = self._snapshot(db, provider, "products")
        balance, balance_time = self._snapshot(db, provider, "balance")
        found = next((p for p in products.get("products", []) if p.get("productId") == spec["product_id"]), None)
        if not found or found.get("productType") != spec["product_type"]:
            raise ShopError("This supplier product is missing or its type changed")
        req = found.get("purchaseRequirements", {}) or {}
        if not isinstance(req, dict) or req.get("quantityFixed", 1) != 1:
            raise ShopError("Supplier purchase requirements are not supported")
        if any(v is True and k not in {"customerEmail", "slotMonths"} for k, v in req.items()):
            raise ShopError("Supplier requires additional input; manual integration review is needed")
        if spec["product_type"] == "account" and (req.get("customerEmail") or req.get("slotMonths")):
            raise ShopError("This account product needs additional inputs and cannot be sold automatically")
        if req.get("slotMonths") and "slot_months" not in spec:
            raise ShopError("Supplier now requires duration input; update product mapping")
        if "slot_months" in spec and spec["slot_months"] not in req.get("allowedMonths", []):
            raise ShopError("The selected duration is not offered by the supplier")
        price = found.get("price", {})
        currency = spec["currency"]
        if price.get("currency") != currency or balance.get("walletCurrency") != currency or settings.budget_currency != currency:
            raise ShopError("Supplier wallet, budget and product currencies must match")
        try:
            current_price = money(price.get("amount"))
            cap = money(spec["max_cost"], positive=True)
            wallet = money(balance.get("balance"))
            budget = money(settings.spend_budget, positive=True)
        except CanbosoError as exc:
            raise ShopError("Supplier pricing/balance is invalid; checkout is paused") from exc
        if current_price > cap:
            raise ShopError("Supplier price exceeds this product's approved preflight limit")
        intents = db.execute(
            "SELECT i.*,o.state AS order_state FROM supplier_intents i JOIN orders o ON o.id=i.order_id "
            "WHERE i.order_id<>? AND i.provider=?", (exclude_order, provider),
        ).fetchall()
        budget_used, wallet_held, units_held = Decimal(0), Decimal(0), 0
        for row in intents:
            open_checkout = row["state"] == "draft" and row["order_state"] == "checkout"
            if not (row["budget_held"] or open_checkout):
                continue
            if row["currency"] != currency:
                raise ShopError("Existing supplier commitments use another currency; operator review needed")
            cost = money(row["actual_cost"] if row["actual_cost"] is not None else row["max_cost"])
            budget_used += cost
            outstanding = row["state"] in {"draft", "queued", "processing", "retry_wait", "retry_approved", "uncertain"}
            outstanding = outstanding or (row["actual_cost"] is None and row["budget_held"] == 1)
            recently_charged = row["actual_cost"] is not None and row["updated_at"] >= balance_time
            if outstanding or recently_charged:
                wallet_held += cost
            if row["product_id"] == spec["product_id"] and outstanding:
                units_held += 1
        available = (found.get("availability") or {}).get("available")
        if available is None and spec["product_type"] == "account":
            raise ShopError("Supplier did not confirm account availability")
        if available is not None:
            try:
                if money(available) - units_held < 1:
                    raise ShopError("Supplier stock is currently unavailable")
            except CanbosoError as exc:
                raise ShopError("Supplier availability is invalid") from exc
        if budget_used + cap > budget:
            raise ShopError("The merchant's cumulative supplier spending budget is exhausted")
        if wallet - wallet_held < cap:
            raise ShopError("The merchant supplier wallet needs funding before this purchase")

    def prepare(self, db: sqlite3.Connection, order_id: str, sku: str, email: str | None) -> None:
        spec = self.mapping(sku, db)
        provider = spec["provider"]
        settings = self.settings_for(provider)
        self._check_quote(db, spec)
        body = providers.build_request(spec, settings, email, order_id=order_id)
        now = self.store.clock()
        db.execute(
            "INSERT INTO supplier_intents(order_id,idempotency_key,request_ciphertext,key_hash,"
            "provider,product_id,"
            "product_type,max_cost,currency,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, "ds-" + order_id, self.encrypt(body), settings.key_fingerprint,
             provider, spec["product_id"],
             spec["product_type"], spec["max_cost"], spec["currency"], now, now),
        )

    def checkout(self, db: sqlite3.Connection, order_id: str) -> None:
        row = db.execute("SELECT * FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            raise ShopError("Supplier order not found")
        provider = row["provider"]
        if row["key_hash"] != self.settings_for(provider).key_fingerprint:
            raise ShopError("Supplier configuration changed; start a new checkout")
        body = self.decrypt(row["request_ciphertext"])
        spec = {"provider": provider, "product_id": row["product_id"],
                "product_type": row["product_type"],
                "currency": row["currency"], "max_cost": row["max_cost"]}
        if "slot_months" in body:
            spec["slot_months"] = body["slot_months"]
        self._check_quote(db, spec, exclude_order=order_id)

    def queue_paid(self, db: sqlite3.Connection, order_id: str) -> None:
        db.execute("UPDATE supplier_intents SET state='queued',budget_held=1,updated_at=? "
                   "WHERE order_id=? AND state='draft'", (self.store.clock(), order_id))
        db.execute("UPDATE orders SET state='paid',error_code='supplier_queued' WHERE id=?", (order_id,))

    def info(self, order_id: str, db: sqlite3.Connection | None = None) -> dict | None:
        if db is None:
            with self.store.connection() as conn:
                return self.info(order_id, conn)
        row = db.execute("SELECT state,supplier_reference,hold_reason,product_type,resolution_version "
                         "FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def info_many(self, order_ids: list[str]) -> dict[str, dict]:
        if not order_ids:
            return {}
        placeholders = ",".join("?" * len(order_ids))
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT order_id,state,supplier_reference,hold_reason,product_type,"
                f"resolution_version FROM supplier_intents WHERE order_id IN ({placeholders})",
                order_ids,
            ).fetchall()
        return {row["order_id"]: dict(row) for row in rows}

    def preview_input(self, order_id: str, user_id: int) -> dict:
        with self.store.connection() as db:
            row = db.execute("SELECT i.request_ciphertext FROM supplier_intents i JOIN orders o ON o.id=i.order_id "
                             "WHERE o.id=? AND o.user_id=?", (order_id, user_id)).fetchone()
            if not row:
                raise ShopError("Order not found")
            body = self.decrypt(row[0])
            return {k: body[k] for k in ("customer_email", "slot_months") if k in body}

    def recover_interrupted(self) -> None:
        with self.store.transaction() as db:
            now = self.store.clock()
            db.execute("UPDATE supplier_intents SET state='uncertain',hold_reason='process_interrupted',"
                       "updated_at=? WHERE state='processing' AND lease_until<=?", (now, now))

    def inspect(self, order_id: str) -> dict:
        """Sensitive local operator export. Never send to a chat or a log."""
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
            if not row:
                raise ShopError("Supplier order not found")
            request = self.decrypt(row["request_ciphertext"])
            request.pop("key", None)
            return {"order_id": order_id, "state": row["state"], "provider": row["provider"],
                    "request": request,
                    "idempotency_key": row["idempotency_key"], "attempts": row["attempts"],
                    "supplier_reference": row["supplier_reference"], "hold_reason": row["hold_reason"],
                    "response": self.decrypt(row["response_ciphertext"]) if row["response_ciphertext"] else None}

    def claim(self) -> dict | None:
        with self.store.transaction() as db:
            now = self.store.clock()
            # A crash after request transmission is ambiguous. It must not cause
            # an automatic replay after an undocumented idempotency-retention period.
            db.execute("UPDATE supplier_intents SET state='uncertain',hold_reason='process_interrupted',"
                       "updated_at=? WHERE state='processing' AND lease_until<=?", (now, now))
            # Exclude disabled or cooling-down providers in SQL so a pile of
            # their intents cannot starve a claimable intent from another
            # provider beyond the fetch limit.
            blocked = {name for name in self._provider_names()
                       if not self.settings_for(name).enabled}
            for meta in db.execute("SELECT key,value FROM metadata WHERE key LIKE '%_not_before'"):
                if float(meta[1]) > now:
                    blocked.add(meta[0][:-len("_not_before")])
            query = ("SELECT i.* FROM supplier_intents i JOIN orders o ON o.id=i.order_id "
                     "WHERE i.state IN ('queued','retry_wait','retry_approved') AND i.next_attempt_at<=? "
                     "AND o.state='paid'")
            params: list = [now]
            if blocked:
                query += f" AND i.provider NOT IN ({','.join('?' * len(blocked))})"
                params.extend(sorted(blocked))
            rows = db.execute(query + " ORDER BY i.created_at LIMIT 10", params).fetchall()
            for row in rows:
                provider = row["provider"]
                settings = self.settings_for(provider)
                if self.cooldown_until(provider, db) > now:
                    continue
                if not settings.enabled:
                    continue
                if row["key_hash"] != settings.key_fingerprint:
                    db.execute("UPDATE supplier_intents SET state='uncertain',hold_reason='buyer_key_changed' "
                               "WHERE order_id=?", (row["order_id"],))
                    continue
                try:
                    # Approval never bypasses the current budget, price or wallet checks.
                    self.checkout(db, row["order_id"])
                except ShopError:
                    db.execute("UPDATE supplier_intents SET state=CASE WHEN attempts>0 THEN 'uncertain' ELSE 'blocked' END,"
                               "hold_reason='preflight_blocked',budget_held=CASE WHEN attempts>0 THEN 1 ELSE 0 END,"
                               "updated_at=? WHERE order_id=?", (now, row["order_id"]))
                    continue
                db.execute("UPDATE supplier_intents SET state='processing',attempts=attempts+1,"
                           "budget_held=1,first_sent_at=coalesce(first_sent_at,?),lease_until=?,updated_at=? "
                           "WHERE order_id=?", (now, now + 180, now, row["order_id"]))
                result = dict(row)
                result["body"] = self.decrypt(row["request_ciphertext"])
                return result
            return None

    def finish(self, order_id: str, result: PurchaseResult) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT i.*,o.state AS order_state FROM supplier_intents i "
                             "JOIN orders o ON o.id=i.order_id WHERE i.order_id=?", (order_id,)).fetchone()
            if not row:
                raise ShopError("Supplier order not found")
            if row["state"] != "processing":
                raise ShopError("Supplier response is stale; do not overwrite a resolved order")
            hold = None
            if result.currency != row["currency"] or result.amount > money(row["max_cost"]):
                hold = "supplier_price_or_currency_changed"
            result_type = result.product_type or (result.raw.get("order", {}) or {}).get("productType")
            if result_type != row["product_type"]:
                hold = "supplier_product_type_mismatch"
            if row["order_state"] != "paid":
                hold = "customer_payment_no_longer_payable"
            state = "uncertain" if hold else result.status
            db.execute("UPDATE supplier_intents SET state=?,response_ciphertext=?,delivery_ciphertext=?,"
                       "supplier_reference=?,actual_cost=?,hold_reason=?,lease_until=0,updated_at=? WHERE order_id=?",
                       (state, self.encrypt(result.raw), self.encrypt(result.payload), result.reference,
                        str(result.amount), hold, self.store.clock(), order_id))
            if state == "completed":
                self._allocate_delivery(db, order_id, result.payload, row["provider"])
            else:
                db.execute("UPDATE orders SET error_code=? WHERE id=? AND state='paid'",
                           ("supplier_" + state, order_id))

    def _allocate_delivery(self, db: sqlite3.Connection, order_id: str, payload: str,
                           provider: str = "canboso") -> None:
        order = db.execute("SELECT sku FROM orders WHERE id=?", (order_id,)).fetchone()
        digest = hashlib.sha256((provider + "-delivery:" + order_id).encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO stock(sku,fingerprint,ciphertext,state,order_id,created_at) "
                   "VALUES (?,?,?,'sold',?,?)", (order["sku"], digest,
                   self.store.cipher.encrypt(payload.encode()).decode(), order_id, self.store.clock()))
        db.execute("UPDATE orders SET state='paid',error_code=NULL,next_attempt_at=0,updated_at=? WHERE id=?",
                   (self.store.clock(), order_id))

    def fail(self, order_id: str, code: str, *, rejected: bool = False, retry_after: int = 0,
             raw: dict | None = None) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT attempts FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
            state = "failed" if rejected else "uncertain"
            # No POST auto-retries, including rate limits. Later rejection does
            # not prove an earlier ambiguous attempt never charged the wallet.
            if rejected and row and row["attempts"] > 1:
                state, rejected = "uncertain", False
            db.execute("UPDATE supplier_intents SET state=?,hold_reason=?,next_attempt_at=?,lease_until=0,"
                       "budget_held=?,response_ciphertext=?,updated_at=? WHERE order_id=?",
                       (state, code, self.store.clock() + retry_after, 0 if rejected else 1,
                        self.encrypt(raw) if raw else "", self.store.clock(), order_id))
            db.execute("UPDATE orders SET error_code=? WHERE id=? AND state='paid'", ("supplier_" + state, order_id))

    def guard_refund(self, db: sqlite3.Connection, order_id: str) -> None:
        row = db.execute("SELECT state FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
        if row and row["state"] in {"processing", "uncertain", "pending", "retry_wait", "retry_approved"}:
            raise ShopError("Supplier purchase is pending or uncertain. Resolve it before refunding; "
                            "a Stars refund does not cancel the supplier order")
        if row and row["state"] in {"draft", "queued", "blocked", "failed"}:
            db.execute("UPDATE supplier_intents SET state='cancelled',budget_held=0 WHERE order_id=?", (order_id,))

    def external_refund(self, db: sqlite3.Connection, order_id: str) -> None:
        row = db.execute("SELECT state FROM supplier_intents WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return
        if row["state"] in {"draft", "queued", "blocked", "failed"}:
            db.execute("UPDATE supplier_intents SET state='cancelled',budget_held=0 WHERE order_id=?", (order_id,))
        else:
            db.execute("UPDATE supplier_intents SET hold_reason='external_customer_refund' WHERE order_id=?", (order_id,))

    def uncertain_intents(self) -> list[dict]:
        """Full rows for uncertain intents, for read-only provider recovery."""
        with self.store.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM supplier_intents WHERE state='uncertain' ORDER BY created_at LIMIT 20")]

    def complete_recovered(self, order_id: str, result: PurchaseResult) -> str | None:
        """Apply a purchase result obtained from a provider's documented
        order-lookup/history endpoint to an uncertain intent. Same hold checks
        as finish(); nothing is re-sent and no evidence is discarded.
        Returns the hold reason when the result was held for an operator
        (the intent stays uncertain and is re-checked slowly, not every pass)."""
        with self.store.transaction() as db:
            row = db.execute("SELECT i.*,o.state AS order_state FROM supplier_intents i "
                             "JOIN orders o ON o.id=i.order_id WHERE i.order_id=?", (order_id,)).fetchone()
            if not row:
                raise ShopError("Supplier order not found")
            if row["state"] != "uncertain":
                raise ShopError("Only an uncertain supplier order can be recovered by lookup")
            hold = None
            if result.currency != row["currency"] or result.amount > money(row["max_cost"]):
                hold = "supplier_price_or_currency_changed"
            if result.product_type and result.product_type != row["product_type"]:
                hold = "supplier_product_type_mismatch"
            if row["order_state"] != "paid":
                hold = "customer_payment_no_longer_payable"
            state = "uncertain" if hold else result.status
            # A held result still needs an operator: re-check hourly at most,
            # so auto-recovery cannot hot-loop on it or spam notifications.
            next_attempt = self.store.clock() + 3600 if hold else 0
            db.execute("UPDATE supplier_intents SET state=?,response_ciphertext=?,delivery_ciphertext=?,"
                       "supplier_reference=?,actual_cost=?,hold_reason=?,lease_until=0,"
                       "next_attempt_at=?,resolution_version=resolution_version+1,updated_at=? "
                       "WHERE order_id=?",
                       (state, self.encrypt(result.raw), self.encrypt(result.payload),
                        result.reference, str(result.amount),
                        hold or "recovered_via_supplier_lookup", next_attempt,
                        self.store.clock(), order_id))
            if state == "completed":
                self._allocate_delivery(db, order_id, result.payload, row["provider"])
            else:
                db.execute("UPDATE orders SET error_code=? WHERE id=? AND state='paid'",
                           ("supplier_" + state, order_id))
        return hold

    def review(self) -> list[dict]:
        with self.store.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT i.order_id,i.provider,i.state,i.supplier_reference,i.hold_reason,i.resolution_version,o.user_id "
                "FROM supplier_intents i JOIN orders o ON o.id=i.order_id WHERE i.state IN "
                "('pending','uncertain','blocked','failed','retry_wait') OR i.hold_reason='external_customer_refund' "
                "ORDER BY i.created_at LIMIT 20")]

    def resolve(self, order_id: str, action: str, evidence: str, operator_id: str,
                *, delivery: str = "") -> None:
        if action not in {"retry_same_request", "stop_for_refund", "fulfill"}:
            raise ShopError("Unsupported supplier resolution")
        if not isinstance(evidence, str) or not 12 <= len(evidence.strip()) <= 2000:
            raise ShopError("Record 12-2000 characters of supplier verification evidence")
        with self.store.transaction() as db:
            row = db.execute("SELECT i.*,o.state AS order_state FROM supplier_intents i JOIN orders o ON o.id=i.order_id "
                             "WHERE i.order_id=?", (order_id,)).fetchone()
            if not row or row["state"] not in {"blocked", "failed", "uncertain", "pending"} or row["order_state"] != "paid":
                raise ShopError("This supplier order cannot be manually resolved in its current state")
            provider = row["provider"]
            if action == "retry_same_request":
                if not providers.idempotent_purchases(provider):
                    raise ShopError(
                        f"{providers.display(provider)} has no purchase idempotency; never resend "
                        "a purchase. Wait for history-based recovery or stop for refund"
                    )
                self.assert_enabled(provider)
                if row["state"] == "pending" or row["key_hash"] != self.settings_for(provider).key_fingerprint:
                    raise ShopError("Do not retry an accepted pending order or change the buyer key")
                # Record approval offline, even during a cooldown. Claim always
                # rechecks fresh pricing, wallet and budget before sending POST.
                db.execute("UPDATE supplier_intents SET budget_held=1 WHERE order_id=?", (order_id,))
                new_state = "retry_approved"
            elif action == "stop_for_refund":
                new_state = "resolved_for_refund"
            else:
                if not isinstance(delivery, str) or not 1 <= len(delivery.encode()) <= 1_000_000:
                    raise ShopError("Verified fulfillment needs a non-empty delivery text file (max 1 MB)")
                self._allocate_delivery(db, order_id, delivery, provider)
                new_state = "completed"
            db.execute("UPDATE supplier_intents SET state=?,next_attempt_at=0,resolution_version=resolution_version+1,"
                       "hold_reason='operator_verified',updated_at=? WHERE order_id=?",
                       (new_state, self.store.clock(), order_id))
            db.execute("INSERT INTO supplier_audit(order_id,operator_id,action,evidence_ciphertext,created_at) "
                       "VALUES (?,?,?,?,?)", (order_id, operator_id, action, self.encrypt(evidence), self.store.clock()))
