"""Supplier-linked sell pricing. Manual mode is the default; auto is opt-in per SKU.

Auto rule: sell_stars = max(ceil(cost * fx * (1 + markup_pct/100)), ceil(cost * fx) + min_profit_stars)
A single-sync cost move larger than max_jump_pct is clamped and flagged for
review instead of applied in full. Every decision lands in price_events.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from .canboso import CanbosoError, money
from .errors import ShopError

PRICING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pricing_rules (
    sku TEXT PRIMARY KEY REFERENCES products(sku),
    mode TEXT NOT NULL DEFAULT 'manual' CHECK(mode IN ('manual','auto')),
    markup_pct INTEGER NOT NULL DEFAULT 0 CHECK(markup_pct BETWEEN 0 AND 900),
    min_profit_stars INTEGER NOT NULL DEFAULT 0 CHECK(min_profit_stars >= 0),
    max_jump_pct INTEGER NOT NULL DEFAULT 25 CHECK(max_jump_pct BETWEEN 1 AND 500),
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS price_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL, old_cost TEXT, new_cost TEXT NOT NULL,
    old_price INTEGER NOT NULL, new_price INTEGER NOT NULL,
    flagged INTEGER NOT NULL CHECK(flagged IN (0,1)),
    fx_rate TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS price_events_sku ON price_events(sku, created_at DESC);
"""

MAX_PRICE_STARS = 100000  # Matches Store.upsert_product validation.


@dataclass(frozen=True)
class PriceChange:
    sku: str
    old_cost: str | None
    new_cost: str
    old_price: int
    new_price: int
    flagged: bool
    note: str = ""


def _ceil_stars(value: Decimal) -> int:
    return math.ceil(value)


