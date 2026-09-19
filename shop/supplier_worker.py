"""Durable post-payment supplier purchases. No implicit POST replays."""
from __future__ import annotations

import asyncio
import logging
import sys

from .canboso import CanbosoError, PurchaseRejected, RateLimited
from .delivery import DeliveryWorker
from .store import Store

log = logging.getLogger(__name__)


class SupplierWorker:
    def __init__(self, store: Store, client, delivery: DeliveryWorker,
                 pricer=None, router=None):
        self.store = store
        # One client per provider; a bare client is the original Canboso one.
        self.clients: dict = dict(client) if isinstance(client, dict) else {"canboso": client}
        self.client = self.clients.get("canboso")
        self.delivery = delivery
        self.pricer = pricer
        self.router = router
        self.last_sync = float("-inf")
        self.last_review = float("-inf")
        self.reviewed: set[tuple] = set()
        # Set by kick() when a supplier payment lands; the 5s sweep remains as
        # the retry/recovery safety net.
        self.wake = asyncio.Event()

    def kick(self) -> None:
        self.wake.set()

    async def synchronize(self) -> bool:
        """Refresh every configured provider; route and reprice from the fresh
        snapshots. A failing or cooling-down provider sits out the round
        without pausing the healthy ones."""
        snapshots: dict[str, dict] = {}
        for provider, client in self.clients.items():
            if not self.store.supplier.settings_for(provider).enabled:
                continue
            if await asyncio.to_thread(self.store.supplier.cooldown_until, provider) > self.store.clock():
                continue
            try:
                products = await client.products()
                balance = await client.balance()
                await asyncio.to_thread(
                    self.store.supplier.cache_snapshot, provider, products, balance
                )
            except CanbosoError as exc:
                await asyncio.to_thread(
                    self.store.supplier.defer_network, exc.retry_after or 60, provider
                )
                log.warning("Supplier synchronization paused for %s (%s)", provider, exc.code)
                continue
            snapshots[provider] = products
        if not snapshots:
            return False
        self.last_sync = self.store.clock()
        await self.route_and_reprice(snapshots)
        return True

    async def route_and_reprice(self, snapshots: dict) -> None:
        """Pick the cheapest in-stock supplier per SKU, then reprice from the
        winner's cost. A failure here must never break the sync/purchase loop."""
        if self.router is not None:
            try:
                decisions = await asyncio.to_thread(self.router.route, snapshots)
            except Exception:
                log.error("Router failed (%s)", type(sys.exc_info()[1]).__name__)
            else:
                switched = [d for d in decisions if d.changed]
                if switched:
                    lines = "\n".join(f"{d.sku} -> {d.winner} (cost {d.cost})"
                                      for d in switched[:10])
                    await self.delivery.notify_admins(
                        "Supplier routing switched:\n" + lines
                    )
                unavailable = [d for d in decisions if d.winner is None]
                if unavailable:
                    lines = "\n".join(f"{d.sku}: {d.reason}" for d in unavailable[:10])
                    await self.delivery.notify_admins(
                        "No supplier candidate currently sellable:\n" + lines
                    )
        if self.pricer is None:
            return
        try:
            changes = await asyncio.to_thread(self.pricer.reprice, snapshots)
        except Exception:
            log.error("Repricer failed (%s)", type(sys.exc_info()[1]).__name__)
            return
        for change in changes:
            if change.flagged:
                log.warning("Price jump clamped for %s", change.sku)
        flagged = [c for c in changes if c.flagged]
        if flagged:
            lines = "\n".join(
                f"{c.sku}: {c.old_price} -> {c.new_price} Stars (cost {c.old_cost} -> {c.new_cost})"
                for c in flagged[:10]
            )
            await self.delivery.notify_admins(
                "Supplier price jump clamped, review recommended:\n" + lines
            )

    async def purchase_one(self) -> bool:
        intent = await asyncio.to_thread(self.store.supplier.claim)
        if intent is None:
            return False
        order_id = intent["order_id"]
        client = self.clients.get(intent["provider"])
        if client is None:
            # Never sent anything; hold for the operator instead of guessing.
            await asyncio.to_thread(
                self.store.supplier.fail, order_id, "provider_client_not_configured"
            )
            return True
        # The processing lease is already durable. Cancellation or a hard crash
        # leaves an uncertain intent for operator review, never a fresh purchase.
        try:
            result = await client.purchase(intent["body"], intent["idempotency_key"])
        except RateLimited as exc:
            await asyncio.to_thread(
                self.store.supplier.defer_network, exc.retry_after or 60, intent["provider"]
            )
            await asyncio.to_thread(self.store.supplier.fail, order_id, exc.code)
        except PurchaseRejected as exc:
            await asyncio.to_thread(self.store.supplier.fail, order_id, exc.code, rejected=True)
        except CanbosoError as exc:
            await asyncio.to_thread(self.store.supplier.fail, order_id, exc.code,
                                    raw=getattr(exc, "raw", None))
        except Exception:
            # Do not stringify a remote exception: it may include keys or delivery.
            await asyncio.to_thread(self.store.supplier.fail, order_id, "unexpected_purchase_failure")
        else:
            await asyncio.to_thread(self.store.supplier.finish, order_id, result)
        self.last_sync = float("-inf")  # Refresh wallet/availability before another order.
        return True

    async def notify_reviews(self) -> None:
        if self.store.clock() - self.last_review < 30:
            return
        rows = await asyncio.to_thread(self.store.supplier.review)
        current = {(r["order_id"], r["state"], r["hold_reason"], r["resolution_version"]) for r in rows}
        for row in rows:
            key = (row["order_id"], row["state"], row["hold_reason"], row["resolution_version"])
            if key not in self.reviewed:
                await self.delivery.notify_admins(
                    f"Supplier order needs attention: {row['order_id']} ({row['state']}). "
                    "Use /supplierreview. Do not buy again or refund a pending supplier order blindly."
                )
        self.reviewed = current
        self.last_review = self.store.clock()

    async def tick(self) -> None:
        await asyncio.to_thread(self.store.expire_orders)
        await asyncio.to_thread(self.store.supplier.recover_interrupted)
        ready = True
        if self.store.clock() - self.last_sync >= 45:
            ready = await self.synchronize()
        if ready and self.store.supplier.purchases_allowed():
            await self.purchase_one()
        await self.notify_reviews()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.error("Supplier worker paused (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
