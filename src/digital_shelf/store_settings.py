"""Typed canonical store settings and transactional persistence."""

import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class SettingKey(StrEnum):
    SHOP_NAME = "shop_name"
    SUPPORT_CONTACT = "support_contact"
    TERMS_TEXT = "terms_text"
    PRIVACY_TEXT = "privacy_text"
    CHECKOUT_PAUSED = "checkout_paused"


class InvalidSettingValueError(Exception):
    """A setting failed its server-owned type or content rules."""


class SettingRevisionConflictError(Exception):
    """One or more setting writes used a stale revision."""


class SettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: object
    expected_revision: int = Field(ge=0)


class SettingsPatchCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: list[SettingUpdate] = Field(min_length=1, max_length=len(SettingKey))

    @model_validator(mode="after")
    def unique_keys(self) -> "SettingsPatchCommand":
        keys = [update.key for update in self.updates]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate setting key")
        return self


class SettingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: SettingKey
    value: object
    revision: int
    updated_at: datetime


class SettingStore(Protocol):
    async def list_settings(self) -> Sequence[SettingView]: ...

    async def update_settings(
        self,
        *,
        command: SettingsPatchCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> Sequence[SettingView]: ...


def _validated_text(value: object, *, maximum: int, multiline: bool) -> str:
    if not isinstance(value, str):
        raise InvalidSettingValueError
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidSettingValueError
    if "\x00" in normalized:
        raise InvalidSettingValueError
    if not multiline and any(ord(character) < 32 for character in normalized):
        raise InvalidSettingValueError
    return normalized


def validate_setting(key: SettingKey, value: object) -> object:
    if key is SettingKey.SHOP_NAME:
        return _validated_text(value, maximum=64, multiline=False)
    if key is SettingKey.SUPPORT_CONTACT:
        return _validated_text(value, maximum=256, multiline=False)
    if key in {SettingKey.TERMS_TEXT, SettingKey.PRIVACY_TEXT}:
        return _validated_text(value, maximum=10_000, multiline=True)
    if key is SettingKey.CHECKOUT_PAUSED:
        if not isinstance(value, bool):
            raise InvalidSettingValueError
        return value
    raise InvalidSettingValueError


def validate_settings_command(
    command: SettingsPatchCommand,
) -> tuple[tuple[SettingKey, object, int], ...]:
    validated = []
    for update in command.updates:
        try:
            key = SettingKey(update.key)
        except ValueError as exc:
            raise KeyError(update.key) from exc
        validated.append((key, validate_setting(key, update.value), update.expected_revision))
    return tuple(sorted(validated, key=lambda item: item[0].value))


class DatabaseSettingStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_settings(self) -> Sequence[SettingView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT key, value, revision, updated_at
                        FROM digital_shelf.store_settings
                        ORDER BY key
                        """
                    )
                )
            ).mappings().all()
        return [
            SettingView(
                key=SettingKey(str(row["key"])),
                value=row["value"],
                revision=int(row["revision"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def update_settings(
        self,
        *,
        command: SettingsPatchCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> Sequence[SettingView]:
        validated = validate_settings_command(command)
        views: list[SettingView] = []
        async with self._engine.begin() as connection:
            for key, value, expected_revision in validated:
                current_revision = await connection.scalar(
                    text(
                        """
                        SELECT revision
                        FROM digital_shelf.store_settings
                        WHERE key = :key
                        FOR UPDATE
                        """
                    ),
                    {"key": key.value},
                )
                if current_revision is None:
                    if expected_revision != 0:
                        raise SettingRevisionConflictError
                    revision = 1
                    updated_at = await connection.scalar(
                        text(
                            """
                            INSERT INTO digital_shelf.store_settings
                                (key, value, revision, updated_by)
                            VALUES (:key, CAST(:value AS jsonb), :revision, :updated_by)
                            ON CONFLICT (key) DO NOTHING
                            RETURNING updated_at
                            """
                        ),
                        {
                            "key": key.value,
                            "value": json.dumps(value),
                            "revision": revision,
                            "updated_by": actor_admin_id,
                        },
                    )
                    if updated_at is None:
                        raise SettingRevisionConflictError
                else:
                    if int(current_revision) != expected_revision:
                        raise SettingRevisionConflictError
                    revision = expected_revision + 1
                    updated_at = await connection.scalar(
                        text(
                            """
                            UPDATE digital_shelf.store_settings
                            SET value = CAST(:value AS jsonb),
                                revision = :revision,
                                updated_by = :updated_by,
                                updated_at = now()
                            WHERE key = :key
                            RETURNING updated_at
                            """
                        ),
                        {
                            "key": key.value,
                            "value": json.dumps(value),
                            "revision": revision,
                            "updated_by": actor_admin_id,
                        },
                    )
                if not isinstance(updated_at, datetime):
                    raise RuntimeError("setting update did not return a timestamp")
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.store_setting_revisions
                            (setting_key, value, revision, changed_by)
                        VALUES (:key, CAST(:value AS jsonb), :revision, :changed_by)
                        """
                    ),
                    {
                        "key": key.value,
                        "value": json.dumps(value),
                        "revision": revision,
                        "changed_by": actor_admin_id,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.audit_events
                            (actor_admin_id, action, target_type, target_id, result,
                             detail, correlation_id)
                        VALUES
                            (:actor_admin_id, 'setting.update', 'setting', :target_id,
                             'succeeded', CAST(:detail AS jsonb), :correlation_id)
                        """
                    ),
                    {
                        "actor_admin_id": actor_admin_id,
                        "target_id": key.value,
                        "detail": json.dumps({"revision": revision}),
                        "correlation_id": correlation_id,
                    },
                )
                views.append(
                    SettingView(
                        key=key,
                        value=value,
                        revision=revision,
                        updated_at=updated_at,
                    )
                )
        return views
