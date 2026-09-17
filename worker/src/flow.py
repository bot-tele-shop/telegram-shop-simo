"""Buyer flow for the Digital Shelf webhook Worker.

Mirrors shop/bot.py semantics: terms gate, Stars invoices, atomic fulfillment.
Every step is idempotent so Telegram webhook retries are always safe.
"""

import hashlib
import uuid

from db import DB
from fernet import Fernet
from storefront import back_rows, information_text, menu_rows, welcome_text
from telegram import Telegram

TERMS_COMMANDS = {"/terms", "/privacy"}
ORDER_STATES = {
    "invoice": "awaiting payment",
    "checkout": "payment in progress",
    "expired": "expired",
    "cancelled": "cancelled",
    "paid": "paid",
    "delivering": "being fulfilled by the seller",
    "delivered": "delivered",
    "delivery_failed": "delivery failed",
    "needs_refund": "refund due",
    "refund_pending": "refund in progress",
    "refunded": "refunded",
}


class Ctx:
    def __init__(self, env):
        self.env = env
        self.tg = Telegram(str(env.TELEGRAM_BOT_TOKEN))
        self.db = DB(str(env.SUPABASE_URL), str(env.SUPABASE_SERVICE_ROLE_KEY))
        self.fernet = Fernet(str(env.STOCK_FERNET_KEY))
        self.shop_name = str(env.SHOP_NAME)
        self.support = str(env.SUPPORT_CONTACT)
        self.terms = str(env.TERMS_TEXT)
        self.privacy = str(env.PRIVACY_TEXT)
        raw_admins = str(getattr(env, "ADMIN_IDS", "") or "")
        self.admins = [int(x) for x in raw_admins.split(",") if x.strip().isdigit()]

    @property
    def terms_version(self):
        blob = self.terms + "\n" + self.privacy
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    async def notify_admins(self, text):
        for admin in self.admins:
            try:
                await self.tg.send_message(admin, text)
            except Exception:
                pass


async def has_accepted(ctx, user_id):
    row = await ctx.db.select_one(
        "terms_acceptances",
        {"user_id": f"eq.{user_id}", "version": f"eq.{ctx.terms_version}", "select": "user_id"},
    )
    return row is not None


async def show_terms(ctx, chat_id):
    await ctx.tg.send_message(chat_id, f"Shop terms\n\n{ctx.terms}")
    await ctx.tg.send_message(
        chat_id,
        f"Privacy notice\n\n{ctx.privacy}",
        keyboard=keyboard([[("I accept these terms and privacy notice", f"accept:{ctx.terms_version}")]]),
    )


def keyboard(rows):
    return [[{"text": text, "callback_data": data} for text, data in row] for row in rows]


async def show_home(ctx, chat_id):
    await ctx.tg.send_message(
        chat_id, welcome_text(ctx.shop_name), keyboard=menu_rows(), parse_mode="HTML"
    )


async def show_information(ctx, chat_id, user_id, page):
    await ctx.tg.send_message(
        chat_id, information_text(page, user_id),
        keyboard=keyboard([[("🛍 Products", "cat:0"), ("📋 Orders", "orders")],
                           [("🏠 Main menu", "home")]])
    )


async def show_support(ctx, chat_id):
    await ctx.tg.send_message(
        chat_id,
        f"Purchase support: {ctx.support}\n\n"
        "Include your order ID from /orders, not your product key or bot credentials. "
        "The merchant handles fulfillment, disputes and refund requests, not Telegram support.",
        keyboard=back_rows(),
    )


async def show_catalog(ctx, chat_id):
    products = await ctx.db.rpc("catalog_with_stock", {})
    if not products:
        await ctx.tg.send_message(chat_id, f"{ctx.shop_name}\n\nThe catalog is empty right now.",
                                  keyboard=back_rows())
        return
    rows = []
    lines = [f"{ctx.shop_name}\n\nChoose a digital product:"]
    for p in products:
        if p["source"] == "stock":
            if not p["available"]:
                continue
            availability = f"{p['available']} in stock"
        else:
            availability = "fulfilled after payment"
        lines.append(f"\n{p['title']} - {p['price_stars']} Stars ({availability})\n{p['description']}")
        rows.append([(f"Buy {p['title']} - {p['price_stars']} Stars", f"buy:{p['sku']}")])
    if not rows:
        await ctx.tg.send_message(chat_id, "Everything is sold out right now. Check back soon!",
                                  keyboard=back_rows())
        return
    rows.append([("Terms", "terms"), ("Privacy", "privacy")])
    rows.append([("📋 Orders", "orders"), ("🏠 Main menu", "home")])
    await ctx.tg.send_message(chat_id, "\n".join(lines), keyboard=keyboard(rows))


