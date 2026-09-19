"""Acczone Activation API v1, from its published documentation
(https://api.acczone.xyz).

This API is deliberately different and stays safe anyway:

* The API key travels as a documented `apikey` query parameter, so request
  URLs are secret: they are never logged, never included in errors, and
  network exceptions are re-raised from None with a safe code.
* Purchases are GET /buyCpn with NO native idempotency. This client never
  retries, and shop.supplier_store.resolve blocks operator-approved resends
  for this provider (IDEMPOTENT_PURCHASES = False).
* An uncertain purchase is recovered read-only via GET /getHistory: exactly
  one matching record after the intent's first send adopts it; zero or many
  leaves the intent uncertain for an operator.
* Documented rate limit is one request every two seconds, enforced here.

Amounts are documented as plain numbers (USD-denominated in every example);
they are normalized to USD at this boundary, and any explicit non-USD
currency field would fail the sync instead of being guessed.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
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

BASE_URL = "https://api.acczone.xyz"
SERVICES_PATH = "/getServices"
BALANCE_PATH = "/getBalance"
PURCHASE_PATH = "/buyCpn"
HISTORY_PATH = "/getHistory"

# No native purchase idempotency: supplier_store.resolve must refuse
# retry_same_request for this provider. Recovery is history-based only.
IDEMPOTENT_PURCHASES = False

# Documented: maximum one request every two seconds.
REQUEST_INTERVAL = 2.1

ALLOWED = {("GET", SERVICES_PATH), ("GET", BALANCE_PATH),
           ("GET", PURCHASE_PATH), ("GET", HISTORY_PATH)}


def validate_mapping_spec(specification: dict) -> None:
    """Acczone-specific mapping rules, called by shop.providers.validate_spec."""
    service_key = specification.get("product_id")
    if not isinstance(service_key, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", service_key):
        raise ShopError("Acczone product_id is the service key from GET /getServices")
    if specification.get("product_type") != "account":
        raise ShopError("Acczone sells activation codes only (internal type: account)")
    if specification.get("slot_months") is not None:
        raise ShopError("Acczone has no duration variants; do not send slot_months")


def build_purchase_body(settings, spec: dict, email: str | None,
                        *, order_id: str = "") -> dict:
    """The exact purchase body persisted with an intent; never rebuilt later.

    The key is NOT part of the persisted body: it is attached by the client
    at send time so stored intents stay free of credentials.
    """
    if email:
        raise ShopError("Email is not required for this product")
    return {"service_key": spec["product_id"], "quantity": 1}


class Transport:
    async def request(self, method: str, path: str, *, query: dict | None = None,
                      purchase: bool = False) -> Reply: ...


class HttpTransport:
    """Allowlisted Acczone endpoints only. Every query string may contain the
    API key, so no error raised here ever carries a URL or parameter values."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def request(self, method: str, path: str, *, query=None, purchase=False) -> Reply:
        if (method, path) not in ALLOWED:
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
                    method, BASE_URL + path, params=query,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    status = response.status
                    async for chunk in response.content.iter_chunked(16384):
                        remaining = 2_000_000 - len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            evidence = response_evidence(raw, status, truncated=True)
                            if purchase:
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
                # aiohttp errors embed the request URL, which carries the key.
                # Re-raise bare: no chaining, no message, no URL.
                if purchase:
                    evidence = response_evidence(raw, status, truncated=True) if status is not None else None
                    raise PurchaseUncertain("supplier_transport_failure", raw=evidence) from None
                raise CanbosoError("supplier_transport_failure") from None


