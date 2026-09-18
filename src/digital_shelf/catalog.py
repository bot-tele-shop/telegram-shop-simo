"""Canonical catalog entities and product delivery-policy validation."""

import json
import re
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class FulfillmentType(StrEnum):
    UNIQUE_CODE = "unique_code"
    UNIQUE_URL = "unique_url"
    DOWNLOAD_FILE = "download_file"
    REUSABLE_CONTENT = "reusable_content"
    MANUAL = "manual"
    SUBSCRIPTION_ACCESS = "subscription_access"


class InventoryPolicy(StrEnum):
    FINITE_UNIQUE = "finite_unique"
    FINITE_QUANTITY = "finite_quantity"
    UNLIMITED = "unlimited"
    MANUAL = "manual"


class ProductStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    AVAILABLE = "available"
    PAUSED = "paused"
    ARCHIVED = "archived"


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Multiline text still forbids control characters, but permits tab/LF/CR.
_MULTILINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: object, *, label: str, maximum: int, multiline: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} is empty or too long")
    if (_MULTILINE_CONTROL_RE if multiline else _CONTROL_RE).search(normalized):
        raise ValueError(f"{label} contains control characters")
    return normalized


def normalize_sku(value: object) -> str:
    """Normalize an internal SKU; it is never accepted from callback data."""

    if not isinstance(value, str):
        raise ValueError("SKU must be text")
    normalized = value.strip().upper()
    if not _SKU_RE.fullmatch(normalized):
        raise ValueError("SKU must contain only letters, numbers, '.', '_' or '-'")
    return normalized


def normalize_slug(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("slug must be text")
    normalized = value.strip().lower()
    if len(normalized) > 64 or not _SLUG_RE.fullmatch(normalized):
        raise ValueError("slug must use lowercase letters, numbers, and hyphens")
    return normalized


def validate_fulfillment_policy(
    fulfillment_type: FulfillmentType,
    inventory_policy: InventoryPolicy,
) -> tuple[str, ...]:
    allowed: dict[FulfillmentType, frozenset[InventoryPolicy]] = {
        FulfillmentType.UNIQUE_CODE: frozenset({InventoryPolicy.FINITE_UNIQUE}),
        FulfillmentType.UNIQUE_URL: frozenset({InventoryPolicy.FINITE_UNIQUE}),
        FulfillmentType.DOWNLOAD_FILE: frozenset(
            {InventoryPolicy.FINITE_UNIQUE, InventoryPolicy.UNLIMITED}
        ),
        FulfillmentType.REUSABLE_CONTENT: frozenset({InventoryPolicy.UNLIMITED}),
        FulfillmentType.MANUAL: frozenset({InventoryPolicy.MANUAL}),
        FulfillmentType.SUBSCRIPTION_ACCESS: frozenset(
            {InventoryPolicy.MANUAL, InventoryPolicy.UNLIMITED}
        ),
    }
    if inventory_policy in allowed[fulfillment_type]:
        return ()
    if fulfillment_type in {FulfillmentType.UNIQUE_CODE, FulfillmentType.UNIQUE_URL}:
        return (f"{fulfillment_type.value} requires finite_unique",)
    if fulfillment_type is FulfillmentType.REUSABLE_CONTENT:
        return ("reusable_content requires unlimited",)
    if fulfillment_type is FulfillmentType.MANUAL:
        return ("manual requires manual",)
    if fulfillment_type is FulfillmentType.DOWNLOAD_FILE:
        return ("download_file requires finite_unique or unlimited",)
    return ("subscription_access requires manual or unlimited",)


class CategoryCreateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    parent_id: UUID | None = None
    description: str = ""
    emoji: str | None = None
    position: StrictInt = Field(default=0, ge=0)
    active: bool = True

    @model_validator(mode="after")
    def normalize(self) -> Self:
        self.slug = normalize_slug(self.slug)
        self.name = _text(self.name, label="category name", maximum=80, multiline=False)
        if self.description:
            self.description = _text(
                self.description, label="category description", maximum=2_000
            )
        if self.emoji is not None:
            self.emoji = _text(self.emoji, label="category emoji", maximum=16, multiline=False)
        return self


class ProductCreateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: UUID
    sku: str
    title: str
    description: str
    price_stars: StrictInt = Field(gt=0)
    fulfillment_type: FulfillmentType
    inventory_policy: InventoryPolicy
    warranty_text: str | None = None
    delivery_text: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    status: ProductStatus = ProductStatus.DRAFT

    @model_validator(mode="after")
    def validate_and_normalize(self) -> Self:
        self.sku = normalize_sku(self.sku)
        self.title = _text(self.title, label="product title", maximum=160, multiline=False)
        self.description = _text(self.description, label="product description", maximum=10_000)
        if self.warranty_text is not None:
            self.warranty_text = _text(self.warranty_text, label="warranty text", maximum=2_000)
        if self.delivery_text is not None:
            self.delivery_text = _text(self.delivery_text, label="delivery text", maximum=2_000)
        reasons = validate_fulfillment_policy(self.fulfillment_type, self.inventory_policy)
        if reasons:
            raise ValueError(f"fulfillment/inventory policy: {reasons[0]}")
        if self.status not in {ProductStatus.DRAFT, ProductStatus.READY}:
            raise ValueError("product status must be draft or ready at creation")
        try:
            json.dumps(self.metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("product metadata must be JSON serializable") from exc
        return self


class ProductUpdateCommand(BaseModel):
    """Mutable product fields; SKU and category identity are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    price_stars: StrictInt | None = Field(default=None, gt=0)
    fulfillment_type: FulfillmentType | None = None
    inventory_policy: InventoryPolicy | None = None
    warranty_text: str | None = None
    delivery_text: str | None = None
    metadata: dict[str, object] | None = None
    status: ProductStatus | None = None
    expected_version: StrictInt = Field(ge=1)


class AssetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_key: str | None = None
    telegram_file_id: str | None = None


def validate_asset_reference(reference: AssetReference) -> AssetReference:
    storage_key = reference.storage_key.strip() if isinstance(reference.storage_key, str) else ""
    telegram_file_id = (
        reference.telegram_file_id.strip()
        if isinstance(reference.telegram_file_id, str)
        else ""
    )
    if bool(storage_key) == bool(telegram_file_id):
        raise ValueError("asset must contain exactly one private storage key or Telegram file id")
    if storage_key:
        if "://" in storage_key or storage_key.startswith("/") or _CONTROL_RE.search(storage_key):
            raise ValueError("asset storage key must be a private relative reference")
        return AssetReference(storage_key=storage_key)
    if not telegram_file_id or any(character.isspace() for character in telegram_file_id):
        raise ValueError("asset Telegram file id is invalid")
    return AssetReference(telegram_file_id=telegram_file_id)
