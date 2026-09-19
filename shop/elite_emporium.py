"""Elite Digital Emporium Telegram Buyer API v1, from its published OpenAPI
contract (https://shop.elitedigitalemporium.com/api/telegram-buyer/openapi.json).

Auth is a Bearer header (tgb_ key), never a URL parameter. Purchases carry
the intent's idempotency_key in the documented request field, and this client
NEVER retries on its own. Elite has no server-side max-price parameter, so
the shop's client-side preflight max_cost cap is the only guard and stays
enforced in supplier_store on every claim.

The contract documents response content only by description, so every field
is validated strictly and anything unexpected fails safe (sync error or an
uncertain purchase for operator review, never a silent guess).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
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
)
from .errors import ShopError

BASE_URL = "https://shop.elitedigitalemporium.com"
PRODUCTS_PATH = "/api/telegram-buyer/products"
BALANCE_PATH = "/api/telegram-buyer/balance"
PURCHASE_PATH = "/api/telegram-buyer/purchase"
ORDERS_PATH = "/api/telegram-buyer/orders"

# Purchases carry a documented idempotency_key field; an operator-approved
# retry resends the exact same persisted body.
IDEMPOTENT_PURCHASES = True

# No documented rate limit; stay under Canboso-style courtesy pacing.
REQUEST_INTERVAL = 2.2


def validate_mapping_spec(specification: dict) -> None:
    """Elite-specific mapping rules, called by shop.providers.validate_spec."""
    product_id = specification.get("product_id")
    if not isinstance(product_id, str) or not re.fullmatch(r"[0-9]{1,18}", product_id):
        raise ShopError("Elite Digital Emporium product_id is the numeric catalog ID")
    if specification.get("product_type") != "account":
        raise ShopError("Elite Digital Emporium sells credential products only "
                        "(internal type: account)")
    if specification.get("slot_months") is not None:
        raise ShopError("Elite Digital Emporium has no duration variants; do not send slot_months")


def build_purchase_body(settings, spec: dict, email: str | None,
                        *, order_id: str = "") -> dict:
    """The exact purchase body persisted with an intent; never rebuilt later."""
    if email:
        raise ShopError("Email is not required for this product")
    body: dict[str, Any] = {
        "product_id": int(spec["product_id"]),
        "quantity": 1,
    }
    if order_id:
        key = f"ds-{order_id}"
        # One key per purchase, stored with the intent; a retry reuses both.
        body["idempotency_key"] = key
        body["external_order_id"] = key
    return body


class Transport:
    async def request(self, method: str, path: str, *, query: dict | None = None,
                      body: dict | None = None, headers: dict | None = None) -> Reply: ...


class HttpTransport:
    """Allowlisted Elite endpoints only. The bearer key is a header; remote
    messages and network errors are reduced to safe codes."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def request(self, method: str, path: str, *, query=None, body=None, headers=None) -> Reply:
        allowed = (method, path) in {("GET", PRODUCTS_PATH), ("GET", BALANCE_PATH),
                                     ("POST", PURCHASE_PATH)} or (
            method == "GET" and re.fullmatch(r"/api/telegram-buyer/orders/[0-9]{1,18}", path))
        if not allowed:
            raise CanbosoError("unsupported_endpoint")
        async with self.lock:
            delay = self.last_request + REQUEST_INTERVAL - time.monotonic()
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
                    evidence = None if isinstance(result, (dict, list)) else response_evidence(raw, status)
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


