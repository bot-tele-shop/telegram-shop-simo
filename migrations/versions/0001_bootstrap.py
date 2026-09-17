"""Create the namespace for canonical application tables.

Revision ID: 0001_bootstrap
Revises: none
"""

from alembic import op

revision = "0001_bootstrap"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS digital_shelf")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS digital_shelf")
