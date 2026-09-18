"""Cheapest-wins routing across supplier providers.

A SKU can list the same logical product at several suppliers
(supplier_candidates). At each sync the router picks the cheapest candidate
that is available, within its approved max_cost, and rewrites the SKU's
active supplier mapping. Checkout and repricing then run unchanged against
that winner; the per-sync max_cost cap still guards between syncs.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from .canboso import CanbosoError, money
from .errors import ShopError
from .providers import registered

ROUTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_candidates (
    sku TEXT NOT NULL REFERENCES products(sku),
    provider TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    max_cost TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at REAL NOT NULL,
    PRIMARY KEY (sku, provider, product_id)
);
CREATE INDEX IF NOT EXISTS supplier_candidates_sku ON supplier_candidates(sku, active);
"""


@dataclass(frozen=True)
class RouteDecision:
    sku: str
    winner: str | None  # "provider:product_id" of the chosen candidate
    cost: str | None
    cost_stars: int | None
    changed: bool  # active mapping was rewritten
    reason: str


class Router:
    def __init__(self, store, fx: dict[str, Decimal] | None = None) -> None:
        self.store = store
        self.fx = fx or {}

    def add_candidate(self, sku: str, *, provider: str, product_id: str,
                      product_type: str, currency: str, max_cost: str) -> None:
        if not registered(provider):
            raise ShopError(f"Provider {provider!r} is not registered in shop/providers.py")
        if not isinstance(product_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", product_id):
            raise ShopError("Set a real supplier product_id from that provider's catalog")
        if product_type not in {"account", "slot"}:
            raise ShopError("product_type must be account or slot")
        if currency not in {"VND", "USD"}:
            raise ShopError("Use the provider's documented wallet currency, VND or USD")
        try:
            ceiling = money(max_cost, positive=True)
        except CanbosoError as exc:
            raise ShopError("Set a positive max_cost in provider currency") from exc
        with self.store.transaction() as db:
            product = db.execute("SELECT source FROM products WHERE sku=?", (sku,)).fetchone()
            if not product or product["source"] != "supplier":
                raise ShopError("Routing candidates need a supplier-backed product SKU")
            db.execute(
                "INSERT INTO supplier_candidates VALUES (?,?,?,?,?,?,1,?) "
                "ON CONFLICT(sku,provider,product_id) DO UPDATE SET "
                "product_type=excluded.product_type,currency=excluded.currency,"
                "max_cost=excluded.max_cost,active=1",
                (sku, provider, product_id, product_type, currency, str(ceiling),
                 self.store.clock()),
            )

    def remove_candidate(self, sku: str, *, provider: str, product_id: str) -> None:
        with self.store.transaction() as db:
            cursor = db.execute(
                "DELETE FROM supplier_candidates WHERE sku=? AND provider=? AND product_id=?",
                (sku, provider, product_id),
            )
            if not cursor.rowcount:
                raise ShopError("No such routing candidate")

    def candidates(self, sku: str | None = None) -> list[dict]:
        with self.store.connection() as db:
            if sku is None:
                rows = db.execute(
                    "SELECT * FROM supplier_candidates ORDER BY sku, provider"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM supplier_candidates WHERE sku=? ORDER BY provider", (sku,)
                ).fetchall()
        return [dict(r) for r in rows]

    def _cheapest(self, db: sqlite3.Connection, sku: str,
                  snapshots: dict[str, dict]) -> tuple[dict | None, Decimal | None, str]:
        """Return (winning candidate, its cost, reason)."""
        rows = db.execute(
            "SELECT * FROM supplier_candidates WHERE sku=? AND active=1", (sku,)
        ).fetchall()
        if not rows:
            return None, None, "no candidates"
        best, best_cost, best_comparable, rejected = None, None, None, []
        for cand in rows:
            key = f"{cand['provider']}:{cand['product_id']}"
            snapshot = snapshots.get(cand["provider"]) or {}
            found = next(
                (p for p in snapshot.get("products", [])
                 if isinstance(p, dict) and p.get("productId") == cand["product_id"]),
                None,
            )
            if not found:
                rejected.append(f"{key} not in catalog")
                continue
            availability = found.get("availability", {}) or {}
            if availability.get("available", 0) < 1:
                rejected.append(f"{key} out of stock")
                continue
            price = found.get("price", {}) or {}
            if price.get("currency") != cand["currency"]:
                rejected.append(f"{key} currency changed")
                continue
            try:
                cost = money(price.get("amount"), positive=True)
                cap = money(cand["max_cost"], positive=True)
            except CanbosoError:
                rejected.append(f"{key} bad price data")
                continue
            if cost > cap:
                rejected.append(f"{key} over approved cap")
                continue
            rate = self.fx.get(cand["currency"])
            if rate is None:
                rejected.append(f"{key} missing stars_fx for {cand['currency']}")
                continue
            comparable = cost * rate  # Compare in Stars, never raw currency units.
            if best is None or comparable < best_comparable:
                best, best_cost, best_comparable = cand, cost, comparable
        if best is None:
            return None, None, "; ".join(rejected) or "no candidate available"
        return best, best_cost, ""

    def route(self, snapshots: dict[str, dict], *, dry_run: bool = False) -> list[RouteDecision]:
        """Pick the cheapest in-stock candidate per SKU and make it the active
        mapping. Runs inside one transaction; dry_run reports without writing."""
        decisions: list[RouteDecision] = []
        with self.store.transaction() as db:
            skus = [
                r["sku"] for r in db.execute(
                    "SELECT DISTINCT sku FROM supplier_candidates WHERE active=1"
                ).fetchall()
            ]
            for sku in skus:
                winner, cost, reason = self._cheapest(db, sku, snapshots)
                if winner is None:
                    # Leave the last working mapping in place; preflight blocks
                    # checkout if it is genuinely unsellable.
                    decisions.append(RouteDecision(sku, None, None, None, False,
                                                   reason or "no candidate available"))
                    continue
                rate = self.fx[winner["currency"]]  # Guaranteed present by _cheapest.
                cost_stars = math.ceil(cost * rate)
                current = db.execute(
                    "SELECT specification FROM supplier_mappings WHERE sku=?", (sku,)
                ).fetchone()
                current_spec = json.loads(current["specification"]) if current else {}
                same = (
                    current_spec.get("provider") == winner["provider"]
                    and current_spec.get("product_id") == winner["product_id"]
                )
                changed = not same
                if changed and not dry_run:
                    self.store.supplier.set_mapping(db, sku, {
                        "provider": winner["provider"],
                        "product_id": winner["product_id"],
                        "product_type": winner["product_type"],
                        "currency": winner["currency"],
                        "max_cost": winner["max_cost"],
                    })
                decisions.append(RouteDecision(
                    sku, f"{winner['provider']}:{winner['product_id']}", str(cost),
                    cost_stars, changed, "cheapest in-stock candidate" if not reason else reason,
                ))
            if dry_run:
                db.rollback()
        return decisions
