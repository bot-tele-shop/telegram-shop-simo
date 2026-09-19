"""Jaha Digital buyer API v1, from its published guide and OpenAPI contract.

Auth travels in the Authorization header only, never in URLs or logs.
POST /v1/orders sends the persisted exact body plus the intent's
Idempotency-Key header and always carries server-side max price protection
(max_unit_price_usdt). This client NEVER retries a purchase on its own;
the worker's no-implicit-replay rules apply unchanged.

USDT is USD-pegged, so amounts are normalized to USD at this boundary:
budgets, routing and FX stay single-currency per supplier.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from decimal import Decimal
from typing import Any

import aiohttp

from .canboso import (
    CanbosoError,
    PurchaseRejected,
    PurchaseResult,
    PurchaseUncertain,
    RateLimited,
    Reply,
    invalid_json_constant,
    money,
    response_evidence,
    text,
    unique_object,
    valid_email,
)
from .errors import ShopError

BASE_URL = "https://api.jahadigital.shop"
ACCOUNT_PATH = "/v1/account"
PRODUCTS_PATH = "/v1/products"
ORDERS_PATH = "/v1/orders"

# POST /v1/orders is idempotent by key: an operator-approved retry resends the
# exact same body and key and can never create a second order.
IDEMPOTENT_PURCHASES = True

ORDER_PATH_RE = re.compile(r"^/v1/orders/[A-Za-z0-9_-]{1,128}$")
PRODUCT_PATH_RE = re.compile(r"^/v1/products/[A-Za-z0-9_-]{1,128}$")

# Documented in the guide: 120 read requests/minute, 10 order creations/minute.
READ_INTERVAL = 0.55
ORDER_INTERVAL = 6.1


def validate_mapping_spec(specification: dict) -> None:
    """Jaha-specific mapping rules, called by shop.providers.validate_spec."""
    if specification.get("slot_months") is not None:
        raise ShopError("Jaha Digital has no duration variants; do not send slot_months")
    if specification.get("product_type") not in ("account", "slot"):
        raise ShopError("Jaha Digital products map to account (automatic) or "
                        "slot (email-required) internal types")


def build_purchase_body(settings, spec: dict, email: str | None,
                        *, order_id: str = "") -> dict:
    """The exact purchase body persisted with an intent; never rebuilt later.

    max_unit_price_usdt carries the SKU's approved preflight cap to the
    server, so a price jump rejects the order instead of debiting more.
    """
    body: dict[str, Any] = {
        "product_code": spec["product_id"],
        "quantity": 1,
        "max_unit_price_usdt": str(money(spec["max_cost"], positive=True)),
    }
    if order_id:
        # Identical to the intent's idempotency key, so order-history lookups
        # can recover an uncertain purchase by either value.
        body["external_order_id"] = f"ds-{order_id}"[:128]
    if spec["product_type"] == "slot":
        try:
            address = valid_email(email)
        except CanbosoError as exc:
            raise ShopError("Enter a valid customer email for this product") from exc
        body["buyer_input"] = {"items": [{"email": address}]}
    elif email:
        raise ShopError("Email is not required for this product")
    return body


class Transport:
    async def request(self, method: str, path: str, *, query: dict | None = None,
                      body: dict | None = None, headers: dict | None = None) -> Reply: ...


class HttpTransport:
    """Allowlisted Jaha endpoints only. The bearer key is a header, so request
    URLs are safe; remote problem details and network errors are still reduced
    to safe codes."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.lock = asyncio.Lock()
        self.last_read = 0.0
        self.last_order = 0.0

    async def request(self, method: str, path: str, *, query=None, body=None, headers=None) -> Reply:
        allowed = (method, path) in {("GET", ACCOUNT_PATH), ("GET", PRODUCTS_PATH),
                                     ("GET", ORDERS_PATH), ("POST", ORDERS_PATH)} or (
            method == "GET" and (ORDER_PATH_RE.match(path) or PRODUCT_PATH_RE.match(path)))
        if not allowed:
            raise CanbosoError("unsupported_endpoint")
        async with self.lock:
            now = time.monotonic()
            if method == "POST":
                delay = self.last_order + ORDER_INTERVAL - now
            else:
                delay = self.last_read + READ_INTERVAL - now
            if delay > 0:
                await asyncio.sleep(delay)
            if method == "POST":
                self.last_order = time.monotonic()
            else:
                self.last_read = time.monotonic()
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
                if method == "POST":
                    evidence = response_evidence(raw, status, truncated=True) if status is not None else None
                    raise PurchaseUncertain("supplier_transport_failure", raw=evidence) from None
                raise CanbosoError("supplier_transport_failure") from None