class Pricer:
    def __init__(self, store, fx: dict[str, Decimal] | None = None) -> None:
        self.store = store
        # Stars per one unit of supplier currency, e.g. {"USD": Decimal("50")}.
        self.fx = fx or {}

    def set_rule(self, sku: str, *, mode: str, markup_pct: int,
                 min_profit_stars: int, max_jump_pct: int) -> None:
        if mode not in {"manual", "auto"}:
            raise ShopError("Pricing mode must be manual or auto")
        if type(markup_pct) is not int or not 0 <= markup_pct <= 900:
            raise ShopError("markup_pct must be an integer from 0 to 900")
        if type(min_profit_stars) is not int or not 0 <= min_profit_stars <= MAX_PRICE_STARS:
            raise ShopError("min_profit_stars must be an integer from 0 to 100000")
        if type(max_jump_pct) is not int or not 1 <= max_jump_pct <= 500:
            raise ShopError("max_jump_pct must be an integer from 1 to 500")
        with self.store.transaction() as db:
            product = db.execute("SELECT source FROM products WHERE sku=?", (sku,)).fetchone()
            if not product:
                raise ShopError("Unknown product SKU")
            if mode == "auto":
                if product["source"] != "supplier":
                    raise ShopError("Auto pricing requires a supplier-backed product")
                mapping = self.store.supplier.mapping(sku, db)
                if mapping["currency"] not in self.fx:
                    raise ShopError(
                        f"Set stars_fx for {mapping['currency']} in config before auto pricing"
                    )
            db.execute(
                "INSERT INTO pricing_rules VALUES (?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET "
                "mode=excluded.mode,markup_pct=excluded.markup_pct,"
                "min_profit_stars=excluded.min_profit_stars,max_jump_pct=excluded.max_jump_pct,"
                "updated_at=excluded.updated_at",
                (sku, mode, markup_pct, min_profit_stars, max_jump_pct, self.store.clock()),
            )

    def rule_for(self, sku: str, db: sqlite3.Connection | None = None) -> dict | None:
        if db is None:
            with self.store.connection() as conn:
                return self.rule_for(sku, conn)
        row = db.execute("SELECT * FROM pricing_rules WHERE sku=?", (sku,)).fetchone()
        return dict(row) if row else None

    def list_rules(self) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT r.*,p.price_stars,p.title,p.active FROM pricing_rules r "
                "JOIN products p ON p.sku=r.sku ORDER BY r.sku"
            ).fetchall()
        return [dict(r) for r in rows]

    def events(self, sku: str | None = None, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ShopError("Limit must be 1-500")
        with self.store.connection() as db:
            if sku is None:
                rows = db.execute(
                    "SELECT * FROM price_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM price_events WHERE sku=? ORDER BY id DESC LIMIT ?",
                    (sku, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def reprice(self, snapshot_products: dict, *, dry_run: bool = False) -> list[PriceChange]:
        """Apply auto rules against a fresh supplier snapshot. Never raises on
        per-product data problems; those become flagged events or skips."""
        changes: list[PriceChange] = []
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT r.*,p.price_stars FROM pricing_rules r JOIN products p ON p.sku=r.sku "
                "WHERE r.mode='auto' AND p.active=1"
            ).fetchall()
            available = {
                p.get("productId"): p
                for p in snapshot_products.get("products", [])
                if isinstance(p, dict)
            }
            for rule in rows:
                sku = rule["sku"]
                old_price = rule["price_stars"]
                try:
                    mapping = self.store.supplier.mapping(sku, db)
                    found = available.get(mapping["product_id"])
                    if not found:
                        continue  # Out of catalog; preflight already blocks purchases.
                    price = found.get("price", {})
                    if price.get("currency") != mapping["currency"]:
                        raise ShopError("currency changed")
                    cost = money(price.get("amount"), positive=True)
                    rate = self.fx.get(mapping["currency"])
                    if rate is None:
                        continue  # set_rule blocks this; a removed fx entry just skips.
                    cost_stars = _ceil_stars(cost * rate)
                    target = max(
                        _ceil_stars(cost * rate * (1 + rule["markup_pct"] / Decimal(100))),
                        cost_stars + rule["min_profit_stars"],
                    )
                    flagged, note = False, ""
                    if target > MAX_PRICE_STARS:
                        target, flagged, note = MAX_PRICE_STARS, True, "clamped to max Stars price"
                    if old_price > 0:
                        jump = abs(target - old_price) * 100 / old_price
                        if jump > rule["max_jump_pct"]:
                            direction = 1 if target > old_price else -1
                            allowed = old_price * (100 + direction * rule["max_jump_pct"]) // 100
                            allowed = min(max(int(allowed), 1), MAX_PRICE_STARS)
                            if allowed != target:
                                target, flagged = allowed, True
                                note = (note + "; " if note else "") + "jump clamped for review"
                    change = PriceChange(
                        sku=sku,
                        old_cost=mapping["max_cost"],
                        new_cost=str(cost),
                        old_price=old_price,
                        new_price=target,
                        flagged=flagged,
                        note=note,
                    )
                    if target == old_price and not flagged:
                        continue  # No change, no event noise.
                    changes.append(change)
                    if dry_run:
                        continue
                    db.execute(
                        "UPDATE products SET price_stars=? WHERE sku=?", (target, sku)
                    )
                    # Keep the preflight cap tracking the new cost so checkout
                    # still blocks if the supplier jumps again between syncs.
                    new_cap = cost * (1 + Decimal(rule["max_jump_pct"]) / Decimal(100))
                    # Router-managed SKUs also carry the owner's approved
                    # per-candidate cap; never derive above it.
                    cand = db.execute(
                        "SELECT max_cost FROM supplier_candidates "
                        "WHERE sku=? AND provider=? AND product_id=? AND active=1",
                        (sku, mapping.get("provider", ""), mapping["product_id"]),
                    ).fetchone()
                    if cand:
                        new_cap = min(new_cap, money(cand["max_cost"], positive=True))
                    spec = dict(mapping)
                    spec["max_cost"] = str(new_cap.quantize(Decimal("0.0001")))
                    db.execute(
                        "UPDATE supplier_mappings SET specification=? WHERE sku=?",
                        (json.dumps(spec, sort_keys=True), sku),
                    )
                    db.execute(
                        "INSERT INTO price_events "
                        "(sku,old_cost,new_cost,old_price,new_price,flagged,fx_rate,note,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (sku, change.old_cost, change.new_cost, old_price, target,
                         int(flagged), str(rate), note, self.store.clock()),
                    )
                except (ShopError, CanbosoError):
                    continue  # Bad supplier data for one SKU must not stop the rest.
            if dry_run:
                db.rollback()
        return changes
