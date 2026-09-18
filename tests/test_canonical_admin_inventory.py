from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from digital_shelf.admin import AdminPrincipal
from digital_shelf.api import create_app
from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.config import Settings
from digital_shelf.inventory import InventoryImportPreview
from digital_shelf.inventory_admin import (
    InventoryImportResult,
    InventoryPolicyError,
    ProductNotFoundError,
)


class FakeVerifier:
    async def verify(self, token: str) -> AuthenticatedIdentity:
        assert token == "valid-token"
        return AuthenticatedIdentity(subject=uuid4(), email="owner@example.com")


class FakeAuthorizer:
    async def authorize(self, identity: AuthenticatedIdentity) -> AdminPrincipal:
        return AdminPrincipal(
            admin_id=uuid4(),
            subject=identity.subject,
            email=identity.email or "owner@example.com",
            roles=("owner",),
            permissions=frozenset({"*"}),
        )


class MemoryInventoryStore:
    def __init__(self) -> None:
        self.product_id = uuid4()
        self.preview_lines: list[str] | None = None
        self.commit_lines: list[str] | None = None

    async def preview(self, *, product_id: UUID, lines: Sequence[str]) -> InventoryImportPreview:
        if product_id != self.product_id:
            raise ProductNotFoundError
        self.preview_lines = list(lines)
        return InventoryImportPreview(
            total_lines=len(lines),
            accepted_count=1,
            duplicate_count=0,
            blank_count=0,
            invalid_count=0,
            issues=(),
        )

    async def commit(
        self,
        *,
        product_id: UUID,
        lines: Sequence[str],
        actor_admin_id: UUID,
        correlation_id: UUID,
    ) -> InventoryImportResult:
        del actor_admin_id, correlation_id
        if product_id != self.product_id:
            raise ProductNotFoundError
        self.commit_lines = list(lines)
        return InventoryImportResult(
            total_lines=len(lines),
            added_count=1,
            duplicate_count=0,
            blank_count=0,
            invalid_count=0,
            issues=(),
        )


def build_client() -> tuple[TestClient, MemoryInventoryStore]:
    inventory_store = MemoryInventoryStore()
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(),
        admin_authorizer=FakeAuthorizer(),
        inventory_store=inventory_store,
    )
    return TestClient(app), inventory_store


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def test_preview_returns_counts_without_stock_values() -> None:
    client, store = build_client()
    with client:
        response = client.post(
            f"/admin/v1/products/{store.product_id}/inventory/preview",
            headers=headers(),
            json={"lines": ["SECRET-CODE-001"]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "total_lines": 1,
        "accepted_count": 1,
        "duplicate_count": 0,
        "blank_count": 0,
        "invalid_count": 0,
        "issues": [],
    }
    assert "SECRET-CODE-001" not in response.text
    assert store.preview_lines == ["SECRET-CODE-001"]


def test_commit_requires_explicit_confirmation() -> None:
    client, store = build_client()
    with client:
        response = client.post(
            f"/admin/v1/products/{store.product_id}/inventory/commit",
            headers=headers(),
            json={"lines": ["SECRET-CODE-001"]},
        )

    assert response.status_code == 422
    assert store.commit_lines is None


def test_commit_returns_added_counts_and_forwards_lines_to_transactional_store() -> None:
    client, store = build_client()
    with client:
        response = client.post(
            f"/admin/v1/products/{store.product_id}/inventory/commit",
            headers=headers(),
            json={"lines": ["SECRET-CODE-001"], "confirm": True},
        )

    assert response.status_code == 200
    assert response.json()["added_count"] == 1
    assert "SECRET-CODE-001" not in response.text
    assert store.commit_lines == ["SECRET-CODE-001"]


def test_unknown_product_returns_bad_request() -> None:
    client, _ = build_client()
    with client:
        response = client.post(
            f"/admin/v1/products/{uuid4()}/inventory/preview",
            headers=headers(),
            json={"lines": ["code"]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "product not found"}


def test_non_stock_product_returns_bad_request() -> None:
    class NonStockStore(MemoryInventoryStore):
        async def preview(self, *, product_id: UUID, lines: Sequence[str]) -> InventoryImportPreview:
            del product_id, lines
            raise InventoryPolicyError

    store = NonStockStore()
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(),
        admin_authorizer=FakeAuthorizer(),
        inventory_store=store,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/admin/v1/products/{store.product_id}/inventory/preview",
            headers=headers(),
            json={"lines": ["code"]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "product does not use unique inventory"}
