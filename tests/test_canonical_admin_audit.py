from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from digital_shelf.admin import AdminPrincipal
from digital_shelf.admin_audit import AuditEventView
from digital_shelf.api import create_app
from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.config import Settings


class FakeVerifier:
    async def verify(self, token: str) -> AuthenticatedIdentity:
        assert token == "valid-token"
        return AuthenticatedIdentity(subject=uuid4(), email="owner@example.com")


class FakeAuthorizer:
    def __init__(self, permissions: frozenset[str]) -> None:
        self.permissions = permissions

    async def authorize(self, identity: AuthenticatedIdentity) -> AdminPrincipal:
        return AdminPrincipal(
            admin_id=uuid4(),
            subject=identity.subject,
            email=identity.email or "owner@example.com",
            roles=("owner",) if "*" in self.permissions else ("support",),
            permissions=self.permissions,
        )


class FakeAuditStore:
    def __init__(self) -> None:
        self.last_limit: int | None = None

    async def list_events(self, *, limit: int) -> Sequence[AuditEventView]:
        self.last_limit = limit
        return [
            AuditEventView(
                id=uuid4(),
                actor_admin_id=uuid4(),
                action="setting.update",
                target_type="setting",
                target_id="checkout_paused",
                result="succeeded",
                correlation_id=uuid4(),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]


def build_client(
    permissions: frozenset[str] = frozenset({"*"}),
) -> tuple[TestClient, FakeAuditStore]:
    audit_store = FakeAuditStore()
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(),
        admin_authorizer=FakeAuthorizer(permissions),
        audit_store=audit_store,
    )
    return TestClient(app), audit_store


def test_owner_can_read_bounded_minimized_audit_history() -> None:
    client, audit_store = build_client()
    with client:
        response = client.get(
            "/admin/v1/audit?limit=10", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 200
    assert audit_store.last_limit == 10
    assert response.json()[0]["action"] == "setting.update"
    assert "detail" not in response.json()[0]


def test_audit_history_requires_owner_permission() -> None:
    client, _ = build_client(frozenset({"settings.read"}))
    with client:
        response = client.get(
            "/admin/v1/audit", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 403


def test_audit_history_has_bounded_limit() -> None:
    client, _ = build_client()
    with client:
        response = client.get(
            "/admin/v1/audit?limit=1000", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 422
