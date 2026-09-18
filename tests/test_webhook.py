"""Webhook intake: authenticate every request, persist before acknowledging."""

import asyncio
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer

from shop.config import WebhookSettings
from shop.polling import DurablePolling
from shop.webhook import WebhookServer

SECRET = "test-secret-token_123"

MESSAGE_UPDATE = {
    "update_id": 777001,
    "message": {
        "message_id": 1,
        "date": 1700000000,
        "chat": {"id": 101, "type": "private"},
        "text": "/start",
    },
}


def settings():
    return WebhookSettings(
        url="https://shop.example.invalid/telegram/webhook",
        secret=SECRET,
    )


def make_client(store):
    sink = DurablePolling(Mock(), Mock(), store)
    server = WebhookServer(Mock(), sink, settings())
    from aiohttp import web

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post(server.settings.path, server.handle)
    return TestClient(TestServer(app)), sink


def test_rejects_missing_and_wrong_secret(store):
    async def scenario():
        client, sink = make_client(store)
        async with client:
            for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
                response = await client.post(
                    settings().path, json=MESSAGE_UPDATE, headers=headers
                )
                assert response.status == 403
        assert sink.queues["ui"].empty()
        assert store.claim_update("ui") is None

    asyncio.run(scenario())


def test_valid_update_is_durable_before_ack(store):
    async def scenario():
        client, sink = make_client(store)
        async with client:
            response = await client.post(
                settings().path,
                json=MESSAGE_UPDATE,
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
            assert response.status == 200
        assert sink.queues["ui"].get_nowait() == MESSAGE_UPDATE["update_id"]

    asyncio.run(scenario())
    update_id, body = store.claim_update("ui")
    assert update_id == MESSAGE_UPDATE["update_id"]
    assert "/start" in body


def test_malformed_update_acks_and_drops(store):
    async def scenario():
        client, sink = make_client(store)
        async with client:
            response = await client.post(
                settings().path,
                json={"update_id": "not-a-real-update"},
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
            # 200, not 4xx: a permanently malformed payload must not retry forever.
            assert response.status == 200
        assert sink.queues["ui"].empty()

    asyncio.run(scenario())
    assert store.claim_update("ui") is None


def test_persistence_failure_asks_telegram_to_retry(store, monkeypatch):
    def broken(_updates):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "save_updates", broken)

    async def scenario():
        client, _sink = make_client(store)
        async with client:
            response = await client.post(
                settings().path,
                json=MESSAGE_UPDATE,
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
            assert response.status == 500

    asyncio.run(scenario())


def test_webhook_settings_validation():
    assert not WebhookSettings().enabled
    assert WebhookSettings().problems() == []
    good = settings()
    assert good.enabled and good.problems() == []
    assert WebhookSettings(url="http://insecure.example", secret=SECRET).problems()
    assert WebhookSettings(url=good.url, secret="").problems()
    assert WebhookSettings(url=good.url, secret=SECRET, path="../etc").problems()
    assert WebhookSettings(url=good.url, secret=SECRET, listen_port=0).problems()
