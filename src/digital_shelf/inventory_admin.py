"""Transactional inventory import commands and admin-safe results."""

import json
from collections.abc import Sequence
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.inventory import InventoryCipher, InventoryImporter, InventoryImportPreview


class ProductNotFoundError(Exception):
    """The requested product is not present."""


class InventoryPolicyError(Exception):
    """The product cannot accept unique encrypted inventory."""


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
