from collections.abc import Sequence
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from digital_shelf.admin import AdminPrincipal
from digital_shelf.api import create_app
from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.catalog import CategoryCreateCommand, ProductCreateCommand
from digital_shelf.catalog_admin import (
    CategoryView,
    ProductConflictError,
    ProductNotFoundError,
    ProductView,
)
from digital_shelf.config import Settings


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


class MemoryCatalogStore:
    def __init__(self) -> None:
        self.category_id = uuid4()
        self.categories = [
            CategoryView(
                id=self.category_id,
                parent_id=None,
                slug="ai-tools",
                name="AI Tools",
                description="",
                emoji="🤖",
                position=0,
                active=True,
            )
        ]
        self.products: list[ProductView] = []

    async def list_categories(self) -> Sequence[CategoryView]:
        return self.categories

    async def create_category(
        self, *, command: CategoryCreateCommand, actor_admin_id: UUID, correlation_id: UUID
    ) -> CategoryView:
        del actor_admin_id, correlation_id
        view = CategoryView(
            id=uuid4(),
            parent_id=command.parent_id,
            slug=command.slug,
            name=command.name,
            description=command.description,
            emoji=command.emoji,
            position=command.position,
            active=command.active,
        )
        self.categories.append(view)
        return view

    async def list_products(self) -> Sequence[ProductView]:
        return self.products

    async def create_product(
        self, *, command: ProductCreateCommand, actor_admin_id: UUID, correlation_id: UUID
    ) -> ProductView:
        del actor_admin_id, correlation_id
        if command.category_id not in {category.id for category in self.categories}:
            raise ProductNotFoundError
        if any(product.sku == command.sku for product in self.products):
            raise ProductConflictError
        view = ProductView(
            id=uuid4(),
            category_id=command.category_id,
            sku=command.sku,
            title=command.title,
            description=command.description,
            price_stars=command.price_stars,
            currency="XTR",
            status=command.status,
            fulfillment_type=command.fulfillment_type,
            inventory_policy=command.inventory_policy,
            warranty_text=command.warranty_text,
            delivery_text=command.delivery_text,
            metadata=command.metadata,
            version=1,
        )
        self.products.append(view)
        return view


def build_client() -> tuple[TestClient, MemoryCatalogStore]:
    catalog_store = MemoryCatalogStore()
    app = create_app(
        Settings(environment="test"),
        jwt_verifier=FakeVerifier(),
        admin_authorizer=FakeAuthorizer(),
        catalog_store=catalog_store,
    )
    return TestClient(app), catalog_store


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def test_owner_can_list_categories_and_products() -> None:
    client, _ = build_client()
    with client:
        categories = client.get("/admin/v1/categories", headers=auth_headers())
        products = client.get("/admin/v1/products", headers=auth_headers())

    assert categories.status_code == 200
    assert categories.json()[0]["slug"] == "ai-tools"
    assert products.status_code == 200
    assert products.json() == []


def test_owner_can_create_category_with_normalized_identity() -> None:
    client, _ = build_client()
    with client:
        response = client.post(
            "/admin/v1/categories",
            headers=auth_headers(),
            json={"slug": "  subscriptions ", "name": " Subscriptions ", "position": 1},
        )

    assert response.status_code == 201
    assert response.json()["slug"] == "subscriptions"
    assert response.json()["name"] == "Subscriptions"


def test_owner_can_create_product_and_get_customer_safe_view() -> None:
    client, store = build_client()
    with client:
        response = client.post(
            "/admin/v1/products",
            headers=auth_headers(),
            json={
                "category_id": str(store.category_id),
                "sku": " chatgpt-plus ",
                "title": " ChatGPT Plus ",
                "description": "A subscription code",
                "price_stars": 50,
                "fulfillment_type": "unique_code",
                "inventory_policy": "finite_unique",
            },
        )

    assert response.status_code == 201
    assert response.json()["sku"] == "CHATGPT-PLUS"
    assert response.json()["status"] == "draft"
    assert "inventory" not in response.json()


def test_duplicate_product_sku_returns_conflict() -> None:
    client, store = build_client()
    payload = {
        "category_id": str(store.category_id),
        "sku": "item",
        "title": "Item",
        "description": "A reusable item",
        "price_stars": 1,
        "fulfillment_type": "reusable_content",
        "inventory_policy": "unlimited",
    }
    with client:
        first = client.post("/admin/v1/products", headers=auth_headers(), json=payload)
        second = client.post("/admin/v1/products", headers=auth_headers(), json=payload)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"detail": "product SKU already exists"}


def test_unknown_category_returns_bad_request() -> None:
    client, _ = build_client()
    with client:
        response = client.post(
            "/admin/v1/products",
            headers=auth_headers(),
            json={
                "category_id": str(uuid4()),
                "sku": "item",
                "title": "Item",
                "description": "A manual item",
                "price_stars": 1,
                "fulfillment_type": "manual",
                "inventory_policy": "manual",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "category not found"}
