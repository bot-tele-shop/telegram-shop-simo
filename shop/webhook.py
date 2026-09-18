"""Webhook intake. Updates take the same durable inbox path as polling.

TLS terminates at the operator's reverse proxy (Caddy, Nginx, a tunnel). This
server listens on a local address and authenticates callers with Telegram's
secret_token header; without it the endpoint is an unauthenticated write into
the order pipeline, so every request without a matching token is rejected.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web
from aiogram import Bot
from aiogram.types import Update

from .config import WebhookSettings
from .polling import DurablePolling

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(self, bot: Bot, sink: DurablePolling, settings: WebhookSettings) -> None:
        self.bot = bot
        self.sink = sink
        self.settings = settings
        self.runner: web.AppRunner | None = None

    async def handle(self, request: web.Request) -> web.Response:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token.encode(), self.settings.secret.encode()):
            return web.Response(status=403)
        try:
            update = Update.model_validate(await request.json(), context={"bot": self.bot})
        except Exception:
            # 200, not 4xx: a permanently malformed payload must not retry forever.
            log.warning("Webhook received an unparseable update; dropping it")
            return web.Response(status=200)
        try:
            await self.sink.ingest([update])
        except Exception:
            # 500 tells Telegram to retry later. Never ack what is not durable.
            log.error("Webhook intake could not persist an update; asking Telegram to retry")
            return web.Response(status=500)
        return web.Response(status=200)

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post(self.settings.path, self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.settings.listen_host, self.settings.listen_port)
        await site.start()
        log.info(
            "Webhook intake listening on %s:%s%s",
            self.settings.listen_host,
            self.settings.listen_port,
            self.settings.path,
        )

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