class AcczoneClient:
    """products(), balance(), purchase() plus history-based recovery, all
    normalized to the shared internal snapshot/result shapes."""

    def __init__(self, settings, transport: Transport, environment: str):
        self.settings = settings
        self.transport = transport
        self.environment = environment
        self._services: dict[str, dict] = {}

    def check_enabled(self) -> None:
        if not self.settings.enabled or not self.settings.api_key:
            raise CanbosoError("acczone_disabled_or_key_missing")

    def _key_query(self) -> dict[str, str]:
        return {"apikey": self.settings.api_key}

    async def products(self) -> dict:
        """GET /getServices is unauthenticated per the documentation."""
        self.check_enabled()
        reply = await self.transport.request("GET", SERVICES_PATH)
        result = self.check_response(reply)
        if not isinstance(result, list):
            raise CanbosoError("invalid_products_response")
        products = []
        seen: set[str] = set()
        for service in result:
            products.append(self._normalize_service(service, seen))
        self._services = {p["productId"]: p for p in products}
        return {"products": products, "walletCurrency": "USD"}

    def _normalize_service(self, service: Any, seen: set[str]) -> dict:
        if not isinstance(service, dict):
            raise CanbosoError("invalid_product")
        key = text(service.get("key"), "invalid_product_id", nonempty=True)
        if not re.fullmatch(r"[a-z0-9_]{1,64}", key):
            raise CanbosoError("invalid_product_id")
        if key in seen:
            raise CanbosoError("duplicate_product_id")
        seen.add(key)
        name = text(service.get("name"), "invalid_product_name", nonempty=True)
        amount = money(service.get("price"), positive=True)
        if "currency" in service and service["currency"] not in ("USD", "USDT"):
            raise CanbosoError("unsupported_wallet_currency")
        active = service.get("is_active")
        if active not in (0, 1):
            raise CanbosoError("invalid_product_availability")
        # The API exposes no stock counter; an active service is purchasable
        # one outstanding order at a time. A sold-out purchase rejects and
        # fails the order without a debit.
        return {
            "productId": key,
            "name": name,
            "productType": "account",
            "price": {"amount": str(amount), "currency": "USD", "text": f"USD {amount}"},
            "availability": {"available": 1 if active == 1 else 0, "sold": 0},
            "promotions": [],
            "purchaseRequirements": {"quantityFixed": 1},
        }

    async def balance(self) -> dict:
        self.check_enabled()
        reply = await self.transport.request("GET", BALANCE_PATH, query=self._key_query())
        result = self.check_response(reply)
        if not isinstance(result, dict):
            raise CanbosoError("invalid_balance_response")
        if "currency" in result and result["currency"] not in ("USD", "USDT"):
            raise CanbosoError("unsupported_wallet_currency")
        amount = money(result.get("balance"))
        return {"balance": str(amount), "walletCurrency": "USD", "balanceText": f"USD {amount}"}

    async def purchase(self, exact_body: dict, idempotency_key: str) -> PurchaseResult:
        """One documented GET /buyCpn. Never retried: there is no idempotency,
        and an ambiguous outcome is recovered from history or by an operator."""
        self.check_enabled()
        if not self.settings.allow_purchases or self.settings.problems(self.environment):
            raise CanbosoError("live_supplier_spending_locked")
        if not isinstance(exact_body, dict):
            raise CanbosoError("invalid_purchase_request")
        if set(exact_body) - {"service_key", "quantity"}:
            raise CanbosoError("unsupported_purchase_fields")
        service_key = text(exact_body.get("service_key"), "invalid_product_id", nonempty=True)
        if not re.fullmatch(r"[a-z0-9_]{1,64}", service_key):
            raise CanbosoError("invalid_product_id")
        if exact_body.get("quantity") != 1:
            raise CanbosoError("only_single_item_orders_supported")
        query = {**self._key_query(), "service_key": service_key, "quantity": 1}
        try:
            reply = await self.transport.request("GET", PURCHASE_PATH, query=query, purchase=True)
        except PurchaseUncertain:
            raise
        except (CanbosoError, aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise PurchaseUncertain("purchase_transport_uncertain",
                                    raw=getattr(exc, "raw", None)) from None
        result = self.check_response(reply, purchase=True)
        try:
            return self._parse_records(result, service_key)
        except (CanbosoError, TypeError, KeyError, ValueError):
            raise PurchaseUncertain("purchase_response_needs_review",
                                    raw=reply.review_raw) from None

    def check_response(self, reply: Reply, *, purchase: bool = False) -> Any:
        if reply.status == 429:
            # Documented limit is one request per two seconds; back off longer.
            raise RateLimited("supplier_rate_limited", retry_after=5)
        if reply.status == 200:
            return reply.body
        # The documented error envelope is {"detail": "..."} with a 400; the
        # detail text is not propagated, only the safe status-derived code.
        if purchase:
            if reply.status == 400:
                raise PurchaseRejected("supplier_rejected_400")
            raise PurchaseUncertain(f"supplier_unconfirmed_{reply.status}",
                                    raw=reply.review_raw)
        if reply.status in (400, 401, 403, 404, 422):
            raise CanbosoError(f"supplier_rejected_{reply.status}")
        raise CanbosoError(f"supplier_unconfirmed_{reply.status}")

    def _parse_records(self, result: Any, service_key: str) -> PurchaseResult:
        if not isinstance(result, list):
            raise CanbosoError("missing_purchase_result")
        if not result:
            # A 200 with no record is not documented; do not assume no debit.
            raise PurchaseUncertain("purchase_response_needs_review")
        if len(result) != 1:
            raise CanbosoError("supplier_order_mismatch")
        record = result[0]
        if not isinstance(record, dict):
            raise CanbosoError("missing_purchase_result")
        if record.get("service_key") != service_key:
            raise CanbosoError("supplier_order_mismatch")
        reference = self._record_reference(record)
        payload = text(record.get("code_value"), "invalid_account_delivery", nonempty=True)
        if len(payload.encode()) > 1_000_000:
            raise CanbosoError("delivery_too_large")
        amount = self._service_price(service_key)
        return PurchaseResult(reference, "completed", amount, "USD", payload,
                              {"activations": result}, product_type="account")

    @staticmethod
    def _record_reference(record: dict) -> str:
        raw_id = record.get("id")
        if type(raw_id) is not int or raw_id < 1:
            raise CanbosoError("invalid_supplier_order_reference")
        return str(raw_id)

    def _service_price(self, service_key: str):
        service = self._services.get(service_key)
        if not service:
            # The purchase succeeded; the price comes from the last catalog
            # sync, so a missing cache entry means the response needs review.
            raise CanbosoError("missing_purchase_amount")
        return money(service["price"]["amount"])

    async def recover_uncertain(self, intent: dict) -> PurchaseResult | None:
        """Read-only recovery via GET /getHistory. Adopts the purchase only
        when exactly one history record for this service appears after the
        intent's first send; anything else stays uncertain for an operator."""
        self.check_enabled()
        service_key = intent.get("product_id") or ""
        if not re.fullmatch(r"[a-z0-9_]{1,64}", service_key):
            return None
        first_sent = intent.get("first_sent_at") or intent.get("created_at") or 0
        cutoff = first_sent - 120  # Clock-skew slack; records are second-granular.
        matches: list[dict] = []
        for page in range(1, 6):  # Recent history only; best-effort recovery.
            reply = await self.transport.request(
                "GET", HISTORY_PATH,
                query={**self._key_query(), "page": page, "limit": 100})
            result = self.check_response(reply)
            if not isinstance(result, list):
                raise CanbosoError("invalid_order_history_response")
            if not result:
                break
            oldest_on_page = None
            for record in result:
                if not isinstance(record, dict):
                    raise CanbosoError("invalid_order_history_response")
                used_at = _parse_used_at(record.get("used_at"))
                if record.get("service_key") == service_key and used_at is not None \
                        and used_at >= cutoff:
                    matches.append(record)
                if oldest_on_page is None or (used_at is not None and used_at < oldest_on_page):
                    oldest_on_page = used_at
            if len(result) < 100:
                break  # Last page.
            if oldest_on_page is not None and oldest_on_page < cutoff:
                break  # Older pages can only be older.
        if len(matches) != 1:
            return None
        try:
            record = matches[0]
            reference = self._record_reference(record)
            payload = text(record.get("code_value"), "invalid_account_delivery", nonempty=True)
            amount = self._service_price(service_key)
            return PurchaseResult(reference, "completed", amount, "USD", payload,
                                  {"activations": [record]}, product_type="account")
        except (CanbosoError, TypeError, KeyError, ValueError):
            return None


def _parse_used_at(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


# Shared provider-module interface (see shop/providers.py).
Client = AcczoneClient