def _retry_after(reply: Reply) -> int:
    headers = {k.lower(): v for k, v in reply.headers.items()}
    value = headers.get("retry-after")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return max(1, int(value))
    return 60


def _problem_code(reply: Reply) -> str:
    body = reply.body if isinstance(reply.body, dict) else {}
    code = body.get("code")
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
        return code
    return f"http_{reply.status}"


class JahaClient:
    """products(), balance(), purchase() plus documented order lookup, all
    normalized to the shared internal snapshot/result shapes."""

    def __init__(self, settings, transport: Transport, environment: str):
        self.settings = settings
        self.transport = transport
        self.environment = environment
        self._products: dict[str, dict] = {}

    def check_enabled(self) -> None:
        if not self.settings.enabled or not self.settings.api_key:
            raise CanbosoError("jaha_digital_disabled_or_key_missing")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    async def products(self) -> dict:
        """Full current catalog, normalized to the internal snapshot shape."""
        self.check_enabled()
        products: list[dict] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(20):  # Hard page cap; a cursor loop is a supplier bug.
            query: dict[str, Any] = {"limit": 100, "current_only": "true"}
            if cursor:
                query["cursor"] = cursor
            reply = await self.transport.request("GET", PRODUCTS_PATH, query=query,
                                                 headers=self._auth())
            result = self.check_response(reply)
            page = result.get("products")
            if not isinstance(page, list):
                raise CanbosoError("invalid_products_response")
            for raw_product in page:
                products.append(self._normalize_product(raw_product, seen))
            cursor = result.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 3 <= len(cursor) <= 128:
                raise CanbosoError("invalid_products_cursor")
        else:
            raise CanbosoError("supplier_catalog_pagination_loop")
        return {"products": products, "walletCurrency": "USD"}

    def _normalize_product(self, product: Any, seen: set[str]) -> dict:
        if not isinstance(product, dict):
            raise CanbosoError("invalid_product")
        code = text(product.get("code"), "invalid_product_id", nonempty=True)
        if code in seen:
            raise CanbosoError("duplicate_product_id")
        seen.add(code)
        title = text(product.get("title"), "invalid_product_name", nonempty=True)
        variant = product.get("variant")
        name = f"{title} {variant}".strip() if isinstance(variant, str) else title
        amount = money(product.get("price_usdt"), positive=True)
        status = product.get("status")
        if status not in ("available", "out_of_stock"):
            raise CanbosoError("invalid_product_availability")
        available = product.get("available")
        if type(available) is not int or available < 0:
            raise CanbosoError("invalid_product_availability")
        if product.get("delivery_type") not in ("automatic", "manual"):
            raise CanbosoError("invalid_product_delivery_type")
        for numeric in ("min_quantity", "max_quantity"):
            if type(product.get(numeric)) is not int or product[numeric] < 1:
                raise CanbosoError("invalid_product_quantity_limit")
        requirements: dict[str, Any] = {"quantityFixed": 1}
        if product["min_quantity"] > 1:
            # The shop buys one unit per order; flag it so preflight blocks.
            requirements["quantityFixed"] = product["min_quantity"]
        buyer_input = product.get("buyer_input")
        if not isinstance(buyer_input, dict) or type(buyer_input.get("required")) is not bool:
            raise CanbosoError("invalid_product_buyer_input")
        product_type = "account"
        if buyer_input["required"]:
            if buyer_input.get("type") == "email" and buyer_input.get("scope") == "per_unit":
                # One email per unit maps onto the existing email-collection
                # (slot) flow: the bot gathers it before the invoice.
                product_type = "slot"
                requirements["customerEmail"] = True
            else:
                # email_password / per-order text input has no buyer flow yet.
                requirements["buyerInput"] = True
        return {
            "productId": code,
            "name": name,
            "productType": product_type,
            # USDT is USD-pegged; normalized at this boundary.
            "price": {"amount": amount, "currency": "USD", "text": f"USDT {amount}"},
            "availability": {"available": available if status == "available" else 0, "sold": 0},
            "promotions": [],
            "purchaseRequirements": requirements,
            "deliveryType": product["delivery_type"],
        }

    async def balance(self) -> dict:
        self.check_enabled()
        reply = await self.transport.request("GET", ACCOUNT_PATH, headers=self._auth())
        result = self.check_response(reply)
        account = result.get("account")
        if not isinstance(account, dict):
            raise CanbosoError("invalid_balance_response")
        if account.get("currency") != "USDT":
            raise CanbosoError("unsupported_wallet_currency")
        amount = money(account.get("balance_usdt"))
        return {"balance": amount, "walletCurrency": "USD",
                "balanceText": f"USDT {amount}",
                "accountStatus": text(account.get("status"), "invalid_account_status")}

    async def purchase(self, exact_body: dict, idempotency_key: str) -> PurchaseResult:
        self.check_enabled()
        if not self.settings.allow_purchases or self.settings.problems(self.environment):
            raise CanbosoError("live_supplier_spending_locked")
        if not isinstance(exact_body, dict):
            raise CanbosoError("invalid_purchase_request")
        if (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)):
            raise CanbosoError("invalid_idempotency_key")
        if set(exact_body) - {"product_code", "quantity", "max_unit_price_usdt",
                              "external_order_id", "buyer_input"}:
            raise CanbosoError("unsupported_purchase_fields")
        product_code = text(exact_body.get("product_code"), "invalid_product_id", nonempty=True)
        if exact_body.get("quantity") != 1:
            raise CanbosoError("only_single_item_orders_supported")
        money(exact_body.get("max_unit_price_usdt"), positive=True)
        if "external_order_id" in exact_body:
            text(exact_body["external_order_id"], "invalid_external_order_id", nonempty=True)
        product = self._products.get(product_code, {})
        requirements = product.get("purchaseRequirements", {})
        if product and requirements.get("quantityFixed", 1) != 1:
            raise CanbosoError("unsupported_purchase_quantity")
        if requirements.get("buyerInput"):
            raise CanbosoError("buyer_input_needs_manual_integration")
        if requirements.get("customerEmail"):
            items = (exact_body.get("buyer_input") or {}).get("items")
            if (not isinstance(items, list) or len(items) != 1
                    or not isinstance(items[0], dict)
                    or valid_email(items[0].get("email")) != items[0].get("email")):
                raise CanbosoError("invalid_customer_email")
        elif "buyer_input" in exact_body:
            raise CanbosoError("buyer_input_not_allowed")
        headers = {**self._auth(), "Idempotency-Key": idempotency_key}
        try:
            reply = await self.transport.request("POST", ORDERS_PATH, body=exact_body,
                                                 headers=headers)
        except PurchaseUncertain:
            raise
        except (CanbosoError, aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise PurchaseUncertain("purchase_transport_uncertain",
                                    raw=getattr(exc, "raw", None)) from None
        result = self.check_response(reply, purchase=True)
        try:
            return self._parse_order(result, exact_body)
        except (CanbosoError, TypeError, KeyError, ValueError):
            raise PurchaseUncertain("purchase_response_needs_review",
                                    raw=reply.review_raw) from None

    def check_response(self, reply: Reply, *, purchase: bool = False) -> dict:
        if reply.status == 429:
            raise RateLimited("supplier_rate_limited", retry_after=_retry_after(reply))
        code = _problem_code(reply)
        if purchase:
            if reply.status == 201:
                if not isinstance(reply.body, dict) or not isinstance(reply.body.get("order"), dict):
                    raise PurchaseUncertain("purchase_response_needs_review", raw=reply.review_raw)
                return reply.body
            # Input is validated before any debit; rejections never charged.
            if reply.status in (400, 401, 403, 404, 422):
                raise PurchaseRejected(f"supplier_rejected_{code}")
            if reply.status == 409:
                if code in ("insufficient_balance", "price_changed", "out_of_stock",
                            "product_unavailable"):
                    raise PurchaseRejected(f"supplier_rejected_{code}")
                # request_in_progress / idempotency conflicts mean the first
                # attempt may still debit; never assume either way.
                raise PurchaseUncertain(f"supplier_unconfirmed_{code}", raw=reply.review_raw)
            raise PurchaseUncertain(f"supplier_unconfirmed_{reply.status}",
                                    raw=reply.review_raw)
        if reply.status == 200 and isinstance(reply.body, dict):
            return reply.body
        if reply.status in (400, 401, 403, 404, 422):
            raise CanbosoError(f"supplier_rejected_{code}")
        raise CanbosoError(f"supplier_unconfirmed_{reply.status}")

    def _parse_order(self, body: dict, request: dict) -> PurchaseResult:
        order = body["order"]
        reference = text(order.get("order_number"), "invalid_supplier_order_reference",
                         nonempty=True)
        if len(reference) > 128:
            raise CanbosoError("invalid_supplier_order_reference")
        if order.get("product_code") != request["product_code"]:
            raise CanbosoError("supplier_order_mismatch")
        if order.get("quantity") != 1:
            raise CanbosoError("supplier_order_mismatch")
        external = order.get("external_order_id")
        if external is not None and external != request.get("external_order_id"):
            raise CanbosoError("supplier_order_mismatch")
        if "buyer_input" in request and order.get("buyer_input_received") is not True:
            raise CanbosoError("supplier_buyer_input_not_recorded")
        amount = money(order.get("total_usdt"))
        status = order.get("status")
        payload = ""
        state = "pending"
        if status == "completed":
            if order.get("delivered_quantity") != 1:
                raise CanbosoError("incomplete_supplier_delivery")
            delivery = text(order.get("delivery"), "invalid_delivery", nonempty=True)
            instructions = order.get("instructions")
            payload = delivery
            if isinstance(instructions, str) and instructions.strip():
                payload += f"\n\nInstructions: {instructions}"
            state = "completed"
        elif status in ("processing", "manual_review", "partially_completed"):
            state = "pending"  # Debited; fulfillment runs on the supplier side.
        else:
            raise CanbosoError("unconfirmed_supplier_order_status")
        if len(payload.encode()) > 1_000_000:
            raise CanbosoError("delivery_too_large")
        return PurchaseResult(reference, state, amount, "USD", payload, body,
                              product_type="account")

    async def lookup_order(self, order_number: str) -> PurchaseResult | None:
        """Documented GET /v1/orders/{order_number}; None when it does not exist."""
        self.check_enabled()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", order_number):
            raise CanbosoError("invalid_supplier_order_reference")
        reply = await self.transport.request("GET", f"{ORDERS_PATH}/{order_number}",
                                             headers=self._auth())
        if reply.status == 404:
            return None
        result = self.check_response(reply)
        request = {"product_code": result["order"].get("product_code"),
                   "external_order_id": result["order"].get("external_order_id")}
        try:
            return self._parse_order(result, request)
        except (CanbosoError, TypeError, KeyError, ValueError):
            return None  # Keep the intent uncertain for an operator.

    async def recover_uncertain(self, intent: dict) -> PurchaseResult | None:
        """Recover an ambiguous purchase without re-sending anything: find the
        order the supplier recorded for this intent's idempotency key."""
        reference = intent.get("supplier_reference") or ""
        if reference:
            return await self.lookup_order(reference)
        external_id = intent.get("idempotency_key") or ""
        if not external_id:
            return None
        cursor: str | None = None
        for _ in range(5):  # Recent history only; recovery is best-effort.
            query: dict[str, Any] = {"limit": 100}
            if cursor:
                query["cursor"] = cursor
            reply = await self.transport.request("GET", ORDERS_PATH, query=query,
                                                 headers=self._auth())
            result = self.check_response(reply)
            orders = result.get("orders")
            if not isinstance(orders, list):
                raise CanbosoError("invalid_order_history_response")
            matches = [o for o in orders if isinstance(o, dict)
                       and o.get("external_order_id") == external_id]
            if len(matches) > 1:
                return None  # Ambiguous; an operator must resolve it.
            if matches:
                order = matches[0]
                if order.get("status") in ("failed", "cancelled", "refunded"):
                    # The supplier confirms no fulfillment; safe to refund.
                    raise PurchaseRejected("supplier_confirmed_not_fulfilled")
                return await self.lookup_order(str(order.get("order_number") or ""))
            cursor = result.get("next_cursor")
            if cursor is None:
                return None
        return None


# Shared provider-module interface (see shop/providers.py).
Client = JahaClient
