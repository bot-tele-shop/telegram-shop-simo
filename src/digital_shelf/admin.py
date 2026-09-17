"""Canonical admin authorization and feature-state persistence."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.features import FeatureKey, FeatureState, evaluate_feature


class AuthorizationError(Exception):
    """An authenticated identity is not an active canonical administrator."""


class FeatureNotFoundError(Exception):
    """A requested feature is not registered."""


class RevisionConflictError(Exception):
    """A write used a stale optimistic revision."""


@dataclass(frozen=True)
class AdminPrincipal:
    admin_id: UUID
    subject: UUID
    email: str
    roles: tuple[str, ...]
    permissions: frozenset[str]

    def allows(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


class AdminAuthorizer(Protocol):
    async def authorize(self, identity: AuthenticatedIdentity) -> AdminPrincipal: ...


class DatabaseAdminAuthorizer:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def authorize(self, identity: AuthenticatedIdentity) -> AdminPrincipal:
        query = text(
            """
            SELECT
                au.id,
                au.auth_subject,
                au.email,
                COALESCE(
                    array_agg(DISTINCT aur.role_name)
                        FILTER (WHERE aur.role_name IS NOT NULL),
                    ARRAY[]::text[]
                ) AS roles,
                COALESCE(
                    array_agg(DISTINCT rp.permission)
                        FILTER (WHERE rp.permission IS NOT NULL),
                    ARRAY[]::text[]
                ) AS permissions
            FROM digital_shelf.admin_users AS au
            LEFT JOIN digital_shelf.admin_user_roles AS aur ON aur.admin_user_id = au.id
            LEFT JOIN digital_shelf.role_permissions AS rp ON rp.role_name = aur.role_name
            WHERE au.auth_subject = :subject AND au.active = true
            GROUP BY au.id, au.auth_subject, au.email
            """
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query, {"subject": identity.subject})).mappings().one_or_none()
        if row is None:
            raise AuthorizationError
        return AdminPrincipal(
            admin_id=UUID(str(row["id"])),
            subject=UUID(str(row["auth_subject"])),
            email=str(row["email"]),
            roles=tuple(str(role) for role in row["roles"]),
            permissions=frozenset(str(permission) for permission in row["permissions"]),
        )


class FeatureUpdateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_enabled: bool
    config: dict[str, object]
    expected_revision: int = Field(ge=1)


class FeatureView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_key: FeatureKey
    requested_enabled: bool
    state: FeatureState
    config: dict[str, object]
    revision: int
    missing: tuple[str, ...]


class FeatureStore(Protocol):
    async def list_features(self) -> Sequence[FeatureView]: ...

    async def update_feature(
        self,
        *,
        feature: FeatureKey,
        command: FeatureUpdateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> FeatureView: ...


def _feature_view(row: dict[str, object]) -> FeatureView:
    feature = FeatureKey(str(row["feature_key"]))
    config_value = row["config"]
    config = dict(config_value) if isinstance(config_value, dict) else {}
    requested_enabled = bool(row["requested_enabled"])
    evaluation = evaluate_feature(
        feature,
        requested_enabled=requested_enabled,
        config=config,
        facts={},
    )
    return FeatureView(
        feature_key=feature,
        requested_enabled=requested_enabled,
        state=evaluation.state,
        config=config,
        revision=int(str(row["revision"])),
        missing=evaluation.missing,
    )


class DatabaseFeatureStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_features(self) -> Sequence[FeatureView]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT feature_key, requested_enabled, state, config, revision
                        FROM digital_shelf.feature_flags
                        ORDER BY feature_key
                        """
                    )
                )
            ).mappings().all()
        return [_feature_view(dict(row)) for row in rows]

    async def update_feature(
        self,
        *,
        feature: FeatureKey,
        command: FeatureUpdateCommand,
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> FeatureView:
        evaluation = evaluate_feature(
            feature,
            requested_enabled=command.requested_enabled,
            config=command.config,
            facts={},
        )
        new_revision = command.expected_revision + 1
        async with self._engine.begin() as connection:
            current_revision = await connection.scalar(
                text(
                    """
                    SELECT revision
                    FROM digital_shelf.feature_flags
                    WHERE feature_key = :feature_key
                    FOR UPDATE
                    """
                ),
                {"feature_key": feature.value},
            )
            if current_revision is None:
                raise FeatureNotFoundError
            if int(current_revision) != command.expected_revision:
                raise RevisionConflictError

            await connection.execute(
                text(
                    """
                    UPDATE digital_shelf.feature_flags
                    SET requested_enabled = :requested_enabled,
                        state = :state,
                        config = CAST(:config AS jsonb),
                        revision = :revision,
                        updated_by = :updated_by,
                        updated_at = now()
                    WHERE feature_key = :feature_key
                    """
                ),
                {
                    "requested_enabled": command.requested_enabled,
                    "state": evaluation.state.value,
                    "config": json.dumps(command.config, separators=(",", ":")),
                    "revision": new_revision,
                    "updated_by": actor_admin_id,
                    "feature_key": feature.value,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO digital_shelf.audit_events
                        (actor_admin_id, action, target_type, target_id, result,
                         detail, correlation_id)
                    VALUES
                        (:actor_admin_id, 'feature.update', 'feature', :target_id,
                         'succeeded', CAST(:detail AS jsonb), :correlation_id)
                    """
                ),
                {
                    "actor_admin_id": actor_admin_id,
                    "target_id": feature.value,
                    "detail": json.dumps(
                        {
                            "requested_enabled": command.requested_enabled,
                            "state": evaluation.state.value,
                            "revision": new_revision,
                        },
                        separators=(",", ":"),
                    ),
                    "correlation_id": correlation_id,
                },
            )

        return FeatureView(
            feature_key=feature,
            requested_enabled=command.requested_enabled,
            state=evaluation.state,
            config=command.config,
            revision=new_revision,
            missing=evaluation.missing,
        )