async def start_checkout(ctx, callback, user_id, chat_id):
    sku = callback["data"][4:]
    if not await has_accepted(ctx, user_id):
        await ctx.tg.answer_callback(callback["id"], "Please read and accept the terms first")
        await show_terms(ctx, chat_id)
        return
    product = await ctx.db.select_one("products", {"sku": f"eq.{sku}", "select": "*"})
    if not product or not product["active"]:
        await ctx.tg.answer_callback(callback["id"], "This product is unavailable")
        return
    if product["source"] == "stock":
        stock = await ctx.db.select(
            "stock", {"sku": f"eq.{sku}", "state": "eq.available", "select": "id"}, limit=1
        )
        if not stock:
            await ctx.tg.answer_callback(callback["id"], "Sold out - check back soon")
            return
    order_id = "ord_" + uuid.uuid4().hex[:24]
    await ctx.db.insert(
        "orders",
        {
            "id": order_id,
            "user_id": user_id,
            "sku": sku,
            "title": product["title"],
            "price_stars": product["price_stars"],
            "terms_version": ctx.terms_version,
            "state": "invoice",
        },
    )
    await ctx.tg.send_invoice(
        chat_id,
        product["title"],
        product["description"] or "One digital item. Delivery after confirmed payment.",
        order_id,
        product["price_stars"],
    )
    await ctx.tg.answer_callback(callback["id"])


async def handle_pre_checkout(ctx, query):
    query_id = query["id"]
    order_id = query.get("invoice_payload", "")
    user_id = query["from"]["id"]
    order = await ctx.db.select_one("orders", {"id": f"eq.{order_id}", "select": "*"})
    if not order or order["user_id"] != user_id:
        await ctx.tg.answer_pre_checkout(query_id, False, "Order not found. Please start again.")
        return
    if order["state"] not in ("invoice", "checkout"):
        await ctx.tg.answer_pre_checkout(query_id, False, "This order is no longer payable.")
        return
    if order["terms_version"] != ctx.terms_version:
        await ctx.tg.answer_pre_checkout(query_id, False, "Please re-accept the current terms.")
        return
    product = await ctx.db.select_one(
        "products", {"sku": f"eq.{order['sku']}", "select": "source"}
    )
    if product and product["source"] == "stock":
        stock = await ctx.db.select(
            "stock", {"sku": f"eq.{order['sku']}", "state": "eq.available", "select": "id"}, limit=1
        )
        if not stock:
            await ctx.tg.answer_pre_checkout(query_id, False, "Just sold out, sorry!")
            return
    await ctx.tg.answer_pre_checkout(query_id, True)


async def deliver_code(ctx, order_id, chat_id, user_id):
    """Fetch the sold code for an order and send it. Safe to repeat."""
    item = await ctx.db.select_one(
        "stock", {"order_id": f"eq.{order_id}", "select": "ciphertext"}
    )
    if not item:
        return
    code = await ctx.fernet.decrypt(item["ciphertext"])
    await ctx.tg.send_message(
        chat_id,
        f"Your purchase is here!\n\nOrder: {order_id}\n\n{code}\n\n"
        f"Keep this message private. Need help? /paysupport",
    )


async def handle_payment(ctx, message):
    payment = message["successful_payment"]
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    order_id = payment.get("invoice_payload", "")
    charge_id = payment.get("telegram_payment_charge_id", "")
    amount = payment.get("total_amount", 0)
    if payment.get("currency") != "XTR" or not order_id or not charge_id:
        return
    result = await ctx.db.rpc(
        "fulfill_order",
        {
            "p_order_id": order_id,
            "p_charge_id": charge_id,
            "p_user_id": user_id,
            "p_amount": amount,
        },
    )
    if not result or not result.get("ok"):
        reason = (result or {}).get("reason", "error")
        if reason == "out_of_stock":
            await ctx.tg.send_message(
                chat_id,
                f"Payment received, but this item just ran out of stock ({order_id}). "
                "You are entitled to a full Stars refund - contact /paysupport.",
            )
            await ctx.notify_admins(f"OUT OF STOCK after payment: {order_id} - refund needed.")
        return
    if result.get("duplicate"):
        await deliver_code(ctx, order_id, chat_id, user_id)  # retry-safe resend
        return
    if result.get("supplier"):
        await ctx.tg.send_message(
            chat_id,
            f"Payment confirmed for {result.get('title', 'your item')} ({order_id}).\n"
            "The seller is fulfilling your order now - track it in /orders.",
        )
        await ctx.notify_admins(f"Supplier order to fulfill: {order_id}")
        return
    await deliver_code(ctx, order_id, chat_id, user_id)


async def handle_refund(ctx, message):
    refund = message["refunded_payment"]
    user_id = message["from"]["id"]
    charge_id = refund.get("telegram_payment_charge_id", "")
    amount = refund.get("total_amount", 0)
    if not charge_id:
        return
    await ctx.db.rpc(
        "record_refund",
        {"p_charge_id": charge_id, "p_user_id": user_id, "p_amount": amount},
    )
    await ctx.tg.send_message(
        message["chat"]["id"],
        "Your refund is recorded. The delivered code has been revoked and will not be resold.",
    )


