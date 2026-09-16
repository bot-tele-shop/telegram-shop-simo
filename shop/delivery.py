"""Retryable delivery. A retry never obtains a second inventory unit."""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, Message

from .store import Store

log = logging.getLogger(__name__)


async def send_delivery(
    bot: Bot, user_id: int, order_id: str, title: str, payload: str
) -> Message:
    text = (
        f"Your order is ready: {title}\n"
        f"Order: {order_id}\n\n{payload}\n\n"
        "Keep this item private. Reopen it with /orders.\n"
        "For purchase issues, use /paysupport."
        if title
        else f"Delivery for order {order_id}\n\n{payload}"
    )
    # Count the complete message, including astral characters and the delivery wrapper.
    if len(text.encode("utf-16-le")) // 2 > 3500:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", order_id)[:64] or "delivery"
        return await bot.send_document(
            chat_id=user_id,
            document=BufferedInputFile(payload.encode("utf-8"), filename=f"order-{safe_id}.txt"),
            caption=(
                f"Delivery for order {order_id}\n"
                "Your complete item is in the UTF-8 text file. Keep it private.\n"
                "Reopen it with /orders. For purchase issues, use /paysupport."
            ),
            parse_mode=None,
            protect_content=True,
            request_timeout=30,
        )
    return await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=None,
        protect_content=True,
        link_preview_options={"is_disabled": True},
        request_timeout=30,
    )


class DeliveryWorker:
    def __init__(self, store: Store, bot: Bot, admin_ids: frozenset[int]) -> None:
        self.store = store
        self.bot = bot
        self.admin_ids = admin_ids

    async def notify_admins(self, text: str) -> None:
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, parse_mode=None)
            except TelegramAPIError:
                log.warning("Admin notification could not be delivered")

    async def deliver(self, order_id: str) -> bool:
        delivery = await asyncio.to_thread(self.store.claim_delivery, order_id)
        if delivery is None:
            return False
        try:
            message = await send_delivery(
                self.bot, delivery.user_id, delivery.order_id, delivery.title, delivery.payload
            )
        except (TelegramAPIError, OSError, asyncio.TimeoutError) as exc:
            await asyncio.to_thread(self.store.fail_delivery, delivery, type(exc).__name__)
            order = await asyncio.to_thread(self.store.get_order, order_id)
            if order["state"] == "delivery_failed":
                await self.notify_admins(
                    f"Delivery needs attention. Order: {order_id}. Use /admin."
                )
            return False
        # A failure here leaves the lease intact. Recovery resends the same item,
        # never another unit. Telegram message/document sends have no idempotency key.
        return await asyncio.to_thread(self.store.finish_delivery, delivery, message.message_id)

    async def tick(self) -> None:
        await asyncio.to_thread(self.store.expire_orders)
        for order_id in await asyncio.to_thread(self.store.due_deliveries):
            try:
                await self.deliver(order_id)
            except Exception as exc:
                # No exception text: remote errors can contain private payloads.
                log.error("Delivery processing failed (%s)", type(exc).__name__)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.error("Delivery worker paused (%s)", type(exc).__name__)
            await asyncio.sleep(5)
