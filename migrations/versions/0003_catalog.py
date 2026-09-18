"""Add canonical categories, products, and private product assets.

Revision ID: 0003_catalog
Revises: 0002_identity_settings_features
"""

from alembic import op

revision = "0003_catalog"
down_revision = "0002_identity_settings_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE digital_shelf.categories (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            parent_id uuid REFERENCES digital_shelf.categories(id) ON DELETE RESTRICT,
            slug text NOT NULL,
            name text NOT NULL,
            description text NOT NULL DEFAULT '',
            emoji text,
            position integer NOT NULL DEFAULT 0 CHECK (position >= 0),
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT categories_slug_normalized
                CHECK (
                    length(btrim(slug)) BETWEEN 1 AND 64
                    AND slug = lower(btrim(slug))
                    AND slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
                ),
            CONSTRAINT categories_name_present CHECK (length(btrim(name)) BETWEEN 1 AND 80)
        )
        """,
        """
        CREATE UNIQUE INDEX uq_categories_slug_lower
            ON digital_shelf.categories (lower(slug))
        """,
        """
        CREATE INDEX ix_categories_active_position
            ON digital_shelf.categories (active, position, name)
        """,
        """
        CREATE TABLE digital_shelf.products (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            category_id uuid NOT NULL
                REFERENCES digital_shelf.categories(id) ON DELETE RESTRICT,
            sku text NOT NULL,
            title text NOT NULL,
            description text NOT NULL,
            price_stars bigint NOT NULL CHECK (price_stars > 0),
            currency text NOT NULL DEFAULT 'XTR' CHECK (currency = 'XTR'),
            status text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'ready', 'available', 'paused', 'archived')),
            fulfillment_type text NOT NULL CHECK (fulfillment_type IN (
                'unique_code', 'unique_url', 'download_file', 'reusable_content',
                'manual', 'subscription_access'
            )),
            inventory_policy text NOT NULL CHECK (inventory_policy IN (
                'finite_unique', 'finite_quantity', 'unlimited', 'manual'
            )),
            warranty_text text,
            delivery_text text,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT products_sku_normalized
                CHECK (sku = upper(btrim(sku)) AND sku ~ '^[A-Z0-9][A-Z0-9._-]{0,63}$'),
            CONSTRAINT products_title_present CHECK (length(btrim(title)) BETWEEN 1 AND 160),
            CONSTRAINT products_description_present CHECK (length(btrim(description)) BETWEEN 1 AND 10000),
            CONSTRAINT products_policy_pair CHECK (
                (fulfillment_type IN ('unique_code', 'unique_url')
                    AND inventory_policy = 'finite_unique') OR
                (fulfillment_type = 'download_file'
                    AND inventory_policy IN ('finite_unique', 'unlimited')) OR
                (fulfillment_type = 'reusable_content' AND inventory_policy = 'unlimited') OR
                (fulfillment_type = 'manual' AND inventory_policy = 'manual') OR
                (fulfillment_type = 'subscription_access'
                    AND inventory_policy IN ('manual', 'unlimited'))
            )
        )
        """,
        """
        CREATE UNIQUE INDEX uq_products_sku
            ON digital_shelf.products (sku)
        """,
        """
        CREATE INDEX ix_products_active_category
            ON digital_shelf.products (category_id, status, title)
            WHERE status IN ('ready', 'available', 'paused')
        """,
        """
        CREATE INDEX ix_products_search
            ON digital_shelf.products (lower(title), sku)
        """,
        """
        CREATE TABLE digital_shelf.product_assets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id uuid NOT NULL
                REFERENCES digital_shelf.products(id) ON DELETE CASCADE,
            asset_role text NOT NULL DEFAULT 'image'
                CHECK (asset_role IN ('image', 'preview', 'download', 'icon')),
            storage_key text,
            telegram_file_id text,
            checksum text,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT product_assets_one_reference CHECK (
                (storage_key IS NOT NULL AND telegram_file_id IS NULL) OR
                (storage_key IS NULL AND telegram_file_id IS NOT NULL)
            ),
            CONSTRAINT product_assets_private_storage CHECK (
                storage_key IS NULL OR (
                    length(btrim(storage_key)) > 0
                    AND
                    storage_key !~ '://'
                    AND storage_key !~ '^/'
                    AND storage_key !~ '[[:cntrl:]]'
                )
            ),
            CONSTRAINT product_assets_file_id_present CHECK (
                telegram_file_id IS NULL OR length(btrim(telegram_file_id)) > 0
            )
        )
        """,
        """
        CREATE INDEX ix_product_assets_active
            ON digital_shelf.product_assets (product_id, asset_role)
            WHERE active = true
        """,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for table in ("product_assets", "products", "categories"):
        op.execute(f"DROP TABLE digital_shelf.{table}")
