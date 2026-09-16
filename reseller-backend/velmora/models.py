from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Product(Timestamped, Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    supplier_product_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    retail_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    supplier_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    availability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Timestamped, Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    public_ref: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    buyer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    product_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    stars_price: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="XTR", nullable=False)
    invoice_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    payment_state: Mapped[str] = mapped_column(String(32), default="UNPAID", nullable=False)
    supplier_state: Mapped[str] = mapped_column(String(32), default="NOT_STARTED", nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), default="NOT_READY", nullable=False)
    eligible_payment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Payment(Timestamped, Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    telegram_charge_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    buyer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payload: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    review_reason: Mapped[str | None] = mapped_column(String(128))
    conflict_payload: Mapped[dict | None] = mapped_column(JSON)
    raw_event: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Purchase(Timestamped, Base):
    __tablename__ = "purchases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    credential_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="IN_FLIGHT", nullable=False)
    supplier_order_code: Mapped[str | None] = mapped_column(String(128))
    observed_debit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outcome: Mapped[dict | None] = mapped_column(JSON)
    encrypted_response_evidence: Mapped[str | None] = mapped_column(Text)


class Delivery(Timestamped, Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    buyer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message_reference: Mapped[str | None] = mapped_column(String(128))
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Timestamped, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("order_id", "kind", name="uq_job_order_kind"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(128))
    claim_token: Mapped[str | None] = mapped_column(String(36), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))


class Audit(Timestamped, Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    event: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
