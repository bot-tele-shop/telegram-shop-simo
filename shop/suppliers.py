"""Supplier boundary, intentionally disabled until the real API is documented.

A Telegram username is not a fulfillment API. Do not automate a personal account
or replay another bot's private requests. The first version fulfills only stock
already imported by the merchant, so checkout cannot silently spend supplier funds.
"""

from dataclasses import dataclass
from typing import Protocol


class SupplierNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class SupplierProduct:
    supplier_sku: str
    title: str
    available: bool


@dataclass(frozen=True)
class SupplierDelivery:
    reference: str
    payload: str


class SupplierAdapter(Protocol):
    async def list_products(self) -> list[SupplierProduct]: ...

    async def fulfill(self, supplier_sku: str, *, idempotency_key: str) -> SupplierDelivery: ...

    async def lookup_order(self, *, idempotency_key: str) -> SupplierDelivery | None: ...


class UnconfiguredSupplier:
    async def list_products(self) -> list[SupplierProduct]:
        raise SupplierNotConfigured("Supplier API documentation and authorization are required")

    async def fulfill(self, supplier_sku: str, *, idempotency_key: str) -> SupplierDelivery:
        raise SupplierNotConfigured("Supplier checkout is disabled; no external request was sent")

    async def lookup_order(self, *, idempotency_key: str) -> SupplierDelivery | None:
        raise SupplierNotConfigured("Supplier API is not connected")
