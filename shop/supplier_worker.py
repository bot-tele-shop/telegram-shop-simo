"""Durable post-payment supplier purchases. No implicit POST replays."""
from __future__ import annotations

import asyncio
import logging

from .canboso import CanbosoClient, CanbosoError, PurchaseRejected, RateLimited
from .delivery import DeliveryWorker
from .store import Store

log = logging.getLogger(__name__)


class SupplierWorker:
    def __init__(self, store: Store, client: CanbosoClient, delivery: DeliveryWorker):
        self.store = store
        self.client = client
        self.delivery = delivery
        self.last_sync = float("-inf")
        self.last_review = float("-inf")
        self.reviewed: set[tuple] = set()

    async def synchronize(self) -> bool:
        if self.store.supplier.cooldown_until() > self.store.clock():
            return False
        try:
            products = await self.client.products()
            balance = await self.client.balance()
            await asyncio.to_thread(self.store.supplier.cache_snapshot, products, balance)
        except CanbosoError as exc:
            await asyncio.to_thread(self.store.supplier.defer_network, exc.retry_after or 60)
            log.warning("Supplier synchronization paused (%s)", exc.code)
            return False
        self.last_sync = self.store.clock()
        return True

    async def purchase_one(self) -> bool:
        intent = await asyncio.to_thread(self.store.supplier.claim)
        if intent is None:
            return False
        order_id = intent["order_id"]
        # The processing lease is already durable. Cancellation or a hard crash
        # leaves an uncertain intent for operator review, never a fresh purchase.
        try:
            result = await self.client.purchase(intent["body"], intent["idempotency_key"])
        except RateLimited as exc:
            await asyncio.to_thread(self.store.supplier.defer_network, exc.retry_after or 60)
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
        if ready and self.store.supplier.settings.allow_purchases:
            await self.purchase_one()
        await self.notify_reviews()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.error("Supplier worker paused (%s)", type(exc).__name__)
            await asyncio.sleep(5)
