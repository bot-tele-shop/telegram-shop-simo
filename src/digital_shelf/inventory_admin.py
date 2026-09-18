"""Transactional inventory import commands and admin-safe results."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.inventory import (
    InventoryCipher,
    InventoryImporter,
    InventoryImportPreview,
)


class ProductNotFoundError(Exception):
    """The requested product is not present."""


class InventoryPolicyError(Exception):
    """The product cannot accept unique encrypted inventory."""


class InventoryUnavailableError(Exception):
    """No available unique inventory can be reserved."""


class InventoryStateConflictError(Exception):
    """An inventory item is not in the expected state for the command."""


class InventoryImportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[str] = Field(min_length=1, max_length=500)


class InventoryCommitCommand(InventoryImportCommand):
    confirm: Literal[True]


class InventoryImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_lines: int
    added_count: int
    duplicate_count: int
    blank_count: int
    invalid_count: int
    issues: tuple[tuple[int, str], ...]


class InventoryStore(Protocol):
    async def preview(
        self, *, product_id: UUID, lines: Sequence[str]
    ) -> InventoryImportPreview: ...

    async def commit(
        self,
        *,
        product_id: UUID,
        lines: Sequence[str],
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> InventoryImportResult: ...


@dataclass(frozen=True)
class InventoryReservation:
    item_id: UUID
    product_id: UUID
    order_item_id: UUID
    reserved_until: datetime


class InventoryAllocator(Protocol):
    async def reserve_one(
        self,
        *,
        product_id: UUID,
        order_item_id: UUID,
        reserved_until: datetime,
        correlation_id: UUID,
    ) -> InventoryReservation: ...

    async def release_expired(self, *, now: datetime, correlation_id: UUID) -> int: ...

    async def confirm_sale(
        self,
        *,
        item_id: UUID,
        order_item_id: UUID,
        correlation_id: UUID,
    ) -> None: ...

    async def retire_available(self, *, item_id: UUID, correlation_id: UUID) -> None: ...

    async def quarantine_sold(self, *, item_id: UUID, correlation_id: UUID) -> None: ...


class DatabaseInventoryStore:
    def __init__(self, engine: AsyncEngine, cipher: InventoryCipher) -> None:
        self._engine = engine
        self._cipher = cipher

    async def _product_policy(self, connection: object, product_id: UUID) -> str:
        row = await connection.scalar(  # type: ignore[attr-defined]
            text("SELECT inventory_policy FROM digital_shelf.products WHERE id = :id"),
            {"id": product_id},
        )
        if row is None:
            raise ProductNotFoundError
        policy = str(row)
        if policy != "finite_unique":
            raise InventoryPolicyError
        return policy

    async def _existing_fingerprints(self, connection: object, product_id: UUID) -> set[str]:
        rows = await connection.execute(  # type: ignore[attr-defined]
            text(
                """
                SELECT fingerprint FROM digital_shelf.inventory_items
                WHERE product_id = :product_id
                """
            ),
            {"product_id": product_id},
        )
        return {str(value) for value in rows.scalars()}

    async def preview(
        self, *, product_id: UUID, lines: Sequence[str]
    ) -> InventoryImportPreview:
        async with self._engine.connect() as connection:
            await self._product_policy(connection, product_id)
            existing = await self._existing_fingerprints(connection, product_id)
        return InventoryImporter(self._cipher).prepare(lines, existing).preview

    async def commit(
        self,
        *,
        product_id: UUID,
        lines: Sequence[str],
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> InventoryImportResult:
        async with self._engine.begin() as connection:
            await self._product_policy(connection, product_id)
            existing = await self._existing_fingerprints(connection, product_id)
            prepared = InventoryImporter(self._cipher).prepare(lines, existing)
            added_count = 0
            race_duplicate_count = 0
            for record in prepared.records:
                inserted = await connection.scalar(
                    text(
                        """
                        INSERT INTO digital_shelf.inventory_items
                            (product_id, fingerprint, ciphertext, encryption_key_version)
                        VALUES (:product_id, :fingerprint, :ciphertext, :key_version)
                        ON CONFLICT (product_id, fingerprint) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "product_id": product_id,
                        "fingerprint": record.fingerprint,
                        "ciphertext": record.ciphertext.encode(),
                        "key_version": record.key_version,
                    },
                )
                if inserted is None:
                    race_duplicate_count += 1
                else:
                    added_count += 1

            if added_count:
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.inventory_counters
                            (product_id, available_quantity)
                        VALUES (:product_id, :added_count)
                        ON CONFLICT (product_id) DO UPDATE SET
                            available_quantity = digital_shelf.inventory_counters.available_quantity
                                + EXCLUDED.available_quantity,
                            version = digital_shelf.inventory_counters.version + 1,
                            updated_at = now()
                        """
                    ),
                    {"product_id": product_id, "added_count": added_count},
                )

            await connection.execute(
                text(
                    """
                    INSERT INTO digital_shelf.audit_events
                        (actor_admin_id, action, target_type, target_id, result,
                         detail, correlation_id)
                    VALUES
                        (:actor_admin_id, 'inventory.commit', 'product', :target_id,
                         'succeeded', CAST(:detail AS jsonb), :correlation_id)
                    """
                ),
                {
                    "actor_admin_id": actor_admin_id,
                    "target_id": str(product_id),
                    "detail": json.dumps(
                        {
                            "total_lines": prepared.preview.total_lines,
                            "added_count": added_count,
                            "duplicate_count": prepared.preview.duplicate_count
                            + race_duplicate_count,
                            "blank_count": prepared.preview.blank_count,
                            "invalid_count": prepared.preview.invalid_count,
                        },
                        separators=(",", ":"),
                    ),
                    "correlation_id": correlation_id,
                },
            )

        return InventoryImportResult(
            total_lines=prepared.preview.total_lines,
            added_count=added_count,
            duplicate_count=prepared.preview.duplicate_count + race_duplicate_count,
            blank_count=prepared.preview.blank_count,
            invalid_count=prepared.preview.invalid_count,
            issues=prepared.preview.issues,
        )


class DatabaseInventoryAllocator:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _audit(
        self,
        connection: object,
        *,
        action: str,
        target_id: UUID,
        correlation_id: UUID,
    ) -> None:
        await connection.execute(  # type: ignore[attr-defined]
            text(
                """
                INSERT INTO digital_shelf.audit_events
                    (action, target_type, target_id, result, correlation_id)
                VALUES (:action, 'inventory_item', :target_id, 'succeeded', :correlation_id)
                """
            ),
            {
                "action": action,
                "target_id": str(target_id),
                "correlation_id": correlation_id,
            },
        )

    async def _counter_delta(
        self,
        connection: object,
        *,
        product_id: UUID,
        available: int,
        reserved: int,
        sold: int,
    ) -> None:
        result = await connection.execute(  # type: ignore[attr-defined]
            text(
                """
                UPDATE digital_shelf.inventory_counters
                SET available_quantity = available_quantity + :available,
                    reserved_quantity = reserved_quantity + :reserved,
                    sold_quantity = sold_quantity + :sold,
                    version = version + 1,
                    updated_at = now()
                WHERE product_id = :product_id
                  AND available_quantity + :available >= 0
                  AND reserved_quantity + :reserved >= 0
                  AND sold_quantity + :sold >= 0
                """
            ),
            {
                "product_id": product_id,
                "available": available,
                "reserved": reserved,
                "sold": sold,
            },
        )
        if result.rowcount != 1:
            raise InventoryStateConflictError

    async def reserve_one(
        self,
        *,
        product_id: UUID,
        order_item_id: UUID,
        reserved_until: datetime,
        correlation_id: UUID,
    ) -> InventoryReservation:
        async with self._engine.begin() as connection:
            policy = await connection.scalar(
                text("SELECT inventory_policy FROM digital_shelf.products WHERE id = :id"),
                {"id": product_id},
            )
            if policy is None:
                raise ProductNotFoundError
            if str(policy) != "finite_unique":
                raise InventoryPolicyError
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT id
                        FROM digital_shelf.inventory_items
                        WHERE product_id = :product_id AND state = 'available'
                        ORDER BY id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """
                    ),
                    {"product_id": product_id},
                )
            ).mappings().one_or_none()
            if row is None:
                raise InventoryUnavailableError
            item_id = UUID(str(row["id"]))
            updated = (
                await connection.execute(
                    text(
                        """
                        UPDATE digital_shelf.inventory_items
                        SET state = 'reserved',
                            reserved_until = :reserved_until,
                            assigned_order_item_id = :order_item_id,
                            reserved_at = now()
                        WHERE id = :item_id AND state = 'available'
                        RETURNING id
                        """
                    ),
                    {
                        "reserved_until": reserved_until,
                        "order_item_id": order_item_id,
                        "item_id": item_id,
                    },
                )
            ).scalar_one_or_none()
            if updated is None:
                raise InventoryStateConflictError
            await self._counter_delta(
                connection,
                product_id=product_id,
                available=-1,
                reserved=1,
                sold=0,
            )
            await self._audit(
                connection,
                action="inventory.reserve",
                target_id=item_id,
                correlation_id=correlation_id,
            )
        return InventoryReservation(
            item_id=item_id,
            product_id=product_id,
            order_item_id=order_item_id,
            reserved_until=reserved_until,
        )

    async def release_expired(self, *, now: datetime, correlation_id: UUID) -> int:
        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, product_id
                        FROM digital_shelf.inventory_items
                        WHERE state = 'reserved' AND reserved_until <= :now
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"now": now},
                )
            ).mappings().all()
            for row in rows:
                item_id = UUID(str(row["id"]))
                product_id = UUID(str(row["product_id"]))
                await connection.execute(
                    text(
                        """
                        UPDATE digital_shelf.inventory_items
                        SET state = 'available', reserved_until = NULL,
                            assigned_order_item_id = NULL, reserved_at = NULL
                        WHERE id = :item_id AND state = 'reserved'
                        """
                    ),
                    {"item_id": item_id},
                )
                await self._counter_delta(
                    connection,
                    product_id=product_id,
                    available=1,
                    reserved=-1,
                    sold=0,
                )
                await self._audit(
                    connection,
                    action="inventory.release_expired",
                    target_id=item_id,
                    correlation_id=correlation_id,
                )
        return len(rows)

    async def confirm_sale(
        self,
        *,
        item_id: UUID,
        order_item_id: UUID,
        correlation_id: UUID,
    ) -> None:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE digital_shelf.inventory_items
                        SET state = 'sold', reserved_until = NULL, sold_at = now()
                        WHERE id = :item_id
                          AND state = 'reserved'
                          AND assigned_order_item_id = :order_item_id
                        RETURNING product_id
                        """
                    ),
                    {"item_id": item_id, "order_item_id": order_item_id},
                )
            ).mappings().one_or_none()
            if row is None:
                raise InventoryStateConflictError
            await self._counter_delta(
                connection,
                product_id=UUID(str(row["product_id"])),
                available=0,
                reserved=-1,
                sold=1,
            )
            await self._audit(
                connection,
                action="inventory.sell",
                target_id=item_id,
                correlation_id=correlation_id,
            )

    async def retire_available(self, *, item_id: UUID, correlation_id: UUID) -> None:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE digital_shelf.inventory_items
                        SET state = 'retired', retired_at = now()
                        WHERE id = :item_id AND state = 'available'
                        RETURNING product_id
                        """
                    ),
                    {"item_id": item_id},
                )
            ).mappings().one_or_none()
            if row is None:
                raise InventoryStateConflictError
            await self._counter_delta(
                connection,
                product_id=UUID(str(row["product_id"])),
                available=-1,
                reserved=0,
                sold=0,
            )
            await self._audit(
                connection,
                action="inventory.retire",
                target_id=item_id,
                correlation_id=correlation_id,
            )

    async def quarantine_sold(self, *, item_id: UUID, correlation_id: UUID) -> None:
        async with self._engine.begin() as connection:
            updated = (
                await connection.execute(
                    text(
                        """
                        UPDATE digital_shelf.inventory_items
                        SET state = 'quarantined', quarantined_at = now()
                        WHERE id = :item_id AND state = 'sold'
                        RETURNING id
                        """
                    ),
                    {"item_id": item_id},
                )
            ).scalar_one_or_none()
            if updated is None:
                raise InventoryStateConflictError
            await self._audit(
                connection,
                action="inventory.quarantine",
                target_id=item_id,
                correlation_id=correlation_id,
            )
