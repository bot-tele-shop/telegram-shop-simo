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
                await asyncio.to_thread(
                    self.store.save_updates,
                    [
                        (
                            update.update_id,
                            update_kind(update),
                            update.model_dump_json(exclude_none=True),
                        )
                        for update in updates
                    ],
                )
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
        while True:
            item = await asyncio.to_thread(self.store.claim_update, kind)
            if item is None:
                await asyncio.sleep(0.15)
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
