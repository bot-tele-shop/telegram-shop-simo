"""Owner-only, minimized views over canonical audit events."""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class AuditEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    actor_admin_id: UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    result: Literal["succeeded", "denied", "failed"]
    correlation_id: UUID
    created_at: datetime


class AuditStore(Protocol):
    async def list_events(self, *, limit: int) -> Sequence[AuditEventView]: ...


class DatabaseAuditStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_events(self, *, limit: int) -> Sequence[AuditEventView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, actor_admin_id, action, target_type, target_id,
                               result, correlation_id, created_at
                        FROM digital_shelf.audit_events
                        ORDER BY created_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings().all()
        return [AuditEventView.model_validate(dict(row)) for row in rows]
