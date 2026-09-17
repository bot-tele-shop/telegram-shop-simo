"""Minimal Telegram Bot API client for Cloudflare Python Workers."""

from httpclient import request


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"

    async def call(self, method, **params):
        data = await request(
            "POST",
            f"{self.base}/{method}",
            headers={"Content-Type": "application/json"},
            payload=params,
        )
        if not data or not data.get("ok"):
            raise RuntimeError(f"telegram {method} rejected the call")
        return data["result"]

    async def send_message(
        self, chat_id, text, keyboard=None, parse_mode=None, reply_keyboard=None
    ):
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        elif reply_keyboard:
            params["reply_markup"] = {
                "keyboard": reply_keyboard,
                "resize_keyboard": True,
                "is_persistent": True,
                "input_field_placeholder": "Choose an option",
            }
        return await self.call("sendMessage", **params)

    async def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        return await self.call("answerCallbackQuery", **params)

    async def send_invoice(self, chat_id, title, description, payload, amount_stars):
        return await self.call(
            "sendInvoice",
            chat_id=chat_id,
            title=title[:32],
            description=description[:255],
            payload=payload,
            provider_token="",  # empty = Telegram Stars
            currency="XTR",
            prices=[{"label": title[:32], "amount": amount_stars}],
        )

    async def answer_pre_checkout(self, query_id, ok, error=None):
        params = {"pre_checkout_query_id": query_id, "ok": ok}
        if error:
            params["error_message"] = error[:200]
        return await self.call("answerPreCheckoutQuery", **params)