async def show_orders(ctx, chat_id, user_id):
    orders = await ctx.db.select(
        "orders",
        {
            "user_id": f"eq.{user_id}",
            "select": "id,title,price_stars,state,created_at",
            "order": "created_at.desc",
        },
        limit=10,
    )
    if not orders:
        await ctx.tg.send_message(chat_id, "No orders yet. Browse the shop with /shop.",
                                  keyboard=back_rows())
        return
    lines = ["Your recent orders:"]
    for o in orders:
        state = ORDER_STATES.get(o["state"], o["state"])
        lines.append(f"\n{o['id']} - {o['title']} ({o['price_stars']} Stars) - {state}")
    lines.append("\nFor orders waiting on fulfillment, use /paysupport.")
    await ctx.tg.send_message(chat_id, "\n".join(lines), keyboard=back_rows())


async def handle_message(ctx, message):
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    if message.get("successful_payment"):
        await handle_payment(ctx, message)
        return
    if message.get("refunded_payment"):
        await handle_refund(ctx, message)
        return
    if (not user_id or message.get("chat", {}).get("type") != "private"
            or chat_id != user_id):
        return
    text = (message.get("text") or "").strip()
    command = text.split("@")[0].split()[0] if text else ""
    if command in ("/start", "/menu"):
        await show_home(ctx, chat_id)
    elif command == "/shop":
        await show_catalog(ctx, chat_id)
    elif command in ("/profile", "/offers", "/payments", "/referrals", "/api"):
        await show_information(ctx, chat_id, user_id, command[1:])
    elif command == "/terms":
        await show_terms(ctx, chat_id)
    elif command == "/privacy":
        await ctx.tg.send_message(chat_id, ctx.privacy)
    elif command == "/orders":
        await show_orders(ctx, chat_id, user_id)
    elif command in ("/paysupport", "/support"):
        await show_support(ctx, chat_id)
    elif command == "/whoami":
        await ctx.tg.send_message(chat_id, f"Your Telegram user ID: {user_id}")
    elif text:
        await ctx.tg.send_message(chat_id, "Browse products with /shop - help via /paysupport.")


async def handle_callback(ctx, callback):
    data = callback.get("data") or ""
    user_id = callback["from"]["id"]
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "private" or chat_id != user_id:
        await ctx.tg.answer_callback(callback["id"], "Open the shop in your private chat")
        return
    if data in {"home", "cat:0", "orders", "support", "profile", "offers",
                "payments", "referrals", "api"}:
        await ctx.tg.answer_callback(callback["id"])
        if data == "home":
            await show_home(ctx, chat_id)
        elif data == "cat:0":
            await show_catalog(ctx, chat_id)
        elif data == "orders":
            await show_orders(ctx, chat_id, user_id)
        elif data == "support":
            await show_support(ctx, chat_id)
        else:
            await show_information(ctx, chat_id, user_id, data)
    elif data.startswith("accept:"):
        if data[7:] != ctx.terms_version:
            await ctx.tg.answer_callback(callback["id"], "Terms changed - please review again")
            await show_terms(ctx, chat_id)
            return
        await ctx.db.upsert(
            "terms_acceptances",
            {"user_id": user_id, "version": ctx.terms_version},
            on_conflict="user_id,version",
        )
        await ctx.tg.answer_callback(callback["id"], "Terms accepted")
        await show_catalog(ctx, chat_id)
    elif data.startswith("buy:"):
        await start_checkout(ctx, callback, user_id, chat_id)
    elif data == "terms":
        await ctx.tg.answer_callback(callback["id"])
        await show_terms(ctx, chat_id)
    elif data == "privacy":
        await ctx.tg.answer_callback(callback["id"])
        await ctx.tg.send_message(chat_id, ctx.privacy)
    else:
        await ctx.tg.answer_callback(callback["id"])


async def handle_update(update, env):
    ctx = Ctx(env)
    update_id = update.get("update_id")
    if update_id is None:
        return
    kind = next((k for k in ("message", "callback_query", "pre_checkout_query") if k in update), "?")
    seen = await ctx.db.select_one(
        "update_inbox", {"update_id": f"eq.{update_id}", "select": "update_id"}
    )
    if seen:
        return
    if "message" in update:
        await handle_message(ctx, update["message"])
    elif "callback_query" in update:
        await handle_callback(ctx, update["callback_query"])
    elif "pre_checkout_query" in update:
        await handle_pre_checkout(ctx, update["pre_checkout_query"])
    # Claim only after successful processing: a crash before this point lets
    # Telegram's retry reprocess the update (every step above is idempotent).
    await ctx.db.rpc("claim_update", {"p_update_id": update_id, "p_kind": kind})
