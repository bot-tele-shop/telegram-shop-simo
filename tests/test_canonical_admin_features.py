from collections.abc import Sequence
from uuid import uuid4

from fastapi.testclient import TestClient

from digital_shelf.admin import AdminPrincipal, FeatureUpdateCommand, FeatureView
from digital_shelf.api import create_app
from digital_shelf.auth import AuthenticatedIdentity, AuthenticationError
from digital_shelf.config import Settings
from digital_shelf.features import FeatureKey, FeatureState


class FakeVerifier:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid

    async def verify(self, token: str) -> AuthenticatedIdentity:
        if not self.valid or token != "valid-token":
            raise AuthenticationError
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


class MemoryFeatureStore:
    def __init__(self) -> None:
        self.feature = FeatureView(
            feature_key=FeatureKey.LOW_STOCK_ALERTS,
            requested_enabled=True,
            state=FeatureState.SETUP_REQUIRED,
            config={},
            revision=1,
            missing=("owner notification destination", "non-negative stock threshold"),
        )

    async def list_features(self) -> Sequence[FeatureView]:
        return [self.feature]

    async def update_feature(
        self,
        *,
        feature: FeatureKey,
        command: FeatureUpdateCommand,
        actor_admin_id: object,
        correlation_id: object,
    ) -> FeatureView:
        del actor_admin_id, correlation_id
        self.feature = FeatureView(
            feature_key=feature,
            requested_enabled=command.requested_enabled,
            state=FeatureState.ENABLED,
            config=command.config,
            revision=command.expected_revision + 1,
            missing=(),
        )
        return self.feature


def build_client(
    *,
    permissions: frozenset[str] = frozenset({"*"}),
    valid_token: bool = True,
) -> TestClient:
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(valid=valid_token),
        admin_authorizer=FakeAuthorizer(permissions),
        feature_store=MemoryFeatureStore(),
    )
    return TestClient(app)


def test_admin_route_requires_bearer_token() -> None:
    with build_client() as client:
        response = client.get("/admin/v1/features")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_admin_token_returns_generic_401() -> None:
    with build_client(valid_token=False) as client:
        response = client.get(
            "/admin/v1/features", headers={"Authorization": "Bearer invalid-token"}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_authenticated_admin_without_permission_is_forbidden() -> None:
    with build_client(permissions=frozenset({"orders.read"})) as client:
        response = client.get(
            "/admin/v1/features", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}


def test_owner_can_read_minimized_feature_states() -> None:
    with build_client() as client:
        response = client.get(
            "/admin/v1/features", headers={"Authorization": "Bearer valid-token"}
        )

    assert response.status_code == 200
    assert response.json() == [
        {
            "feature_key": "low_stock_alerts",
            "requested_enabled": True,
            "state": "setup_required",
            "config": {},
            "revision": 1,
            "missing": ["owner notification destination", "non-negative stock threshold"],
        }
    ]
    assert len(response.headers["x-correlation-id"]) == 36


def test_owner_can_update_known_feature() -> None:
    with build_client() as client:
        response = client.patch(
            "/admin/v1/features/low_stock_alerts",
            headers={"Authorization": "Bearer valid-token"},
            json={
                "requested_enabled": True,
                "config": {"owner_destination": "123", "threshold": 3},
                "expected_revision": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == "enabled"
    assert response.json()["revision"] == 2


def test_unknown_feature_key_returns_400_without_calling_store() -> None:
    with build_client() as client:
        response = client.patch(
            "/admin/v1/features/wallet",
            headers={"Authorization": "Bearer valid-token"},
            json={"requested_enabled": True, "config": {}, "expected_revision": 1},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "unknown feature key"}
