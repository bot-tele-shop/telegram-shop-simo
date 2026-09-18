"""Add canonical encrypted inventory items and quantity counters.

Revision ID: 0004_inventory
Revises: 0003_catalog
"""

from alembic import op

revision = "0004_inventory"
down_revision = "0003_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE digital_shelf.inventory_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            product_id uuid NOT NULL
                REFERENCES digital_shelf.products(id) ON DELETE RESTRICT,
            fingerprint text NOT NULL,
            ciphertext bytea NOT NULL,
            encryption_key_version text NOT NULL,
            state text NOT NULL DEFAULT 'available'
                CHECK (state IN ('available', 'reserved', 'sold', 'quarantined', 'retired')),
            reserved_until timestamptz,
            assigned_order_item_id uuid UNIQUE,
            reserved_at timestamptz,
            sold_at timestamptz,
            quarantined_at timestamptz,
            retired_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT inventory_items_fingerprint_format
                CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
            CONSTRAINT inventory_items_key_version_present
                CHECK (length(btrim(encryption_key_version)) BETWEEN 1 AND 64),
            CONSTRAINT inventory_items_state_reservation CHECK (
                (state = 'reserved' AND reserved_until IS NOT NULL) OR
                (state <> 'reserved' AND reserved_until IS NULL)
            )
        )
        """,
        """
        CREATE UNIQUE INDEX uq_inventory_items_product_fingerprint
            ON digital_shelf.inventory_items (product_id, fingerprint)
        """,
        """
        CREATE INDEX ix_inventory_items_available
            ON digital_shelf.inventory_items (product_id, id)
            WHERE state = 'available'
        """,
        """
        CREATE INDEX ix_inventory_items_reserved_expiry
            ON digital_shelf.inventory_items (reserved_until)
            WHERE state = 'reserved'
        """,
        """
        CREATE TABLE digital_shelf.inventory_counters (
            product_id uuid PRIMARY KEY
                REFERENCES digital_shelf.products(id) ON DELETE CASCADE,
            available_quantity bigint NOT NULL DEFAULT 0 CHECK (available_quantity >= 0),
            reserved_quantity bigint NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
            sold_quantity bigint NOT NULL DEFAULT 0 CHECK (sold_quantity >= 0),
            version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT inventory_counters_total_non_negative CHECK (
                available_quantity + reserved_quantity + sold_quantity >= 0
            )
        )
        """,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for table in ("inventory_counters", "inventory_items"):
        op.execute(f"DROP TABLE digital_shelf.{table}")
