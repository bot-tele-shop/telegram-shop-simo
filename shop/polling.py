"""Persist updates before acknowledging them to Telegram.

Separate checkout, payment and UI workers keep slow stock uploads off the
pre-checkout deadline. Durable payment retries survive process restarts.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import Update

from .store import Store

log = logging.getLogger(__name__)


def update_kind(update: Update) -> str:
    if update.pre_checkout_query:
        return "checkout"
    if update.message and (update.message.successful_payment or update.message.refunded_payment):
        return "payment"
    return "ui"


class DurablePolling:
    def __init__(self, bot: Bot, dispatcher: Dispatcher, store: Store) -> None:
        self.bot, self.dispatcher, self.store = bot, dispatcher, store
        # Wake-up signal only; the durable inbox stays the source of truth, so a
        # crash between queue push and claim loses nothing.
        self.queues: dict[str, asyncio.Queue[int]] = {
            kind: asyncio.Queue() for kind in ("checkout", "payment", "ui")
        }

    async def receive(self) -> None:
        while True:
            offset = await asyncio.to_thread(self.store.polling_offset)
            try:
                updates = await self.bot.get_updates(
                    offset=offset,
                    timeout=20,
                    request_timeout=30,
                    limit=50,
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                )
                batch = [
                    (
                        update.update_id,
                        update_kind(update),
                        update.model_dump_json(exclude_none=True),
                    )
                    for update in updates
                ]
                await asyncio.to_thread(self.store.save_updates, batch)
                for update_id, kind, _ in batch:
                    self.queues[kind].put_nowait(update_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(120, max(1, exc.retry_after)))
            except TelegramAPIError as exc:
                log.warning(
                    "Polling interrupted (%s); retrying without advancing offset",
                    type(exc).__name__,
                )
                await asyncio.sleep(3)
            # Database failure intentionally exits the process. Do not acknowledge
            # a batch whose updates could not be made durable.

    async def consume(self, kind: str) -> None:
        queue = self.queues[kind]
        while True:
            # Fresh updates arrive instantly via the queue. The 0.5s fallback
            # sweep covers crash recovery and scheduled retries.
            try:
                await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            item = await asyncio.to_thread(self.store.claim_update, kind)
            if item is None:
                continue
            update_id, body = item
            try:
                update = Update.model_validate_json(body, context={"bot": self.bot})
                await asyncio.wait_for(self.dispatcher.feed_update(self.bot, update), timeout=60)
            except Exception as exc:
                await asyncio.to_thread(self.store.retry_update, update_id, type(exc).__name__)
                log.warning("Update handler needs retry (%s, %s)", kind, type(exc).__name__)
            else:
                await asyncio.to_thread(self.store.finish_update, update_id)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self.receive())
            for kind in ("checkout", "payment", "ui"):
                group.create_task(self.consume(kind))
