"""Add canonical identity, settings, roles, feature states, and audit.

Revision ID: 0002_identity_settings_features
Revises: 0001_bootstrap
"""

from alembic import op

revision = "0002_identity_settings_features"
down_revision = "0001_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        """
        CREATE TABLE digital_shelf.users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            telegram_user_id bigint NOT NULL UNIQUE CHECK (telegram_user_id > 0),
            username text,
            display_name text,
            locale text NOT NULL DEFAULT 'en',
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'blocked')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE digital_shelf.admin_users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            auth_subject uuid NOT NULL UNIQUE,
            email text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT admin_users_email_normalized
                CHECK (email = lower(btrim(email)) AND position('@' IN email) > 1)
        )
        """,
        """
        CREATE UNIQUE INDEX uq_admin_users_email_lower
            ON digital_shelf.admin_users (lower(email))
        """,
        """
        CREATE TABLE digital_shelf.roles (
            name text PRIMARY KEY,
            description text NOT NULL
        )
        """,
        """
        CREATE TABLE digital_shelf.role_permissions (
            role_name text NOT NULL REFERENCES digital_shelf.roles(name) ON DELETE CASCADE,
            permission text NOT NULL,
            PRIMARY KEY (role_name, permission)
        )
        """,
        """
        CREATE TABLE digital_shelf.admin_user_roles (
            admin_user_id uuid NOT NULL
                REFERENCES digital_shelf.admin_users(id) ON DELETE CASCADE,
            role_name text NOT NULL REFERENCES digital_shelf.roles(name) ON DELETE RESTRICT,
            PRIMARY KEY (admin_user_id, role_name)
        )
        """,
        """
        CREATE TABLE digital_shelf.store_settings (
            key text PRIMARY KEY,
            value jsonb NOT NULL,
            revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
            updated_by uuid REFERENCES digital_shelf.admin_users(id) ON DELETE SET NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT store_settings_key_format
                CHECK (key ~ '^[a-z][a-z0-9_]{1,63}$')
        )
        """,
        """
        CREATE TABLE digital_shelf.store_setting_revisions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            setting_key text NOT NULL
                REFERENCES digital_shelf.store_settings(key) ON DELETE RESTRICT,
            value jsonb NOT NULL,
            revision bigint NOT NULL CHECK (revision > 0),
            changed_by uuid REFERENCES digital_shelf.admin_users(id) ON DELETE SET NULL,
            changed_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (setting_key, revision)
        )
        """,
        """
        CREATE TABLE digital_shelf.feature_flags (
            feature_key text PRIMARY KEY,
            requested_enabled boolean NOT NULL DEFAULT false,
            state text NOT NULL DEFAULT 'disabled'
                CHECK (state IN ('disabled', 'setup_required', 'enabled')),
            config jsonb NOT NULL DEFAULT '{}'::jsonb,
            revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
            updated_by uuid REFERENCES digital_shelf.admin_users(id) ON DELETE SET NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT feature_flags_known_key CHECK (feature_key IN (
                'offers', 'referrals', 'reseller_api', 'restock_announcements',
                'low_stock_alerts', 'localization'
            )),
            CONSTRAINT feature_flags_state_matches_request CHECK (
                (requested_enabled AND state IN ('setup_required', 'enabled')) OR
                (NOT requested_enabled AND state = 'disabled')
            )
        )
        """,
        """
        CREATE TABLE digital_shelf.audit_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_admin_id uuid
                REFERENCES digital_shelf.admin_users(id) ON DELETE SET NULL,
            action text NOT NULL,
            target_type text,
            target_id text,
            result text NOT NULL CHECK (result IN ('succeeded', 'denied', 'failed')),
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            correlation_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE INDEX ix_audit_events_created_at
            ON digital_shelf.audit_events (created_at DESC)
        """,
        """
        CREATE INDEX ix_audit_events_target
            ON digital_shelf.audit_events (target_type, target_id, created_at DESC)
        """,
        """
        INSERT INTO digital_shelf.roles (name, description) VALUES
            ('owner', 'Full store administration'),
            ('operations', 'Catalog, inventory, orders, and delivery operations'),
            ('support', 'Customer and order support without financial administration')
        """,
        """
        INSERT INTO digital_shelf.role_permissions (role_name, permission) VALUES
            ('owner', '*'),
            ('operations', 'catalog.manage'),
            ('operations', 'inventory.manage'),
            ('operations', 'orders.read'),
            ('operations', 'delivery.retry'),
            ('support', 'customers.read'),
            ('support', 'orders.read'),
            ('support', 'support.manage')
        """,
        """
        INSERT INTO digital_shelf.feature_flags
            (feature_key, requested_enabled, state, config)
        VALUES
            ('offers', false, 'disabled', '{}'::jsonb),
            ('referrals', false, 'disabled', '{}'::jsonb),
            ('reseller_api', false, 'disabled', '{}'::jsonb),
            ('restock_announcements', false, 'disabled', '{}'::jsonb),
            ('low_stock_alerts', true, 'setup_required', '{}'::jsonb),
            ('localization', false, 'disabled', '{}'::jsonb)
        """,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    tables = (
        "audit_events",
        "feature_flags",
        "store_setting_revisions",
        "store_settings",
        "admin_user_roles",
        "role_permissions",
        "roles",
        "admin_users",
        "users",
    )
    for table in tables:
        op.execute(f"DROP TABLE digital_shelf.{table}")
