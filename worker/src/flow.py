"""Buyer flow for the Digital Shelf webhook Worker.

Mirrors shop/bot.py semantics: terms gate, Stars invoices, atomic fulfillment.

Durability model:
- Each update_id is claimed (with a lease) BEFORE processing; the claim is the
  ack decision. Processed updates are marked done; failures are recorded.
- Payment allocates stock and records the charge BEFORE any Telegram send, so
  a retry always resends the same code and never allocates another item.
- An order becomes 'delivered' only after Telegram confirms the send.
"""

import hashlib
import json
import uuid

from db import DB
from fernet import Fernet
from storefront import (
    back_rows,
    information_text,
    menu_rows,
    quick_menu_rows,
    welcome_text,
)
from telegram import Telegram

TERMS_COMMANDS = {"/terms", "/privacy"}
ORDER_STATES = {
    "invoice": "awaiting payment",
    "checkout": "payment in progress",
    "expired": "expired",
    "cancelled": "cancelled",
    "paid": "paid",
    "delivering": "being delivered",
    "delivered": "delivered",
    "delivery_failed": "delivery failed - the seller has been notified",
    "needs_refund": "refund due",
    "refund_pending": "refund in progress",
    "refunded": "refunded",
}
CATALOG_PAGE_SIZE = 8
ORDER_GROUPS = {
    "progress": {"invoice", "checkout", "paid", "delivering"},
    "completed": {"delivered", "refunded"},
    "attention": {"delivery_failed", "needs_refund", "refund_pending"},
}


class TransientError(RuntimeError):
    """Retryable infrastructure failure: the webhook answers 500 so Telegram
    redelivers the update."""


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

    async def checkout_paused(self):
        row = await self.db.select_one("metadata", {"key": "eq.checkout_paused", "select": "value"})
        return bool(row and row.get("value") == "true")

    async def load_settings(self):
        """The metadata table is the single source of truth for shop settings:
        dashboard edits take effect here and a deploy can never overwrite them.
        Environment values are only the fallback for keys never set."""
        try:
            rows = await self.db.select("metadata", {"select": "key,value"}, limit=100)
        except Exception:
            return
        values = {row["key"]: row["value"] for row in rows}
        if values.get("shop_name"):
            self.shop_name = values["shop_name"]
        if values.get("support_contact"):
            self.support = values["support_contact"]
        if values.get("terms_text"):
            self.terms = values["terms_text"]
        if values.get("privacy_text"):
            self.privacy = values["privacy_text"]


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
        keyboard=keyboard(
            [[("I accept these terms and privacy notice", f"accept:{ctx.terms_version}")]]
        ),
    )


def keyboard(rows):
    return [[{"text": text, "callback_data": data} for text, data in row] for row in rows]


def product_callback(sku):
    token = hashlib.sha256(str(sku).encode()).hexdigest()[:24]
    return f"buyh:{token}"


async def show_home(ctx, chat_id, *, persistent_menu=False):
    paused = await ctx.checkout_paused()
    if persistent_menu:
        await ctx.tg.send_message(
            chat_id,
            "Quick access is ready below.",
            reply_keyboard=quick_menu_rows(),
        )
    await ctx.tg.send_message(
        chat_id, welcome_text(ctx.shop_name, paused=paused), keyboard=menu_rows(), parse_mode="HTML"
    )


