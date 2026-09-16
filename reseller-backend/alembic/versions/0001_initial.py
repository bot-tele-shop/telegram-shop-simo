"""initial durable offline core

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def ts():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade():
    op.create_table("products", sa.Column("id", sa.String(36), primary_key=True), sa.Column("supplier_product_id", sa.String(128), unique=True, nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("retail_stars", sa.Integer, nullable=False), sa.Column("supplier_price", sa.Numeric(18, 4), nullable=False), sa.Column("supplier_currency", sa.String(8), nullable=False), sa.Column("enabled", sa.Boolean, nullable=False), sa.Column("availability_checked_at", sa.DateTime(timezone=True)), *ts())
    op.create_table("orders", sa.Column("id", sa.String(36), primary_key=True), sa.Column("public_ref", sa.String(48), unique=True, nullable=False), sa.Column("buyer_id", sa.BigInteger, nullable=False), sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id"), nullable=False), sa.Column("product_snapshot", sa.JSON, nullable=False), sa.Column("stars_price", sa.Integer, nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("invoice_expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("terms_version", sa.String(64), nullable=False), sa.Column("payment_state", sa.String(32), nullable=False), sa.Column("supplier_state", sa.String(32), nullable=False), sa.Column("delivery_state", sa.String(32), nullable=False), sa.Column("eligible_payment_id", sa.String(36)), *ts())
    op.create_index("ix_orders_buyer_id", "orders", ["buyer_id"])
    op.create_table("payments", sa.Column("id", sa.String(36), primary_key=True), sa.Column("telegram_charge_id", sa.String(200), unique=True, nullable=False), sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id")), sa.Column("buyer_id", sa.BigInteger, nullable=False), sa.Column("amount_stars", sa.Integer, nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("payload", sa.String(128), nullable=False), sa.Column("state", sa.String(32), nullable=False), sa.Column("review_reason", sa.String(128)), sa.Column("conflict_payload", sa.JSON), sa.Column("raw_event", sa.JSON, nullable=False), *ts())
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_table("purchases", sa.Column("id", sa.String(36), primary_key=True), sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), unique=True, nullable=False), sa.Column("idempotency_key", sa.String(64), unique=True, nullable=False), sa.Column("request", sa.JSON, nullable=False), sa.Column("credential_version", sa.String(64), nullable=False), sa.Column("state", sa.String(32), nullable=False), sa.Column("supplier_order_code", sa.String(128)), sa.Column("observed_debit", sa.Boolean, nullable=False), sa.Column("outcome", sa.JSON), sa.Column("encrypted_response_evidence", sa.Text), *ts())
    op.create_table("deliveries", sa.Column("id", sa.String(36), primary_key=True), sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), unique=True, nullable=False), sa.Column("buyer_id", sa.BigInteger, nullable=False), sa.Column("encrypted_content", sa.Text, nullable=False), sa.Column("state", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer, nullable=False), sa.Column("message_reference", sa.String(128)), sa.Column("retention_until", sa.DateTime(timezone=True)), *ts())
    op.create_table("jobs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False), sa.Column("kind", sa.String(32), nullable=False), sa.Column("state", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer, nullable=False), sa.Column("owner", sa.String(128)), sa.Column("claim_token", sa.String(36)), sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("last_error", sa.String(200)), *ts(), sa.UniqueConstraint("order_id", "kind", name="uq_job_order_kind"))
    op.create_index("ix_jobs_order_id", "jobs", ["order_id"])
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_claim_token", "jobs", ["claim_token"])
    op.create_table("audit", sa.Column("id", sa.String(36), primary_key=True), sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id")), sa.Column("event", sa.String(80), nullable=False), sa.Column("detail", sa.JSON, nullable=False), *ts())
    op.create_index("ix_audit_order_id", "audit", ["order_id"])


def downgrade():
    for table in ("audit", "jobs", "deliveries", "purchases", "payments", "orders", "products"):
        op.drop_table(table)
