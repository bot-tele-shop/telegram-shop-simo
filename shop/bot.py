"""Telegram handlers. Every price and fulfillment decision is server-side."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from . import providers
from .canboso import CanbosoError, valid_email
from .config import Settings
from .delivery import DeliveryWorker, send_delivery
from .store import ShopError, Store
from .storefront import (
    back_rows,
    information_text,
    menu_rows,
    quick_menu_rows,
    welcome_text,
)

log = logging.getLogger(__name__)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


class EventGuard(BaseMiddleware):
    def __init__(self) -> None:
        self.actions: OrderedDict[int, deque[float]] = OrderedDict()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        critical = isinstance(event, Message) and (
            event.successful_payment or event.refunded_payment
        )
        if critical:
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        if not user or not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Open the shop in a private chat", show_alert=True)
            return None
        now = time.monotonic()
        bucket = self.actions.setdefault(user.id, deque())
        self.actions.move_to_end(user.id)
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        while len(self.actions) > 10000:
            self.actions.popitem(last=False)
        if len(bucket) >= 25:
            if isinstance(event, CallbackQuery):
                await event.answer("Please slow down and try again shortly", show_alert=True)
            return None
        bucket.append(now)
        try:
            return await handler(event, data)
        except ShopError as exc:
            if isinstance(event, CallbackQuery):
                await event.answer(str(exc)[:190], show_alert=True)
            else:
                await event.answer(str(exc), parse_mode=None)
            return None


def build_dispatcher(settings: Settings, store: Store, worker: DeliveryWorker) -> Dispatcher:
    store.supplier.configure(settings.canboso)
    store.supplier.configure_many(settings.other_suppliers.values())
    router = Router(name="shop")
    guard = EventGuard()
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)
    awaiting_stock: dict[int, str] = {}
    awaiting_slot: OrderedDict[int, tuple[str, float]] = OrderedDict()
    refund_lock = asyncio.Lock()

    def slot_details(saved: dict) -> str:
        months = saved.get("slot_months")
        duration = (
            f"{months} month{'s' if months != 1 else ''} (fixed)"
            if months is not None
            else "as described for this catalog slot (no month selection)"
        )
        email = f"Email: {saved['customer_email']}\n" if "customer_email" in saved else ""
        return f"{email}Duration: {duration}"

    def supplier_name(spec: dict | None) -> str:
        return providers.display((spec or {}).get("provider") or "canboso")

    def supplier_status(info: dict) -> str:
        states = {
            "draft": "awaiting checkout confirmation/payment",
            "queued": "queued for supplier fulfillment",
            "processing": "supplier request in progress; delivery pending",
            "pending": "waiting for seller; delivery pending",
            "retry_wait": "supplier retry waiting; delivery pending",
            "retry_approved": "seller-approved retry queued; delivery pending",
            "uncertain": "supplier outcome uncertain; waiting for seller review",
            "blocked": "supplier fulfillment blocked; waiting for seller",
            "failed": "supplier request failed; waiting for seller",
            "completed": "supplier fulfillment complete; check delivery status below",
            "resolved_for_refund": "seller review complete; awaiting Stars refund",
            "cancelled": "supplier request stopped locally",
        }
        state = info["state"]
        text = f"Supplier: {state.replace('_', ' ')} - {states.get(state, 'contact support')}"
        if info.get("supplier_reference"):
            text += f"\nSupplier reference: {info['supplier_reference'][:128]}"
        if info.get("hold_reason"):
            text += f"\nReview reason: {info['hold_reason'][:160]}"
        return text

    async def checkout_allowed(message: Message, user_id: int) -> bool:
        if not settings.enable_sales:
            raise ShopError("Checkout is paused while the merchant configures this shop")
        if not await asyncio.to_thread(store.has_accepted_terms, user_id, settings.terms_version):
            await show_terms(message)
            return False
        return True

    async def slot_confirmation(message: Message, order: dict, user_id: int) -> None:
        saved = await asyncio.to_thread(store.supplier.preview_input, order["id"], user_id)
        spec = await asyncio.to_thread(store.supplier.mapping, order["sku"])
        await message.answer(
            f"Confirm slot details\nOrder {order['id']}\n{order['title']}\n"
            f"Price: {order['price_stars']} Stars\n{slot_details(saved)}\n\n"
            f"This email will be shared with {supplier_name(spec)}, the supplier, to fulfill your slot. "
            "Confirm the saved email and duration before receiving an invoice. "
            "No payment or supplier purchase has been made. "
            "Use /shop to start again with a different email, or /orders to recover this draft.",
            reply_markup=keyboard([[("Confirm and get invoice", f"confirm:{order['id']}")]]),
            parse_mode=None,
            protect_content=True,
        )

    async def invoice(bot: Bot, message: Message, user_id: int, order: dict) -> None:
        info = await asyncio.to_thread(store.supplier.info, order["id"])
        description = (
            "TEST ENVIRONMENT. " if settings.environment == "test" else ""
        ) + "One digital item. Delivery after confirmed payment. For help use /paysupport."
        is_slot = info is not None and info["product_type"] == "slot"
        if is_slot:
            saved = await asyncio.to_thread(store.supplier.preview_input, order["id"], user_id)
            description += "\n" + slot_details(saved)
        try:
            await bot.send_invoice(
                chat_id=user_id,
                title=order["title"][:32],
                description=description,
                payload=order["id"],
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=order["title"][:32], amount=order["price_stars"])],
                start_parameter=f"order_{order['id']}",
                protect_content=True,
                request_timeout=20,
            )
        except (TelegramAPIError, OSError, asyncio.TimeoutError):
            # Slot consent is durable; an ambiguous send must not discard the saved input.
            if not is_slot:
                await asyncio.to_thread(store.cancel_invoice, order["id"])
            await message.answer(
                "The invoice could not be confirmed. Check /orders before retrying."
            )

    def require_admin(user_id: int) -> None:
        if user_id not in settings.admin_ids:
            raise ShopError("This action is available to shop administrators only")

    async def home(message: Message, *, persistent_menu: bool = False) -> None:
        if persistent_menu:
            await message.answer(
                "Quick access is ready below.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text=button["text"]) for button in row]
                        for row in quick_menu_rows()
                    ],
                    resize_keyboard=True,
                    is_persistent=True,
                    input_field_placeholder="Choose an option",
                ),
            )
        await message.answer(
            welcome_text(
                settings.shop_name,
                test_mode=settings.environment == "test",
                paused=not settings.enable_sales,
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=menu_rows()),
        )

    async def information(message: Message, page: str, user_id: int) -> None:
        rows = [[("🛍 Products", "cat:0"), ("📋 Orders", "orders")], [("🏠 Main menu", "home")]]
        await message.answer(
            information_text(page, user_id), parse_mode=None, reply_markup=keyboard(rows)
        )

    async def catalog(message: Message, page: int = 0) -> None:
        products = await asyncio.to_thread(store.list_products)
        page = max(0, min(page, max(0, (len(products) - 1) // 8)))
        page_products = products[page * 8 : (page + 1) * 8]
        specs = await asyncio.to_thread(
            store.supplier.mapping_many,
            [p["sku"] for p in page_products if p["source"] == "supplier"],
        )
        rows = []
        for product in page_products:
            availability = (
                "sold out" if not product["available"] else f"{product['price_stars']} Stars"
            )
            title = product["title"]
            if product["source"] == "supplier":
                availability = f"{product['price_stars']} Stars | supplier"
                spec = specs.get(product["sku"])
                if spec is None:
                    availability += " (not connected)"
                elif spec.get("slot_months") is not None:
                    title += f" | {spec['slot_months']} months"
            rows.append([(f"{title} | {availability}", f"p:{product['sku']}")])
        navigation = []
        if page:
            navigation.append(("Previous", f"cat:{page - 1}"))
        if (page + 1) * 8 < len(products):
            navigation.append(("Next", f"cat:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.extend(
            [
                [("My orders", "orders"), ("Support", "support")],
                [("Terms", "terms"), ("Privacy", "privacy")],
                [("🏠 Main menu", "home")],
            ]
        )
        mode = (
            "TEST ENVIRONMENT - sample items are not real products.\n\n"
            if settings.environment == "test"
            else ""
        )
        state = (
            ""
            if settings.enable_sales
            else "\nCheckout is paused while the shop is being configured."
        )
        await message.answer(
            f"{mode}{settings.shop_name}\n\nChoose a digital product. "
            "Prices are in Telegram Stars; delivery follows confirmed payment."
            f"{state}" + ("\nNo products have been added yet." if not products else ""),
            reply_markup=keyboard(rows),
            parse_mode=None,
        )

    async def show_terms(message: Message) -> None:
        await message.answer("Shop terms\n\n" + settings.terms_text, parse_mode=None)
        await message.answer(
            "Privacy notice\n\n" + settings.privacy_text,
            reply_markup=keyboard(
                [[("I accept these terms and privacy notice", f"accept:{settings.terms_version}")]]
            ),
            parse_mode=None,
        )

    async def show_orders(message: Message, user_id: int) -> None:
        await asyncio.to_thread(store.expire_orders)
        orders = await asyncio.to_thread(store.user_orders, user_id)
        if not orders:
            await message.answer(
                "You have no orders yet. Use /shop to browse.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=back_rows()),
            )
            return
        infos = await asyncio.to_thread(
            store.supplier.info_many, [order["id"] for order in orders]
        )
        rows = []
        for order in orders:
            status = order["state"].replace("_", " ")
            info = infos.get(order["id"])
            if info and order["state"] == "paid":
                status += f" / supplier {info['state'].replace('_', ' ')}"
                if info["state"] in {"pending", "uncertain", "blocked", "failed"}:
                    status += " / waiting for seller"
            rows.append([(f"{order['title']} | {status}", f"o:{order['id']}")])
        rows.append([("🏠 Main menu", "home")])
        await message.answer(
            "Your most recent orders\nOpen an order for details or to confirm a saved slot draft. "
            "For orders waiting for seller fulfillment, use /paysupport.",
            reply_markup=keyboard(rows),
        )

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        payment = message.successful_payment
        if message.from_user is None or payment is None:
            raise RuntimeError("Payment update lacks sender or payment")
        result = await asyncio.to_thread(
            store.record_payment,
            payment.invoice_payload,
            message.from_user.id,
            payment.currency,
            payment.total_amount,
            payment.telegram_payment_charge_id,
        )
        # Persist first. No network call can prevent recording the payment.
        if result.order_id and not result.duplicate:
            worker.kick()  # Start delivery/supplier fulfillment now, not on the 5s sweep.
        if result.status == "review" and not result.duplicate:
            asyncio.create_task(
                worker.notify_admins(
                    f"Payment needs manual review. Reference: {result.event_id}. Use /admin."
                )
            )
        elif result.order_id and not result.duplicate:
            order = await asyncio.to_thread(store.get_order, result.order_id)
            info = await asyncio.to_thread(store.supplier.info, result.order_id)
            if info and result.status == "accepted":
                try:
                    await message.answer(
                        f"Payment recorded. Order {result.order_id}\n{supplier_status(info)}\n"
                        "Payment confirmation is not delivery confirmation. "
                        "Track fulfillment in /orders; for help use /paysupport.",
                        parse_mode=None,
                        request_timeout=5,
                    )
                except (TelegramAPIError, OSError, asyncio.TimeoutError):
                    pass
            elif order["state"] == "needs_refund":
                asyncio.create_task(
                    worker.notify_admins(
                        f"Paid order has no stock: {result.order_id}. Restock/retry or refund via /admin."
                    )
                )
                try:
                    await message.answer(
                        f"Payment recorded. Order {result.order_id} needs merchant attention because "
                        "stock is unavailable. Use /paysupport for fulfillment or a refund.",
                        request_timeout=5,
                    )
                except TelegramAPIError:
                    pass

    @router.message(F.refunded_payment)
    async def refunded_payment(message: Message) -> None:
        refund = message.refunded_payment
        if message.from_user is None or refund is None:
            raise RuntimeError("Refund update lacks sender or payment")
        await asyncio.to_thread(
            store.record_refund,
            refund.telegram_payment_charge_id,
            message.from_user.id,
            refund.currency,
            refund.total_amount,
        )

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        try:
            if not settings.enable_sales:
                raise ShopError("Checkout is paused. No payment has been approved")
            await asyncio.wait_for(
                asyncio.to_thread(
                    store.approve_checkout,
                    query.invoice_payload,
                    query.from_user.id,
                    query.currency,
                    query.total_amount,
                    query.id,
                    settings.terms_version,
                ),
                timeout=3,
            )
        except ShopError as exc:
            await query.answer(ok=False, error_message=str(exc)[:200], request_timeout=5)
        except Exception:
            await query.answer(
                ok=False,
                error_message="Checkout is temporarily unavailable. Please retry.",
                request_timeout=5,
            )
        else:
            await query.answer(ok=True, request_timeout=5)

    @router.message(Command("start", "menu"))
    async def start(message: Message) -> None:
        await home(message, persistent_menu=True)

    @router.message(Command("shop"))
    async def shop(message: Message) -> None:
        await catalog(message)

    @router.message(Command("profile", "offers", "payments", "referrals", "api"))
    async def info_command(message: Message) -> None:
        page = (message.text or "").split()[0].split("@")[0][1:]
        await information(message, page, message.from_user.id)

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        await message.answer(f"Your Telegram user ID: {message.from_user.id}")

    @router.message(Command("terms"))
    async def terms(message: Message) -> None:
        await show_terms(message)

    @router.message(Command("privacy"))
    async def privacy(message: Message) -> None:
        await message.answer(settings.privacy_text, parse_mode=None)

    @router.message(Command("paysupport", "support"))
    async def support(message: Message) -> None:
        await message.answer(
            f"Purchase support: {settings.support_contact}\n\n"
            "Include your order ID from /orders, not your product key or bot credentials. "
            "The merchant handles fulfillment, disputes and refund requests, not Telegram support.",
            parse_mode=None,
        )

    @router.message(Command("orders"))
    async def orders(message: Message) -> None:
        await show_orders(message, message.from_user.id)

    @router.message(Command("admin", "stock"))
    async def admin(message: Message) -> None:
        require_admin(message.from_user.id)
        stats = await asyncio.to_thread(store.stats)
        products = await asyncio.to_thread(store.list_products, include_inactive=True)
        reviews = await asyncio.to_thread(store.review_payments)
        lines = [
            f"Admin - {settings.environment}",
            f"Accepted payments: {stats['accepted_stars']} Stars",
            f"Orders: {sum(stats['orders'].values())}",
            "",
            "Stock available:",
        ]
        lines += [f"{p['sku']}: {p['available']}" for p in products[:30]]
        lines += [
            "",
            "Restock: /addstock SKU then attach a UTF-8 .txt file (one item per line).",
            "Retry delivery: /retry ORDER_ID",
            "Supplier fulfillment review: /supplierreview (resolve through the local CLI)",
            "Request refund confirmation: /refund ORDER_ID",
            "Anomalous payment refund: /refundpayment PAYMENT_REFERENCE",
            "Cancel pending stock upload: /cancel",
            "",
            f"Payments needing attention: {len(reviews)}",
        ]
        rows = [
            [(f"Review {r['id'][:8]} | {r['amount']} {r['currency']}", f"rfask:{r['id']}")]
            for r in reviews
        ]
        await message.answer(
            "\n".join(lines), parse_mode=None, reply_markup=keyboard(rows) if rows else None
        )

    @router.message(Command("supplierreview"))
    async def supplier_review(message: Message) -> None:
        require_admin(message.from_user.id)
        reviews = await asyncio.to_thread(store.supplier.review)
        await message.answer(
            f"Supplier orders needing attention: {len(reviews)}\n\n"
            "Use the local CLI supplier-resolve workflow to record verified supplier evidence "
            "and manually authorize safe same-request retry, fulfillment, or stopping for refund. "
            "Do not blindly retry a pending or uncertain purchase. "
            "A Stars refund does not refund or cancel the supplier order.",
            parse_mode=None,
        )
        for review in reviews:
            await message.answer(
                f"Order {review['order_id']}\n{supplier_status(review)}",
                parse_mode=None,
                protect_content=True,
            )

    @router.message(Command("addstock"))
    async def addstock(message: Message) -> None:
        require_admin(message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            raise ShopError("Usage: /addstock SKU")
        product = await asyncio.to_thread(store.get_product, parts[1].strip())
        if product["source"] != "stock":
            raise ShopError("Supplier-backed stock cannot be imported here")
        awaiting_stock[message.from_user.id] = product["sku"]
        await message.answer(
            f"Attach a UTF-8 .txt file for {product['sku']}. One code or HTTPS download "
            "link per line, 1-500 lines, maximum 256 KiB. No secrets are echoed back."
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        awaiting_stock.pop(message.from_user.id, None)
        awaiting_slot.pop(message.from_user.id, None)
        await message.answer(
            "Pending upload or email entry cancelled. Saved drafts remain in /orders; "
            "existing paid orders are unchanged."
        )

    @router.message(F.document)
    async def stock_document(message: Message, bot: Bot) -> None:
        require_admin(message.from_user.id)
        sku = awaiting_stock.get(message.from_user.id)
        if sku is None:
            raise ShopError("Start a stock import with /addstock SKU first")
        document = message.document
        if not (document.file_name or "").lower().endswith(".txt"):
            raise ShopError("Only a UTF-8 .txt stock file is supported")
        if not document.file_size or document.file_size > 262144:
            raise ShopError("Stock file must be 1 byte to 256 KiB")
        buffer = io.BytesIO()
        await bot.download(document, destination=buffer, timeout=20)
        raw = buffer.getvalue()
        if len(raw) > 262144:
            raise ShopError("Stock file exceeds the size limit")
        try:
            lines = [line.strip() for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
        except UnicodeDecodeError as exc:
            raise ShopError("Save stock as UTF-8 text and try again") from exc
        added, skipped = await asyncio.to_thread(store.import_stock, sku, lines)
        awaiting_stock.pop(message.from_user.id, None)
        await message.answer(f"Imported {added} unique items; skipped {skipped} duplicates.")

    @router.message(Command("retry"))
    async def retry(message: Message) -> None:
        require_admin(message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            raise ShopError("Usage: /retry ORDER_ID")
        await asyncio.to_thread(store.retry_order, parts[1].strip())
        await message.answer("Delivery queued. The original item is reused if already allocated.")

    async def refund_prompt(message: Message, event_id: str) -> None:
        event = await asyncio.to_thread(store.get_payment, event_id)
        if event["status"] == "refunded":
            raise ShopError("Payment is already refunded")
        await message.answer(
            f"Confirm full refund?\nPayment: {event['id']}\nOrder: {event['order_id'] or 'unmatched'}\n"
            f"Customer ID: {event['user_id']}\nAmount: {event['amount']} {event['currency']}\n"
            f"Status: {event['status']}\nReason: {event['reason'] or 'merchant request'}\n\n"
            "This returns the payment through Telegram. Delivered codes cannot be recalled, "
            "and allocated stock will not be resold. A Stars refund does not refund or cancel "
            "the supplier order. Review supplier evidence through the local CLI supplier-resolve "
            "workflow before resolving pending or uncertain supplier purchases.",
            reply_markup=keyboard(
                [[("Confirm full refund", f"rf:{event_id}")], [("Keep payment - cancel", "rfno")]]
            ),
            parse_mode=None,
        )

    @router.message(Command("refund", "refundpayment"))
    async def refund_command(message: Message) -> None:
        require_admin(message.from_user.id)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            raise ShopError("Use /refund ORDER_ID or /refundpayment PAYMENT_REFERENCE")
        if parts[0].split("@")[0] == "/refund":
            event = await asyncio.to_thread(store.payment_for_order, parts[1].strip())
            event_id = event["id"]
        else:
            event_id = parts[1].strip()
        await refund_prompt(message, event_id)

    @router.callback_query()
    async def callback(call: CallbackQuery, bot: Bot) -> None:
        data = call.data or ""
        message = call.message
        user_id = call.from_user.id
        if data == "home":
            await call.answer()
            await home(message)
        elif data in {"profile", "offers", "payments", "referrals", "api"}:
            await call.answer()
            await information(message, data, user_id)
        elif data.startswith("cat:"):
            try:
                page = int(data[4:])
            except ValueError as exc:
                raise ShopError("Invalid catalog page") from exc
            await call.answer()
            await catalog(message, page)
        elif data in {"terms", "privacy", "support", "orders"}:
            await call.answer()
            if data == "terms":
                await show_terms(message)
            elif data == "privacy":
                await message.answer(settings.privacy_text, parse_mode=None)
            elif data == "support":
                await message.answer(
                    f"Merchant purchase support: {settings.support_contact}\n"
                    "Include your order ID, not your digital product key.",
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=back_rows()),
                )
            else:
                await show_orders(message, user_id)
        elif data.startswith("accept:"):
            if data[7:] != settings.terms_version:
                raise ShopError("These terms are outdated. Open /terms to read the current version")
            await asyncio.to_thread(store.accept_terms, user_id, settings.terms_version)
            await call.answer("Terms accepted")
            await catalog(message)
        elif data.startswith("p:"):
            product = await asyncio.to_thread(store.get_product, data[2:])
            if not product["active"]:
                raise ShopError("This product is no longer available")
            rows = [[("Back to shop", "cat:0")]]
            details = f"Available units: {product['available']}"
            if product["source"] == "supplier":
                spec = await asyncio.to_thread(store.supplier.mapping, product["sku"])
                details = (
                    f"Fulfilled by {supplier_name(spec)} after payment. "
                    "Supplier availability is checked locally."
                )
                if spec["product_type"] == "slot":
                    details += "\n" + slot_details(spec)
                    details += "\nAn email shared with the supplier and confirmation are required."
                rows.insert(
                    0,
                    [
                        (
                            f"Buy {spec['product_type']} for {product['price_stars']} Stars",
                            f"buy:{product['sku']}",
                        )
                    ],
                )
            elif product["available"]:
                rows.insert(
                    0, [(f"Buy for {product['price_stars']} Stars", f"buy:{product['sku']}")]
                )
            await call.answer()
            await message.answer(
                f"{product['title']}\n\n{product['description']}\n\n"
                f"Price: {product['price_stars']} Stars\n{details}",
                reply_markup=keyboard(rows),
                parse_mode=None,
            )
        elif data.startswith("buy:"):
            if not await checkout_allowed(message, user_id):
                await call.answer("Please read and accept the terms first")
                return
            product = await asyncio.to_thread(store.get_product, data[4:])
            if not product["active"]:
                raise ShopError("This product is no longer available")
            awaiting_slot.pop(user_id, None)
            if product["source"] == "supplier":
                spec = await asyncio.to_thread(store.supplier.mapping, product["sku"])
                await asyncio.to_thread(store.supplier.assert_enabled)
                if spec["product_type"] == "slot":
                    now = time.monotonic()
                    while awaiting_slot and next(iter(awaiting_slot.values()))[1] <= now:
                        awaiting_slot.popitem(last=False)
                    awaiting_slot[user_id] = (product["sku"], now + 600)
                    while len(awaiting_slot) > 10000:
                        awaiting_slot.popitem(last=False)
                    await call.answer()
                    await message.answer(
                        f"{product['title']}\nPrice: {product['price_stars']} Stars\n"
                        f"{slot_details(spec)}\n\n"
                        "Send the email address for your slot in this private chat. "
                        f"This email will be shared with {supplier_name(spec)}, the supplier, to fulfill the slot. "
                        "You will review the saved email and duration and confirm before an invoice "
                        "is sent. Email entry expires after 10 minutes. Use /cancel to stop.",
                        parse_mode=None,
                        protect_content=True,
                    )
                    return
            order = await asyncio.to_thread(
                store.create_order, user_id, data[4:], settings.terms_version
            )
            await call.answer()
            await invoice(bot, message, user_id, order)
        elif data.startswith("confirm:"):
            await asyncio.to_thread(store.expire_orders)
            order = await asyncio.to_thread(store.get_order, data[8:], user_id=user_id)
            if not await checkout_allowed(message, user_id):
                await call.answer("Please read and accept the terms first")
                return
            if order["terms_version"] != settings.terms_version:
                raise ShopError("The shop terms changed; start a new checkout from /shop")
            if order["state"] != "invoice":
                raise ShopError("This invoice expired or is already being processed. Check /orders")
            info = await asyncio.to_thread(store.supplier.info, order["id"])
            if not info or info["product_type"] != "slot":
                raise ShopError("This order does not require slot confirmation")
            await asyncio.to_thread(store.supplier.assert_enabled)
            await call.answer()
            await invoice(bot, message, user_id, order)
        elif data.startswith("o:"):
            await asyncio.to_thread(store.expire_orders)
            order = await asyncio.to_thread(store.get_order, data[2:], user_id=user_id)
            info = await asyncio.to_thread(store.supplier.info, order["id"])
            rows = [[("Back to orders", "orders")]]
            if order["state"] == "delivered":
                rows.insert(0, [("View my delivery", f"get:{order['id']}")])
            details = f"{supplier_status(info)}\n" if info else ""
            await call.answer()
            await message.answer(
                f"Order {order['id']}\n{order['title']}\n{order['price_stars']} Stars\n"
                f"{details}Status: {order['state'].replace('_', ' ')}\n\n"
                f"Need help? /paysupport\nSeller support: {settings.support_contact}",
                reply_markup=keyboard(rows),
                parse_mode=None,
            )
            if info and info["product_type"] == "slot" and order["state"] == "invoice":
                await slot_confirmation(message, order, user_id)
        elif data.startswith("get:"):
            payload = await asyncio.to_thread(store.retrieve_delivery, data[4:], user_id)
            await call.answer()
            await send_delivery(bot, user_id, data[4:], "", payload)
        elif data.startswith("rfask:"):
            require_admin(user_id)
            await call.answer()
            await refund_prompt(message, data[6:])
        elif data == "rfno":
            require_admin(user_id)
            await call.answer("Refund cancelled; payment unchanged")
            await message.edit_reply_markup(reply_markup=None)
        elif data.startswith("rf:"):
            require_admin(user_id)
            await call.answer("Processing refund request")
            async with refund_lock:
                event_id = data[3:]
                event = await asyncio.to_thread(store.begin_refund, event_id)
                try:
                    result = await bot.refund_star_payment(
                        user_id=event["user_id"],
                        telegram_payment_charge_id=event["charge_id"],
                        request_timeout=20,
                    )
                except TelegramAPIError:
                    await message.answer(
                        "Refund confirmation is uncertain or failed. Delivery remains "
                        "on hold. Review /admin and Telegram transaction records "
                        "before retrying; do not refund by another channel blindly."
                    )
                    return
                if result:
                    await asyncio.to_thread(store.finish_refund, event_id)
                    await message.edit_reply_markup(reply_markup=None)
                    await message.answer(f"Full refund confirmed. Payment reference: {event_id}")
                else:
                    await message.answer("Refund not confirmed; the payment remains under review.")
        else:
            await call.answer("This button is no longer available")

    @router.message()
    async def fallback(message: Message) -> None:
        user_id = message.from_user.id
        quick_action = (message.text or "").strip()
        if quick_action == "🛍 Products":
            await catalog(message)
            return
        if quick_action == "🛟 Support":
            await message.answer(
                f"Purchase support: {settings.support_contact}\n\n"
                "Include your order ID from /orders, not your product key or bot credentials.",
                parse_mode=None,
            )
            return
        if quick_action == "⭐ Payments":
            await information(message, "payments", user_id)
            return
        if quick_action == "🔗 API":
            await information(message, "api", user_id)
            return
        pending = awaiting_slot.get(user_id)
        if pending is not None and not (message.text or "").startswith("/"):
            sku, expires_at = pending
            if time.monotonic() >= expires_at:
                awaiting_slot.pop(user_id, None)
                raise ShopError("Email entry expired. Select your slot again from /shop")
            if not await checkout_allowed(message, user_id):
                return
            try:
                email = valid_email(message.text)
            except CanbosoError as exc:
                raise ShopError("Enter a valid email address, or use /cancel to stop") from exc
            order = await asyncio.to_thread(
                store.create_order, user_id, sku, settings.terms_version, customer_email=email
            )
            awaiting_slot.pop(user_id, None)
            await slot_confirmation(message, order, user_id)
            return
        await message.answer("Use /shop to browse, /orders for purchases, or /paysupport for help.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