async def show_information(ctx, chat_id, user_id, page):
    await ctx.tg.send_message(
        chat_id,
        information_text(page, user_id),
        keyboard=[
            [
                {"text": "🛍 Products", "callback_data": "cat:0", "style": "success"},
                {"text": "📋 Orders", "callback_data": "orders", "style": "primary"},
            ],
            [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
        ],
    )


async def show_support(ctx, chat_id):
    await ctx.tg.send_message(
        chat_id,
        f"Purchase support: {ctx.support}\n\n"
        "Include your order ID from /orders, not your product key or bot credentials. "
        "The merchant handles fulfillment, disputes and refund requests, not Telegram support.",
        keyboard=back_rows(),
    )


async def show_catalog(ctx, chat_id, page=0):
    products = await ctx.db.rpc("catalog_with_stock", {})
    available = [
        p for p in products if p.get("source") == "stock" and int(p.get("available") or 0) > 0
    ]
    if not available:
        await ctx.tg.send_message(
            chat_id,
            f"🛍 {ctx.shop_name}\n\n"
            "No products are live right now. The owner is loading the catalog and stock. "
            "Please check again soon or contact Support.",
            keyboard=[
                [{"text": "🛟 Support", "callback_data": "support", "style": "success"}],
                [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
            ],
        )
        return

    page_count = (len(available) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE
    page = max(0, min(int(page), page_count - 1))
    visible = available[page * CATALOG_PAGE_SIZE : (page + 1) * CATALOG_PAGE_SIZE]
    rows = []
    for index, product in enumerate(visible):
        title = str(product.get("title") or "Product")[:36]
        price = int(product.get("price_stars") or 0)
        stock = int(product.get("available") or 0)
        rows.append(
            [
                {
                    "text": f"🔥 {title} • {price} Stars • {stock} left",
                    "callback_data": product_callback(product["sku"]),
                    "style": "success" if index % 2 == 0 else "primary",
                }
            ]
        )

    navigation = []
    if page > 0:
        navigation.append({"text": "⬅ Prev", "callback_data": f"cat:{page - 1}"})
    navigation.append({"text": f"{page + 1}/{page_count}", "callback_data": "noop"})
    if page + 1 < page_count:
        navigation.append({"text": "Next ➡", "callback_data": f"cat:{page + 1}"})
    rows.append(navigation)
    rows.extend(
        [
            [
                {"text": "📋 Orders", "callback_data": "orders", "style": "success"},
                {"text": "🛟 Support", "callback_data": "support", "style": "success"},
            ],
            [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
        ]
    )
    await ctx.tg.send_message(
        chat_id,
        f"🛍 {ctx.shop_name}\n\nAvailable products • Page {page + 1}/{page_count}\n"
        "Tap a product to continue to secure Telegram Stars checkout.",
        keyboard=rows,
    )


async def start_checkout(ctx, callback, user_id, chat_id):
    data = callback["data"]
    if data.startswith("buyh:"):
        token = data[5:]
        catalog = await ctx.db.rpc("catalog_with_stock", {})
        matches = [product for product in catalog if product_callback(product["sku"])[5:] == token]
        if len(matches) != 1:
            await ctx.tg.answer_callback(callback["id"], "This product is unavailable")
            return
        sku = matches[0]["sku"]
    else:
        # Compatibility for buttons sent before the short product token rollout.
        sku = data[4:]
    if await ctx.checkout_paused():
        await ctx.tg.answer_callback(
            callback["id"], "Checkout is temporarily paused while the shop is being updated"
        )
        return
    if not await has_accepted(ctx, user_id):
        await ctx.tg.answer_callback(callback["id"], "Please read and accept the terms first")
        await show_terms(ctx, chat_id)
        return
    product = await ctx.db.select_one("products", {"sku": f"eq.{sku}", "select": "*"})
    if not product or not product["active"]:
        await ctx.tg.answer_callback(callback["id"], "This product is unavailable")
        return
    if product["source"] == "supplier":
        # Reject BEFORE payment: supplier fulfillment is not deployed, so a
        # buyer must never enter an indefinite waiting state.
        await ctx.tg.answer_callback(callback["id"], "This item is temporarily unavailable")
        return
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


_PRE_CHECKOUT_MESSAGES = {
    "not_found": "Order not found. Please start again.",
    "not_payable": "This order is no longer payable.",
    "expired": "This invoice expired. Please start a new order.",
    "amount_mismatch": "The payment details changed. Please start again.",
    "terms": "Please re-accept the current terms.",
    "inactive": "This product is unavailable.",
    "supplier_unavailable": "This item is temporarily unavailable.",
    "out_of_stock": "Just sold out, sorry!",
}


async def handle_pre_checkout(ctx, query):
    query_id = query["id"]
    order_id = query.get("invoice_payload", "")
    user_id = query["from"]["id"]
    result = await ctx.db.rpc(
        "pre_checkout_validate",
        {
            "p_order_id": order_id,
            "p_user_id": user_id,
            "p_amount": query.get("total_amount", 0),
            "p_currency": query.get("currency", ""),
            "p_terms_version": ctx.terms_version,
        },
    )
    if result and result.get("ok"):
        await ctx.tg.answer_pre_checkout(query_id, True)
        return
    reason = (result or {}).get("reason", "not_found")
    await ctx.tg.answer_pre_checkout(
        query_id, False, _PRE_CHECKOUT_MESSAGES.get(reason, "Please start again.")
    )


async def _send_assigned_code(ctx, order_id, chat_id):
    """Send the code already assigned to an order, then confirm delivery.

    Safe to repeat: the stock row is bound to the order at payment time, so a
    retry always resends the SAME code and never allocates another item.
    Returns True once Telegram has confirmed the send.
    """
    item = await ctx.db.select_one("stock", {"order_id": f"eq.{order_id}", "select": "ciphertext"})
    if not item:
        await ctx.db.rpc(
            "record_delivery_failure",
            {"p_order_id": order_id, "p_error": "assigned stock missing"},
        )
        await ctx.notify_admins(f"DELIVERY FAILED: {order_id} - assigned stock missing")
        return False
    code = await ctx.fernet.decrypt(item["ciphertext"])
    try:
        await ctx.tg.send_message(
            chat_id,
            f"Your purchase is here!\n\nOrder: {order_id}\n\n{code}\n\n"
            f"Keep this message private. Need help? /paysupport",
        )
    except Exception as exc:
        # Failed or uncertain send: keep the assignment, record the failure,
        # and let the recovery path (Telegram retry or dashboard resend) retry
        # the SAME code. Never compensate by assigning fresh stock.
        await ctx.db.rpc(
            "record_delivery_failure",
            {"p_order_id": order_id, "p_error": f"{type(exc).__name__}: {exc}"},
        )
        await ctx.notify_admins(
            f"DELIVERY FAILED: {order_id} ({type(exc).__name__}) - resend from the dashboard"
        )
        return False
    await ctx.db.rpc("confirm_delivery", {"p_order_id": order_id})
    return True


async def handle_payment(ctx, message):
    payment = message["successful_payment"]
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    order_id = payment.get("invoice_payload", "")
    charge_id = payment.get("telegram_payment_charge_id", "")
    amount = payment.get("total_amount", 0)
    currency = payment.get("currency", "")
    if not order_id or not charge_id:
        return
    result = await ctx.db.rpc(
        "fulfill_order",
        {
            "p_order_id": order_id,
            "p_charge_id": charge_id,
            "p_user_id": user_id,
            "p_amount": amount,
            "p_currency": currency,
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
        elif reason == "supplier_unavailable":
            await ctx.tg.send_message(
                chat_id,
                f"Payment received for {order_id}, but this item cannot be fulfilled right now. "
                "A full Stars refund will be issued - contact /paysupport if needed.",
            )
            await ctx.notify_admins(
                f"SUPPLIER ITEM PAID but unavailable: {order_id} - refund needed."
            )
        else:
            # order_not_found / payment_mismatch / charge_conflict / bad_state:
            # the paid event is persisted in payment_events for review — never
            # silently discarded.
            await ctx.tg.send_message(
                chat_id,
                f"We received your payment ({order_id}) but it needs a manual review. "
                "Your Stars are safe - contact /paysupport.",
            )
            await ctx.notify_admins(
                f"PAID EVENT NEEDS REVIEW: order={order_id} reason={reason} user={user_id}"
            )
        return
    if result.get("duplicate"):
        # Replay of an already-bound charge: resend the SAME assigned code.
        await _send_assigned_code(ctx, order_id, chat_id)
        return
    await _send_assigned_code(ctx, order_id, chat_id)


async def handle_refund(ctx, message):
    refund = message["refunded_payment"]
    user_id = message["from"]["id"]
    charge_id = refund.get("telegram_payment_charge_id", "")
    amount = refund.get("total_amount", 0)
    if not charge_id:
        return
    result = await ctx.db.rpc(
        "record_refund",
        {"p_charge_id": charge_id, "p_user_id": user_id, "p_amount": amount},
    )
    if not result or not result.get("matched"):
        await ctx.notify_admins(
            f"UNMATCHED REFUND: charge={charge_id} user={user_id} amount={amount} - review needed"
        )
    await ctx.tg.send_message(
        message["chat"]["id"],
        "Your refund is recorded. The delivered code has been quarantined and will not be resold.",
    )


async def show_profile(ctx, chat_id, user):
    user_id = int(user["id"])
    orders = await ctx.db.select(
        "orders",
        {
            "user_id": f"eq.{user_id}",
            "select": "price_stars,state",
            "order": "created_at.desc",
        },
        limit=100,
    )
    completed = [order for order in orders if order["state"] == "delivered"]
    spent = sum(int(order.get("price_stars") or 0) for order in completed)
    username = user.get("username")
    username_line = f"@{username}" if username else "Not set"
    await ctx.tg.send_message(
        chat_id,
        "👤 Your Profile\n\n"
        f"User ID: {user_id}\n"
        f"Username: {username_line}\n"
        f"Completed purchases: {len(completed)}\n"
        f"Stars spent on delivered orders: {spent}\n\n"
        "Payment method: Telegram Stars",
        keyboard=[
            [
                {"text": "🛍 Products", "callback_data": "cat:0", "style": "success"},
                {"text": "📋 Orders", "callback_data": "orders", "style": "success"},
            ],
            [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
        ],
    )


async def show_orders(ctx, chat_id, user_id, group=None):
    orders = await ctx.db.select(
        "orders",
        {
            "user_id": f"eq.{user_id}",
            "select": "id,title,price_stars,state,created_at",
            "order": "created_at.desc",
        },
        limit=50,
    )
    if not orders:
        await ctx.tg.send_message(
            chat_id,
            "📦 My Orders\n\nNo orders yet. Open Products to make your first purchase.",
            keyboard=[
                [{"text": "🛍 Products", "callback_data": "cat:0", "style": "success"}],
                [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
            ],
        )
        return

    if group is None:
        counts = {
            name: sum(order["state"] in states for order in orders)
            for name, states in ORDER_GROUPS.items()
        }
        await ctx.tg.send_message(
            chat_id,
            "📦 My Orders\n\nChoose a status:",
            keyboard=[
                [
                    {
                        "text": f"🔄 In progress ({counts['progress']})",
                        "callback_data": "orders:progress",
                        "style": "success",
                    }
                ],
                [
                    {
                        "text": f"✅ Completed ({counts['completed']})",
                        "callback_data": "orders:completed",
                        "style": "success",
                    }
                ],
                [
                    {
                        "text": f"⚠ Needs attention ({counts['attention']})",
                        "callback_data": "orders:attention",
                        "style": "success",
                    }
                ],
                [
                    {
                        "text": f"📦 All orders ({len(orders)})",
                        "callback_data": "orders:all",
                        "style": "success",
                    }
                ],
                [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
            ],
        )
        return

    selected = (
        orders
        if group == "all"
        else [order for order in orders if order["state"] in ORDER_GROUPS.get(group, set())]
    )
    label = {
        "progress": "In progress",
        "completed": "Completed",
        "attention": "Needs attention",
        "all": "All orders",
    }.get(group, "Orders")
    if not selected:
        lines = [f"📦 {label}\n\nNo orders in this section."]
    else:
        lines = [f"📦 {label}\n"]
    for o in selected[:10]:
        state = ORDER_STATES.get(o["state"], o["state"])
        lines.append(f"\n{o['title']} • ⭐ {o['price_stars']}\n{o['id']} • {state}")
    if len(selected) > 10:
        lines.append(f"\nShowing the latest 10 of {len(selected)} orders.")
    lines.append("\nFor payment or delivery help, open Support.")
    await ctx.tg.send_message(
        chat_id,
        "\n".join(lines),
        keyboard=[
            [{"text": "⬅ Order filters", "callback_data": "orders", "style": "success"}],
            [{"text": "🏠 Main menu", "callback_data": "home", "style": "primary"}],
        ],
    )


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
    if not user_id or message.get("chat", {}).get("type") != "private" or chat_id != user_id:
        return
    text = (message.get("text") or "").strip()
    command = text.split("@")[0].split()[0] if text else ""
    if command in ("/start", "/menu") or text == "🏠 Home":
        await show_home(ctx, chat_id, persistent_menu=True)
    elif command == "/shop" or text == "🛍 Products":
        await show_catalog(ctx, chat_id, 0)
    elif command == "/profile":
        await show_profile(ctx, chat_id, user)
    elif command in ("/offers", "/payments", "/referrals", "/api"):
        await show_information(ctx, chat_id, user_id, command[1:])
    elif text == "⭐ Payments":
        await show_information(ctx, chat_id, user_id, "payments")
    elif text == "🔗 API":
        await show_information(ctx, chat_id, user_id, "api")
    elif command == "/terms":
        await show_terms(ctx, chat_id)
    elif command == "/privacy":
        await ctx.tg.send_message(chat_id, ctx.privacy)
    elif command == "/orders":
        await show_orders(ctx, chat_id, user_id)
    elif command in ("/paysupport", "/support") or text == "🛟 Support":
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
    if data == "noop":
        await ctx.tg.answer_callback(callback["id"])
    elif data == "home":
        await ctx.tg.answer_callback(callback["id"])
        await show_home(ctx, chat_id)
    elif data.startswith("cat:"):
        await ctx.tg.answer_callback(callback["id"])
        try:
            page = int(data[4:])
        except ValueError:
            page = 0
        await show_catalog(ctx, chat_id, page)
    elif data == "orders":
        await ctx.tg.answer_callback(callback["id"])
        await show_orders(ctx, chat_id, user_id)
    elif data.startswith("orders:"):
        await ctx.tg.answer_callback(callback["id"])
        await show_orders(ctx, chat_id, user_id, data.split(":", 1)[1])
    elif data == "support":
        await ctx.tg.answer_callback(callback["id"])
        await show_support(ctx, chat_id)
    elif data == "profile":
        await ctx.tg.answer_callback(callback["id"])
        await show_profile(ctx, chat_id, callback["from"])
    elif data in {"offers", "payments", "referrals", "api"}:
        await ctx.tg.answer_callback(callback["id"])
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
        await show_catalog(ctx, chat_id, 0)
    elif data.startswith(("buy:", "buyh:")):
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
    await ctx.load_settings()
    update_id = update.get("update_id")
    if update_id is None:
        return
    kind = next(
        (k for k in ("message", "callback_query", "pre_checkout_query") if k in update), "unknown"
    )

    # Durable claim BEFORE processing. The update is acknowledged only after
    # it is safely processed or its failure is durably stored for retries.
    # The runtime may hand us nested proxy objects; round-trip through JSON so
    # the stored payload is always plain serializable data.
    safe_payload = json.loads(json.dumps(update, default=str))
    claim = await ctx.db.rpc(
        "claim_update", {"p_update_id": update_id, "p_kind": kind, "p_payload": safe_payload}
    )
    if claim == "done":
        return
    if claim == "busy":
        raise TransientError(f"update {update_id} is already being processed")

    try:
        if "message" in update:
            await handle_message(ctx, update["message"])
        elif "callback_query" in update:
            await handle_callback(ctx, update["callback_query"])
        elif "pre_checkout_query" in update:
            await handle_pre_checkout(ctx, update["pre_checkout_query"])
    except Exception as exc:
        # Record the failure durably, then surface a retryable error so
        # Telegram redelivers. Every processing step is idempotent.
        try:
            await ctx.db.rpc(
                "finish_update",
                {
                    "p_update_id": update_id,
                    "p_ok": False,
                    "p_error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            pass
        raise
    await ctx.db.rpc("finish_update", {"p_update_id": update_id, "p_ok": True})
