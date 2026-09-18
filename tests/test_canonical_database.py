"""Real PostgreSQL evidence for the canonical Slice 0 foundation."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from digital_shelf.admin import (
    DatabaseAdminAuthorizer,
    DatabaseFeatureStore,
    FeatureUpdateCommand,
)
from digital_shelf.admin_audit import DatabaseAuditStore
from digital_shelf.auth import AuthenticatedIdentity
from digital_shelf.catalog import CategoryCreateCommand, ProductCreateCommand
from digital_shelf.catalog_admin import DatabaseCatalogStore
from digital_shelf.db import create_engine, database_ready
from digital_shelf.features import FeatureKey, FeatureState
from digital_shelf.inventory import InventoryCipher
from digital_shelf.inventory_admin import (
    DatabaseInventoryAllocator,
    DatabaseInventoryStore,
    InventoryUnavailableError,
)
from digital_shelf.store_settings import (
    DatabaseSettingStore,
    SettingRevisionConflictError,
    SettingsPatchCommand,
    SettingUpdate,
)


@pytest.mark.integration
def test_bootstrap_migration_and_readiness_against_postgres() -> None:
    database_url = os.environ.get("SHOP_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SHOP_TEST_DATABASE_URL is not configured")

    async def verify() -> None:
        engine = create_engine(database_url)
        try:
            assert await database_ready(engine)
            async with engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                schema = await connection.scalar(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = 'digital_shelf'"
                    )
                )
                table_names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = 'digital_shelf'"
                            )
                        )
                    ).scalars()
                )
                feature_rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT feature_key, requested_enabled, state, revision "
                                "FROM digital_shelf.feature_flags ORDER BY feature_key"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert revision == "0004_inventory"
            assert schema == "digital_shelf"
            assert {
                "users",
                "admin_users",
                "roles",
                "role_permissions",
                "admin_user_roles",
                "store_settings",
                "store_setting_revisions",
                "feature_flags",
                "audit_events",
                "categories",
                "products",
                "product_assets",
                "inventory_items",
                "inventory_counters",
            } <= table_names
            by_key = {row["feature_key"]: row for row in feature_rows}
            assert by_key["offers"]["state"] == "disabled"
            assert dict(by_key["low_stock_alerts"]) == {
                "feature_key": "low_stock_alerts",
                "requested_enabled": True,
                "state": "setup_required",
                "revision": 1,
            }

            subject = uuid4()
            async with engine.begin() as connection:
                admin_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO digital_shelf.admin_users (auth_subject, email)
                        VALUES (:subject, :email)
                        RETURNING id
                        """
                    ),
                    {"subject": subject, "email": f"{subject}@example.com"},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.admin_user_roles (admin_user_id, role_name)
                        VALUES (:admin_id, 'owner')
                        """
                    ),
                    {"admin_id": admin_id},
                )

            principal = await DatabaseAdminAuthorizer(engine).authorize(
                AuthenticatedIdentity(subject=subject, email=f"{subject}@example.com")
            )
            assert principal.admin_id == admin_id
            assert principal.allows("features.manage")

            updated = await DatabaseFeatureStore(engine).update_feature(
                feature=FeatureKey.LOW_STOCK_ALERTS,
                command=FeatureUpdateCommand(
                    requested_enabled=True,
                    config={"owner_destination": "123", "threshold": 3},
                    expected_revision=1,
                ),
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            assert updated.state is FeatureState.ENABLED
            assert updated.revision == 2

            async with engine.connect() as connection:
                persisted_state = await connection.scalar(
                    text(
                        """
                        SELECT state FROM digital_shelf.feature_flags
                        WHERE feature_key = 'low_stock_alerts'
                        """
                    )
                )
                audit_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM digital_shelf.audit_events
                        WHERE action = 'feature.update'
                          AND actor_admin_id = :admin_id
                        """
                    ),
                    {"admin_id": principal.admin_id},
                )
            assert persisted_state == "enabled"
            assert audit_count == 1

            setting_store = DatabaseSettingStore(engine)
            updated_settings = await setting_store.update_settings(
                command=SettingsPatchCommand(
                    updates=[
                        SettingUpdate(
                            key="checkout_paused",
                            value=True,
                            expected_revision=0,
                        ),
                        SettingUpdate(
                            key="shop_name",
                            value="Digital Shelf",
                            expected_revision=0,
                        ),
                    ]
                ),
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            assert [(item.key.value, item.revision) for item in updated_settings] == [
                ("checkout_paused", 1),
                ("shop_name", 1),
            ]

            with pytest.raises(SettingRevisionConflictError):
                await setting_store.update_settings(
                    command=SettingsPatchCommand(
                        updates=[
                            SettingUpdate(
                                key="checkout_paused",
                                value=False,
                                expected_revision=1,
                            ),
                            SettingUpdate(
                                key="support_contact",
                                value="@StoreSupport",
                                expected_revision=99,
                            ),
                        ]
                    ),
                    actor_admin_id=principal.admin_id,
                    correlation_id=uuid4(),
                )

            async with engine.connect() as connection:
                checkout_row = (
                    await connection.execute(
                        text(
                            """
                            SELECT value, revision FROM digital_shelf.store_settings
                            WHERE key = 'checkout_paused'
                            """
                        )
                    )
                ).mappings().one()
                setting_audit_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM digital_shelf.audit_events
                        WHERE action = 'setting.update'
                          AND actor_admin_id = :admin_id
                        """
                    ),
                    {"admin_id": principal.admin_id},
                )
            assert checkout_row["value"] is True
            assert checkout_row["revision"] == 1
            assert setting_audit_count == 2

            recent_audit = await DatabaseAuditStore(engine).list_events(limit=10)
            assert len(recent_audit) == 3
            assert recent_audit[0].action == "setting.update"
            assert "detail" not in recent_audit[0].model_dump()

            category_id = uuid4()
            product_id = uuid4()
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.categories (id, slug, name)
                        VALUES (:id, 'ai-tools', 'AI Tools')
                        """
                    ),
                    {"id": category_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO digital_shelf.products
                            (id, category_id, sku, title, description, price_stars,
                             fulfillment_type, inventory_policy)
                        VALUES
                            (:id, :category_id, 'CHATGPT-PLUS', 'ChatGPT Plus',
                             'Access', 50, 'unique_code', 'finite_unique')
                        """
                    ),
                    {"id": product_id, "category_id": category_id},
                )

            with pytest.raises(Exception):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO digital_shelf.products
                                (category_id, sku, title, description, price_stars,
                                 fulfillment_type, inventory_policy)
                            VALUES
                                (:category_id, 'INVALID', 'Invalid', 'Invalid', 1,
                                 'unique_code', 'unlimited')
                            """
                        ),
                        {"category_id": category_id},
                    )

            async with engine.connect() as connection:
                active_product_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM digital_shelf.products
                        WHERE id = :product_id AND status = 'draft'
                        """
                    ),
                    {"product_id": product_id},
                )
            assert active_product_count == 1

            catalog_store = DatabaseCatalogStore(engine)
            managed_category = await catalog_store.create_category(
                command=CategoryCreateCommand(slug="managed", name="Managed"),
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            managed_product = await catalog_store.create_product(
                command=ProductCreateCommand(
                    category_id=managed_category.id,
                    sku="notion-template",
                    title="Notion Template",
                    description="Reusable template",
                    price_stars=10,
                    fulfillment_type="reusable_content",
                    inventory_policy="unlimited",
                ),
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            assert managed_product.sku == "NOTION-TEMPLATE"
            assert len(await catalog_store.list_categories()) == 2
            assert len(await catalog_store.list_products()) == 2

            async with engine.connect() as connection:
                catalog_audit_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM digital_shelf.audit_events
                        WHERE actor_admin_id = :admin_id
                          AND action IN ('category.create', 'product.create')
                        """
                    ),
                    {"admin_id": principal.admin_id},
                )
            assert catalog_audit_count == 2

            inventory_cipher = InventoryCipher(
                key=Fernet.generate_key().decode(), key_version="v1"
            )
            inventory_store = DatabaseInventoryStore(engine, inventory_cipher)
            import_result = await inventory_store.commit(
                product_id=product_id,
                lines=["LICENSE-001"],
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            assert import_result.added_count == 1
            duplicate_result = await inventory_store.commit(
                product_id=product_id,
                lines=["LICENSE-001"],
                actor_admin_id=principal.admin_id,
                correlation_id=uuid4(),
            )
            assert duplicate_result.duplicate_count == 1
            async with engine.connect() as connection:
                inventory_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*) FROM digital_shelf.inventory_items
                        WHERE product_id = :product_id AND state = 'available'
                        """
                    ),
                    {"product_id": product_id},
                )
                available_quantity = await connection.scalar(
                    text(
                        """
                        SELECT available_quantity FROM digital_shelf.inventory_counters
                        WHERE product_id = :product_id
                        """
                    ),
                    {"product_id": product_id},
                )
            assert inventory_count == 1
            assert available_quantity == 1

            allocator = DatabaseInventoryAllocator(engine)
            item_id = None
            async with engine.connect() as connection:
                item_id = await connection.scalar(
                    text(
                        """
                        SELECT id FROM digital_shelf.inventory_items
                        WHERE product_id = :product_id AND state = 'available'
                        """
                    ),
                    {"product_id": product_id},
                )
            assert item_id is not None
            order_item_id = uuid4()
            reservation = await allocator.reserve_one(
                product_id=product_id,
                order_item_id=order_item_id,
                reserved_until=datetime.now(UTC) + timedelta(minutes=5),
                correlation_id=uuid4(),
            )
            assert reservation.item_id == item_id
            with pytest.raises(InventoryUnavailableError):
                await allocator.reserve_one(
                    product_id=product_id,
                    order_item_id=uuid4(),
                    reserved_until=datetime.now(UTC) + timedelta(minutes=5),
                    correlation_id=uuid4(),
                )
            await allocator.confirm_sale(
                item_id=reservation.item_id,
                order_item_id=order_item_id,
                correlation_id=uuid4(),
            )
            await allocator.quarantine_sold(
                item_id=reservation.item_id,
                correlation_id=uuid4(),
            )
            async with engine.connect() as connection:
                final_state = await connection.scalar(
                    text(
                        """
                        SELECT state FROM digital_shelf.inventory_items WHERE id = :item_id
                        """
                    ),
                    {"item_id": reservation.item_id},
                )
                counters = (
                    await connection.execute(
                        text(
                            """
                            SELECT available_quantity, reserved_quantity, sold_quantity
                            FROM digital_shelf.inventory_counters WHERE product_id = :product_id
                            """
                        ),
                        {"product_id": product_id},
                    )
                ).mappings().one()
            assert final_state == "quarantined"
            assert dict(counters) == {
                "available_quantity": 0,
                "reserved_quantity": 0,
                "sold_quantity": 1,
            }
        finally:
            await engine.dispose()

    asyncio.run(verify())
