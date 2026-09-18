"""Canonical admin catalog commands and minimized read models."""

import json
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.catalog import (
    CategoryCreateCommand,
    FulfillmentType,
    InventoryPolicy,
    ProductCreateCommand,
    ProductStatus,
)


class CategoryConflictError(Exception):
    """Category identity conflicts with an existing category."""


class CategoryNotFoundError(Exception):
    """Category or parent category does not exist."""


class ProductConflictError(Exception):
    """Product identity conflicts with an existing product."""


class ProductNotFoundError(Exception):
    """Product or referenced category does not exist."""


class CategoryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    parent_id: UUID | None
    slug: str
    name: str
    description: str
    emoji: str | None
    position: int
    active: bool


class ProductView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    category_id: UUID
    sku: str
    title: str
    description: str
    price_stars: int
    currency: str
    status: ProductStatus
    fulfillment_type: FulfillmentType
    inventory_policy: InventoryPolicy
    warranty_text: str | None
    delivery_text: str | None
    metadata: dict[str, object]
    version: int


class CatalogStore(Protocol):
    async def list_categories(self) -> Sequence[CategoryView]: ...

    async def create_category(
        self,
        *,
        command: CategoryCreateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> CategoryView: ...

    async def list_products(self) -> Sequence[ProductView]: ...

    async def create_product(
        self,
        *,
        command: ProductCreateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> ProductView: ...


async def _audit(
    connection: object,
    *,
    actor_admin_id: UUID,
    action: str,
    target_type: str,
    target_id: UUID,
    correlation_id: UUID,
) -> None:
    await connection.execute(  # type: ignore[attr-defined]
        text(
            """
            INSERT INTO digital_shelf.audit_events
                (actor_admin_id, action, target_type, target_id, result, correlation_id)
            VALUES (:actor_admin_id, :action, :target_type, :target_id,
                    'succeeded', :correlation_id)
            """
        ),
        {
            "actor_admin_id": actor_admin_id,
            "action": action,
            "target_type": target_type,
            "target_id": str(target_id),
            "correlation_id": correlation_id,
        },
    )


def _category_view(row: dict[str, object]) -> CategoryView:
    return CategoryView(
        id=UUID(str(row["id"])),
        parent_id=UUID(str(row["parent_id"])) if row["parent_id"] else None,
        slug=str(row["slug"]),
        name=str(row["name"]),
        description=str(row["description"]),
        emoji=str(row["emoji"]) if row["emoji"] else None,
        position=int(str(row["position"])),
        active=bool(row["active"]),
    )


def _product_view(row: dict[str, object]) -> ProductView:
    metadata = row["metadata"]
    return ProductView(
        id=UUID(str(row["id"])),
        category_id=UUID(str(row["category_id"])),
        sku=str(row["sku"]),
        title=str(row["title"]),
        description=str(row["description"]),
        price_stars=int(str(row["price_stars"])),
        currency=str(row["currency"]),
        status=ProductStatus(str(row["status"])),
        fulfillment_type=FulfillmentType(str(row["fulfillment_type"])),
        inventory_policy=InventoryPolicy(str(row["inventory_policy"])),
        warranty_text=str(row["warranty_text"]) if row["warranty_text"] else None,
        delivery_text=str(row["delivery_text"]) if row["delivery_text"] else None,
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        version=int(str(row["version"])),
    )


class DatabaseCatalogStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_categories(self) -> Sequence[CategoryView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, parent_id, slug, name, description, emoji, position, active
                        FROM digital_shelf.categories
                        ORDER BY position, name
                        """
                    )
                )
            ).mappings().all()
        return [_category_view(dict(row)) for row in rows]

    async def create_category(
        self,
        *,
        command: CategoryCreateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> CategoryView:
        async with self._engine.begin() as connection:
            if command.parent_id is not None:
                parent_exists = await connection.scalar(
                    text("SELECT 1 FROM digital_shelf.categories WHERE id = :id"),
                    {"id": command.parent_id},
                )
                if parent_exists is None:
                    raise CategoryNotFoundError
            try:
                row = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO digital_shelf.categories
                                (parent_id, slug, name, description, emoji, position, active)
                            VALUES (:parent_id, :slug, :name, :description, :emoji, :position, :active)
                            RETURNING id, parent_id, slug, name, description, emoji, position, active
                            """
                        ),
                        command.model_dump(),
                    )
                ).mappings().one()
            except IntegrityError as exc:
                raise CategoryConflictError from exc
            view = _category_view(dict(row))
            await _audit(
                connection,
                actor_admin_id=actor_admin_id,
                action="category.create",
                target_type="category",
                target_id=view.id,
                correlation_id=correlation_id,
            )
        return view

    async def list_products(self) -> Sequence[ProductView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, category_id, sku, title, description, price_stars, currency,
                               status, fulfillment_type, inventory_policy, warranty_text,
                               delivery_text, metadata, version
                        FROM digital_shelf.products
                        ORDER BY title, sku
                        """
                    )
                )
            ).mappings().all()
        return [_product_view(dict(row)) for row in rows]

    async def create_product(
        self,
        *,
        command: ProductCreateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> ProductView:
        async with self._engine.begin() as connection:
            category_exists = await connection.scalar(
                text("SELECT 1 FROM digital_shelf.categories WHERE id = :id"),
                {"id": command.category_id},
            )
            if category_exists is None:
                raise ProductNotFoundError
            values = command.model_dump()
            values["fulfillment_type"] = command.fulfillment_type.value
            values["inventory_policy"] = command.inventory_policy.value
            values["status"] = command.status.value
            try:
                row = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO digital_shelf.products
                                (category_id, sku, title, description, price_stars,
                                 fulfillment_type, inventory_policy, warranty_text,
                                 delivery_text, metadata, status)
                            VALUES (:category_id, :sku, :title, :description, :price_stars,
                                    :fulfillment_type, :inventory_policy, :warranty_text,
                                    :delivery_text, CAST(:metadata AS jsonb), :status)
                            RETURNING id, category_id, sku, title, description, price_stars, currency,
                                      status, fulfillment_type, inventory_policy, warranty_text,
                                      delivery_text, metadata, version
                            """
                        ),
                        {**values, "metadata": json.dumps(command.metadata)},
                    )
                ).mappings().one()
            except IntegrityError as exc:
                raise ProductConflictError from exc
            view = _product_view(dict(row))
            await _audit(
                connection,
                actor_admin_id=actor_admin_id,
                action="product.create",
                target_type="product",
                target_id=view.id,
                correlation_id=correlation_id,
            )
        return view