def _product_list(payload: Any) -> list:
    """The contract shows a product list; accept a bare array or a common
    paginated envelope, and reject anything else outright."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "products", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise CanbosoError("invalid_products_response")


class EliteClient:
    """products(), balance(), purchase() plus documented order lookup, all
    normalized to the shared internal snapshot/result shapes."""

    def __init__(self, settings, transport: Transport, environment: str):
        self.settings = settings
        self.transport = transport
        self.environment = environment
        self._products: dict[str, dict] = {}

    def check_enabled(self) -> None:
        if not self.settings.enabled or not self.settings.api_key:
            raise CanbosoError("elite_emporium_disabled_or_key_missing")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    async def products(self) -> dict:
        """In-stock catalog, normalized to the internal snapshot shape."""
        self.check_enabled()
        products: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 21):  # Hard page cap; an endless catalog is a bug.
            reply = await self.transport.request(
                "GET", PRODUCTS_PATH,
                query={"per_page": 100, "page": page}, headers=self._auth())
            result = self.check_response(reply)
            items = _product_list(result)
            if not items:
                break
            for raw_product in items:
                products.append(self._normalize_product(raw_product, seen))
            if len(items) < 100:
                break
        # Cache the catalog so _parse_order can fall back to the synced price.
        self._products = {p["productId"]: p for p in products}
        return {"products": products, "walletCurrency": "USD"}

    def _normalize_product(self, product: Any, seen: set[str]) -> dict:
        if not isinstance(product, dict):
            raise CanbosoError("invalid_product")
        raw_id = product.get("id", product.get("product_id"))
        if type(raw_id) is not int or raw_id < 1:
            raise CanbosoError("invalid_product_id")
        product_id = str(raw_id)
        if product_id in seen:
            raise CanbosoError("duplicate_product_id")
        seen.add(product_id)
        name = product.get("name", product.get("title"))
        if name is None:
            name = f"Elite product {product_id}"
        text(name, "invalid_product_name", nonempty=True)
        if "currency" in product and product["currency"] not in ("USD", "USDT"):
            raise CanbosoError("unsupported_wallet_currency")
        price = product.get("price", product.get("price_usd"))
        amount = money(price, positive=True)
        stock = product.get("stock", product.get("stock_count", product.get("available")))
        if type(stock) is not int or stock < 0:
            raise CanbosoError("invalid_product_availability")
        return {
            "productId": product_id,
            "name": name,
            "productType": "account",
            "price": {"amount": str(amount), "currency": "USD", "text": f"USD {amount}"},
            "availability": {"available": stock, "sold": 0},
            "promotions": [],
            "purchaseRequirements": {"quantityFixed": 1},
        }

    async def balance(self) -> dict:
        self.check_enabled()
        reply = await self.transport.request("GET", BALANCE_PATH, headers=self._auth())
        result = self.check_response(reply)
        if not isinstance(result, dict):
            raise CanbosoError("invalid_balance_response")
        if "currency" in result and result["currency"] not in ("USD", "USDT"):
            raise CanbosoError("unsupported_wallet_currency")
        amount = money(result.get("balance"))
        normalized: dict[str, Any] = {"balance": str(amount), "walletCurrency": "USD",
                                      "balanceText": f"USD {amount}"}
        for optional in ("credit_limit", "creditLimit"):
            if optional in result:
                normalized["creditLimit"] = money(result[optional])
        for optional in ("owed", "amount_owed", "dues"):
            if optional in result:
                normalized["amountOwed"] = money(result[optional])
        return normalized

    async def purchase(self, exact_body: dict, idempotency_key: str) -> PurchaseResult:
        self.check_enabled()
        if not self.settings.allow_purchases or self.settings.problems(self.environment):
            raise CanbosoError("live_supplier_spending_locked")
        if not isinstance(exact_body, dict):
            raise CanbosoError("invalid_purchase_request")
        if (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)):
            raise CanbosoError("invalid_idempotency_key")
        if set(exact_body) - {"product_id", "quantity", "idempotency_key",
                              "external_order_id", "customer"}:
            raise CanbosoError("unsupported_purchase_fields")
        if type(exact_body.get("product_id")) is not int or exact_body["product_id"] < 1:
            raise CanbosoError("invalid_product_id")
        if exact_body.get("quantity") != 1:
            raise CanbosoError("only_single_item_orders_supported")
        # The persisted body's idempotency field must match the intent key:
        # rebuilding it here would risk a second purchase on retry.
        if exact_body.get("idempotency_key") != idempotency_key:
            raise CanbosoError("idempotency_key_mismatch_do_not_rebuild_request")
        try:
            reply = await self.transport.request("POST", PURCHASE_PATH, body=exact_body,
                                                 headers=self._auth())
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

    def check_response(self, reply: Reply, *, purchase: bool = False) -> Any:
        if reply.status == 429:
            raise RateLimited("supplier_rate_limited", retry_after=_retry_after(reply))
        if purchase:
            if reply.status == 200:
                return reply.body
            # 402 insufficient balance and 409 insufficient stock are
            # documented rejections: the wallet was not debited.
            if reply.status in (400, 401, 403, 404, 402, 409, 422):
                raise PurchaseRejected(f"supplier_rejected_{reply.status}")
            raise PurchaseUncertain(f"supplier_unconfirmed_{reply.status}",
                                    raw=reply.review_raw)
        if reply.status == 200:
            return reply.body
        if reply.status in (400, 401, 403, 404, 422):
            raise CanbosoError(f"supplier_rejected_{reply.status}")
        raise CanbosoError(f"supplier_unconfirmed_{reply.status}")

    def _parse_order(self, body: Any, request: dict) -> PurchaseResult:
        if not isinstance(body, dict):
            raise CanbosoError("missing_purchase_result")
        order = body.get("order") if isinstance(body.get("order"), dict) else body
        raw_reference = order.get("id", order.get("order_id"))
        if type(raw_reference) is not int or raw_reference < 1:
            raise CanbosoError("invalid_supplier_order_reference")
        reference = str(raw_reference)
        requested = request.get("product_id")
        echoed = order.get("product_id", order.get("productId"))
        if (requested is not None and echoed is not None and type(echoed) is int
                and echoed != requested):
            raise CanbosoError("supplier_order_mismatch")
        amount = None
        for field_name in ("total", "total_price", "amount", "price"):
            if field_name in order:
                amount = money(order[field_name])
                break
        if amount is None:
            # The contract documents a wallet-debit purchase; fall back to the
            # catalog price from the last sync rather than trusting nothing.
            product = self._products.get(str(requested if requested is not None else echoed))
            if not product:
                raise CanbosoError("missing_purchase_amount")
            amount = money(product["price"]["amount"])
        credentials = order.get("credentials", order.get("credential", order.get("delivery")))
        payload = self._payload_text(credentials)
        return PurchaseResult(reference, "completed", amount, "USD", payload,
                              body if isinstance(body, dict) else {"order": order},
                              product_type="account")

    @staticmethod
    def _payload_text(credentials: Any) -> str:
        if isinstance(credentials, str) and credentials.strip():
            payload = credentials
        elif isinstance(credentials, list) and credentials:
            lines = []
            for entry in credentials:
                if isinstance(entry, str) and entry.strip():
                    lines.append(entry)
                elif isinstance(entry, dict):
                    parts = [f"{k}: {v}" for k, v in entry.items()
                             if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
                    if parts:
                        lines.append("\n".join(parts))
                else:
                    raise CanbosoError("invalid_account_delivery")
            payload = "\n\n".join(lines)
        else:
            raise CanbosoError("incomplete_supplier_delivery")
        if not payload.strip() or len(payload.encode()) > 1_000_000:
            raise CanbosoError("invalid_account_delivery")
        return payload

    async def lookup_order(self, reference: str) -> PurchaseResult | None:
        """Documented GET /telegram-buyer/orders/{order}; None when not found."""
        self.check_enabled()
        if not re.fullmatch(r"[0-9]{1,18}", reference):
            raise CanbosoError("invalid_supplier_order_reference")
        reply = await self.transport.request("GET", f"{ORDERS_PATH}/{reference}",
                                             headers=self._auth())
        if reply.status == 404:
            return None
        result = self.check_response(reply)
        try:
            return self._parse_order(result, {"product_id": None})
        except (CanbosoError, TypeError, KeyError, ValueError):
            return None  # Keep the intent uncertain for an operator.

    async def recover_uncertain(self, intent: dict) -> PurchaseResult | None:
        """Elite has no order-history endpoint in the contract, so only an
        intent that already captured a numeric reference can be recovered."""
        reference = intent.get("supplier_reference") or ""
        if not reference:
            return None
        return await self.lookup_order(reference)


# Shared provider-module interface (see shop/providers.py).
Client = EliteClient
