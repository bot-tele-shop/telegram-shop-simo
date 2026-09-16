"""Canboso Buyer API 2.1.0, from its public OpenAPI contract.

No guessed auth headers, order-status routes, provider refunds or sandbox URLs.
GET keys belong in query parameters as documented, so never log request URLs.
POST uses a persisted exact body and Idempotency-Key; this client NEVER retries it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import aiohttp

from .config import CanbosoSettings

BASE_URL = "https://canboso.com"
PRODUCTS_PATH = "/api/v2/telegram-buyer/products"
BALANCE_PATH = "/api/v2/telegram-buyer/balance"
PURCHASE_PATH = "/api/v2/telegram-buyer/purchase"


class CanbosoError(ValueError):
    """Contains only a safe code, never URLs, credentials or remote messages."""
    def __init__(self, code: str, *, retry_after: int = 0) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


class PurchaseUncertain(CanbosoError):
    """The supplier may have debited funds. Do not buy again automatically."""

    def __init__(self, code: str, *, raw: Any = None, retry_after: int = 0) -> None:
        super().__init__(code, retry_after=retry_after)
        # An ordinary attribute keeps sensitive evidence out of str/repr/args.
        # The worker must encrypt it, never log it.
        self.raw = raw


class PurchaseRejected(CanbosoError):
    """The documented API explicitly rejected the purchase."""


class RateLimited(CanbosoError):
    pass


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    raw: Any = field(default=None, repr=False)

    @property
    def review_raw(self) -> Any:
        return self.raw if self.raw is not None else self.body


@dataclass(frozen=True)
class PurchaseResult:
    reference: str
    status: str  # completed or pending; never inferred from success alone
    amount: Decimal
    currency: str
    payload: str = field(repr=False)
    raw: dict[str, Any] = field(repr=False)


class Transport(Protocol):
    async def request(self, method: str, path: str, *, query: dict | None = None,
                      body: dict | None = None, headers: dict | None = None) -> Reply: ...


def money(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CanbosoError("invalid_amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (positive and amount == 0):
            raise InvalidOperation
    except (ValueError, InvalidOperation):
        raise CanbosoError("invalid_amount") from None
    return amount


def valid_email(value: str) -> str:
    if not isinstance(value, str):
        raise CanbosoError("invalid_customer_email")
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise CanbosoError("invalid_customer_email")
    return value


def response_evidence(raw: bytes | bytearray, status: int, *, truncated: bool = False) -> dict:
    # JSON-serializable and lossless, including invalid encodings and partial bodies.
    return {"status": status, "bodyBase64": base64.b64encode(raw).decode("ascii"),
            "truncated": truncated}


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def invalid_json_constant(value: str) -> None:
    raise ValueError("invalid_json_number")


def number(value: Any) -> Decimal:
    # Remote JSON numbers are not configuration strings; bool is not a number.
    if type(value) not in (int, float):
        raise CanbosoError("invalid_numeric_field")
    return money(value)


def text(value: Any, code: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CanbosoError(code)
    return value


class HttpTransport:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def request(self, method: str, path: str, *, query=None, body=None, headers=None) -> Reply:
        if (method, path) not in {("GET", PRODUCTS_PATH), ("GET", BALANCE_PATH), ("POST", PURCHASE_PATH)}:
            raise CanbosoError("unsupported_endpoint")
        async with self.lock:
            # Less than 28 requests/minute across all endpoints for this process.
            delay = self.last_request + 2.2 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request = time.monotonic()
            raw = bytearray()
            status = None
            try:
                async with self.session.request(
                    method, BASE_URL + path, params=query, json=body, headers=headers,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    status = response.status
                    async for chunk in response.content.iter_chunked(16384):
                        remaining = 2_000_000 - len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            evidence = response_evidence(raw, status, truncated=True)
                            if method == "POST":
                                raise PurchaseUncertain("oversized_supplier_response", raw=evidence)
                            raise CanbosoError("oversized_supplier_response")
                    try:
                        result = json.loads(raw, object_pairs_hook=unique_object,
                                            parse_constant=invalid_json_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        return Reply(status, None, dict(response.headers),
                                     response_evidence(raw, status))
                    evidence = None if isinstance(result, dict) else response_evidence(raw, status)
                    return Reply(status, result, dict(response.headers), evidence)
            except CanbosoError:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError):
                # Network exceptions often include credential-bearing GET URLs.
                if method == "POST":
                    evidence = response_evidence(raw, status, truncated=True) if status is not None else None
                    raise PurchaseUncertain("supplier_transport_failure", raw=evidence) from None
                raise CanbosoError("supplier_transport_failure") from None


class CanbosoClient:
    def __init__(self, settings: CanbosoSettings, transport: Transport, environment: str):
        self.settings = settings
        self.transport = transport
        self.environment = environment
        self._products: dict[str, dict] = {}

    def check_enabled(self) -> None:
        if not self.settings.enabled or not self.settings.api_key:
            raise CanbosoError("canboso_disabled_or_key_missing")

    @staticmethod
    def check_response(reply: Reply, *, purchase: bool = False) -> dict:
        body = reply.body if isinstance(reply.body, dict) else {}
        if reply.status == 429:
            headers = {k.lower(): v for k, v in reply.headers.items()}
            limits = body.get("rateLimit")
            body_delay = limits.get("retryAfter") if isinstance(limits, dict) else None
            delays = []
            for value in (headers.get("retry-after"), body_delay):
                if type(value) is int and value > 0:
                    delays.append(value)
                elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                    try:
                        delay = int(value)
                    except ValueError:
                        continue
                    if delay > 0:
                        delays.append(delay)
            raise RateLimited("supplier_rate_limited", retry_after=max(delays, default=60))
        if reply.status in {400, 401, 404} and body.get("success") is False:
            error = PurchaseRejected if purchase else CanbosoError
            raise error(f"supplier_rejected_{reply.status}")
        # 409 can mean a purchase is in progress or an idempotency conflict.
        # Neither it nor a 5xx is proof that no debit occurred.
        if reply.status != 200 or body.get("success") is not True:
            code = f"supplier_unconfirmed_{reply.status}"
            if purchase:
                raise PurchaseUncertain(code, raw=reply.review_raw)
            raise CanbosoError(code)
        return body

    async def products(self) -> dict:
        self.check_enabled()
        reply = await self.transport.request("GET", PRODUCTS_PATH, query={"key": self.settings.api_key})
        result = self.check_response(reply)
        if not isinstance(result.get("products"), list):
            raise CanbosoError("invalid_products_response")
        if "walletCurrency" in result and result["walletCurrency"] not in ("VND", "USD"):
            raise CanbosoError("unsupported_wallet_currency")
        products = {}
        for p in result["products"]:
            validate_product(p)
            if p["productId"] in products:
                raise CanbosoError("duplicate_product_id")
            products[p["productId"]] = p
        self._products = products
        return result

    async def balance(self) -> dict:
        self.check_enabled()
        reply = await self.transport.request("GET", BALANCE_PATH, query={"key": self.settings.api_key})
        result = self.check_response(reply)
        number(result.get("balance"))
        if result.get("walletCurrency") not in ("VND", "USD"):
            raise CanbosoError("unsupported_wallet_currency")
        for name in ("balanceVnd", "balanceUsd", "usdRate", "usdtBalance"):
            if name in result and not (result[name] is None and name != "usdtBalance"):
                number(result[name])
        if "balanceText" in result:
            text(result["balanceText"], "invalid_balance_text")
        return result

    async def purchase(self, exact_body: dict, idempotency_key: str) -> PurchaseResult:
        self.check_enabled()
        if not self.settings.allow_purchases or self.settings.problems(self.environment):
            raise CanbosoError("live_supplier_spending_locked")
        if not isinstance(exact_body, dict):
            raise CanbosoError("invalid_purchase_request")
        if exact_body.get("key") != self.settings.api_key:
            raise CanbosoError("buyer_key_changed_do_not_rebuild_request")
        if (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)):
            raise CanbosoError("invalid_idempotency_key")
        if set(exact_body) - {"key", "product_id", "quantity", "customer_email", "slot_months"}:
            raise CanbosoError("unsupported_purchase_fields")
        product_id = text(exact_body.get("product_id"), "invalid_product_id", nonempty=True)
        quantity = exact_body.get("quantity", 1)
        # The API supports bulk accounts, but the shop only creates one-item orders.
        if type(quantity) is not int or quantity != 1:
            raise CanbosoError("only_single_item_orders_supported")
        if "customer_email" in exact_body:
            if valid_email(exact_body["customer_email"]) != exact_body["customer_email"]:
                raise CanbosoError("invalid_customer_email")
        product = self._products.get(product_id, {})
        requirements = product.get("purchaseRequirements", {})
        if product and product["productType"] not in ("account", "slot"):
            raise CanbosoError("unsupported_product_type")
        if (product_id == "slot_chatgpt_business" or product.get("productType") == "slot"
                or requirements.get("customerEmail") is True):
            valid_email(exact_body.get("customer_email"))
        if requirements.get("quantityFixed", 1) != quantity:
            raise CanbosoError("unsupported_purchase_quantity")
        if product_id == "slot_chatgpt_business":
            months = exact_body.get("slot_months")
            if (type(months) is not int or months not in (1, 3, 6, 12)
                    or months not in requirements.get("allowedMonths", (1, 3, 6, 12))):
                raise CanbosoError("invalid_slot_months")
        elif "slot_months" in exact_body:
            raise CanbosoError("months_only_allowed_for_business_slot")
        try:
            reply = await self.transport.request("POST", PURCHASE_PATH, body=exact_body,
                                                 headers={"Idempotency-Key": idempotency_key})
        except PurchaseUncertain:
            raise
        except (CanbosoError, aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise PurchaseUncertain("purchase_transport_uncertain", raw=getattr(exc, "raw", None)) from None
        result = self.check_response(reply, purchase=True)
        try:
            parsed = parse_purchase(result, exact_body)
            if product and result["order"]["productType"] != product["productType"]:
                raise CanbosoError("supplier_product_type_mismatch")
            return parsed
        except (CanbosoError, TypeError, KeyError, ValueError):
            raise PurchaseUncertain("purchase_response_needs_review", raw=reply.review_raw) from None


def validate_product(product: Any) -> None:
    if not isinstance(product, dict):
        raise CanbosoError("invalid_product")
    text(product.get("productId"), "invalid_product_id", nonempty=True)
    text(product.get("name"), "invalid_product_name", nonempty=True)
    if product.get("productType") not in ("account", "slot", "upgrade_account"):
        raise CanbosoError("unsupported_product_type")
    for name in ("description", "image", "emoji"):
        if name in product:
            text(product[name], "invalid_product_field")
    price = product.get("price")
    if not isinstance(price, dict):
        raise CanbosoError("invalid_product_price")
    number(price.get("amount"))
    text(price.get("text"), "invalid_product_price")
    if price.get("currency") not in ("VND", "USD"):
        raise CanbosoError("unsupported_wallet_currency")
    availability = product.get("availability")
    if not isinstance(availability, dict):
        raise CanbosoError("invalid_product_availability")
    for name in ("available", "sold"):
        if name in availability and not (name == "available" and availability[name] is None):
            number(availability[name])
    promotions = product.get("promotions")
    if not isinstance(promotions, list):
        raise CanbosoError("invalid_product_promotions")
    for promotion in promotions:
        if not isinstance(promotion, dict):
            raise CanbosoError("invalid_product_promotion")
        if "type" in promotion:
            text(promotion["type"], "invalid_product_promotion")
        for name in ("minQty", "percent", "bonusQty"):
            if name in promotion:
                number(promotion[name])
    if "purchaseRequirements" in product:
        requirements = product["purchaseRequirements"]
        if not isinstance(requirements, dict):
            raise CanbosoError("invalid_purchase_requirements")
        for name in ("customerEmail", "slotMonths"):
            if name in requirements and type(requirements[name]) is not bool:
                raise CanbosoError("invalid_purchase_requirements")
        if "quantityFixed" in requirements:
            quantity = requirements["quantityFixed"]
            if type(quantity) is not int or quantity < 1:
                raise CanbosoError("invalid_purchase_requirements")
        if "allowedMonths" in requirements:
            months = requirements["allowedMonths"]
            if not isinstance(months, list) or any(type(m) is not int or m < 1 for m in months):
                raise CanbosoError("invalid_purchase_requirements")


def parse_purchase(body: dict, request: dict) -> PurchaseResult:
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CanbosoError("missing_purchase_result")
    text(body.get("lang"), "invalid_purchase_language")
    order, payment = body.get("order"), body.get("payment")
    if not isinstance(order, dict) or not isinstance(payment, dict):
        raise CanbosoError("missing_purchase_result")
    reference = text(order.get("orderCode"), "invalid_supplier_order_reference", nonempty=True)
    if len(reference) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in reference):
        raise CanbosoError("invalid_supplier_order_reference")
    text(order.get("productName"), "invalid_supplier_product_name", nonempty=True)
    text(order.get("status"), "invalid_supplier_order_status")
    if "productId" in order and order["productId"] != request["product_id"]:
        raise CanbosoError("supplier_order_mismatch")
    quantity, bonus, final_quantity = (order.get(n) for n in ("quantity", "bonusQuantity", "finalQuantity"))
    if type(quantity) is not int or quantity != 1:
        raise CanbosoError("supplier_order_mismatch")
    if (type(bonus) is not int or bonus < 0 or type(final_quantity) is not int
            or not 1 <= final_quantity <= 100 or final_quantity != quantity + bonus):
        raise CanbosoError("incomplete_supplier_delivery")
    if "customerEmail" in order:
        valid_email(order["customerEmail"])
        if order["customerEmail"] != request.get("customer_email"):
            raise CanbosoError("supplier_email_mismatch")
    if "slot_months" in request or "slotMonths" in order:
        if type(order.get("slotMonths")) is not int or order["slotMonths"] != request.get("slot_months"):
            raise CanbosoError("supplier_duration_mismatch")
    if "autoCompleted" in order and type(order["autoCompleted"]) is not bool:
        raise CanbosoError("invalid_fulfillment_status")
    if "fulfillmentStatus" in order:
        text(order["fulfillmentStatus"], "invalid_fulfillment_status")
    amount = number(payment.get("amount"))
    for name in ("originalAmount", "discountPercent", "discountAmount", "balance"):
        number(payment.get(name))
    for name in ("amountText", "originalAmountText", "discountAmountText", "balanceText"):
        text(payment.get(name), "invalid_payment_field")
    currency = payment.get("currency")
    if currency not in ("VND", "USD"):
        raise CanbosoError("unexpected_supplier_currency")
    if "delivery" in body and not isinstance(body["delivery"], dict):
        raise CanbosoError("invalid_account_delivery")
    if request["product_id"] == "slot_chatgpt_business" and order.get("productType") != "slot":
        raise CanbosoError("supplier_product_type_mismatch")
    payload = ""
    state = "pending"
    if order.get("productType") == "account" and order["status"] == "completed":
        accounts = body.get("delivery", {}).get("accounts")
        if not isinstance(accounts, list) or len(accounts) != final_quantity:
            raise CanbosoError("incomplete_supplier_delivery")
        sections = []
        for n, account in enumerate(accounts, 1):
            if not isinstance(account, dict):
                raise CanbosoError("invalid_account_delivery")
            text(account.get("user"), "invalid_account_delivery", nonempty=True)
            text(account.get("password"), "invalid_account_delivery", nonempty=True)
            lines = [f"Item {n}"]
            for name, label in (("user", "Login"), ("password", "Password"), ("verifyEmail", "Verification email"),
                                ("expiryText", "Expiry"), ("otherInfo", "Notes")):
                value = account.get(name)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 50000:
                        raise CanbosoError("invalid_delivery_field")
                    lines.append(f"{label}: {value}")
            sections.append("\n".join(lines))
        payload, state = "\n\n".join(sections), "completed"
    elif order.get("productType") == "slot":
        email = valid_email(request.get("customer_email"))
        if bonus != 0 or final_quantity != 1:
            raise CanbosoError("supplier_order_mismatch")
        fulfillment = order.get("fulfillmentStatus")
        if order["status"] == "completed" and fulfillment in ("invited", "completed"):
            payload = (f"Supplier confirmation: {reference}\n"
                       f"The supplier reports {fulfillment} for {email}.\n"
                       "Check your email for the invitation. Contact /paysupport if it is missing.")
            state = "completed"
        elif order["status"] != "paid" or fulfillment != "waiting_seller":
            raise CanbosoError("unconfirmed_slot_fulfillment")
        # success=true, status=paid, waiting_seller is NOT a delivered item.
    else:
        raise CanbosoError("unsupported_or_unconfirmed_delivery_type")
    if len(payload.encode()) > 1_000_000:
        raise CanbosoError("delivery_too_large")
    return PurchaseResult(reference, state, amount, currency, payload, body)
