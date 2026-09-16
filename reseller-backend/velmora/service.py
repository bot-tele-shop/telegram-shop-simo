from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Audit, Job, Order, Payment, Product


class NotFoundError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def _positive_stars(value: object) -> int:
    # bool is an int subclass, and neither it nor decimal/float values are Stars prices.
    if type(value) is not int or value <= 0:
        raise ValueError("Stars price must be a positive integer (not bool or float)")
    return value


def add_product(session: Session, supplier_id: str, stars: int, title: str = "Approved demo") -> Product:
    stars = _positive_stars(stars)
    product = Product(supplier_product_id=supplier_id, title=title, retail_stars=stars,
                      supplier_price=Decimal("1.0000"), supplier_currency="SIM", enabled=True,
                      availability_checked_at=now())
    session.add(product)
    session.flush()
    return product


def create_order(session: Session, buyer_id: int, product_id: str, terms: str = "demo-v1") -> Order:
    product = session.get(Product, product_id)
    if not product or not product.enabled:
        raise NotFoundError("approved product unavailable")
    # Defend against bad persisted/catalog data too, not only add_product callers.
    stars = _positive_stars(product.retail_stars)
    order = Order(public_ref=f"VM-{uuid4().hex[:16].upper()}", buyer_id=buyer_id, product_id=product.id,
                  product_snapshot={"supplier_product_id": product.supplier_product_id, "title": product.title},
                  stars_price=stars, currency="XTR", terms_version=terms,
                  invoice_expires_at=now() + timedelta(minutes=5))
    session.add(order)
    session.flush()
    session.add(Audit(order_id=order.id, event="ORDER_CREATED", detail={"buyer_id": buyer_id}))
    return order


def _event_dict(buyer_id: object, amount: object, currency: object, payload: object) -> dict:
    # JSON-safe original event representation. It remains evidence even for an unusable amount.
    return {"buyer_id": buyer_id, "amount": amount, "currency": currency, "payload": payload,
            "amount_type": type(amount).__name__}


def _stored_amount(amount: object) -> int:
    return amount if type(amount) is int else 0


def _payment_valid_for_order(order: Order, buyer_id: object, amount: object, currency: object) -> bool:
    return (type(buyer_id) is int and type(amount) is int and amount > 0 and
            buyer_id == order.buyer_id and amount == order.stars_price and currency == "XTR")


def _stop_unstarted_purchase_work(session: Session, order: Order, reason: str) -> None:
    # Order is already locked by the caller. Never assume a RUNNING network call is cancelable.
    jobs = session.scalars(select(Job).where(Job.order_id == order.id, Job.kind == "PURCHASE").with_for_update()).all()
    for job in jobs:
        if job.state == "QUEUED":
            job.state, job.last_error, job.lease_until = "CANCELLED", reason, None


def intake_payment(session: Session, *, charge_id: str, payload: str, buyer_id: int, amount: int,
                   currency: str = "XTR") -> Payment:
    """Internal simulated intake only; it makes no authenticity claim about a payment event."""
    event = _event_dict(buyer_id, amount, currency, payload)
    # Serialize equal charge IDs before their unique constraint is challenged, including concurrent intake.
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:charge))"), {"charge": charge_id})
    existing = session.scalar(select(Payment).where(Payment.telegram_charge_id == charge_id))
    if existing:
        prior = existing.raw_event or _event_dict(existing.buyer_id, existing.amount_stars, existing.currency, existing.payload)
        if prior != event:
            existing.conflict_payload = event
            existing.state = "REVIEW"
            existing.review_reason = "CONFLICTING_DUPLICATE_CHARGE"
            if existing.order_id:
                # Take locks in order->job order, matching every worker state checkpoint.
                order = session.scalar(select(Order).where(Order.id == existing.order_id).with_for_update())
                if order:
                    order.payment_state = "REVIEW"
                    _stop_unstarted_purchase_work(session, order, "payment charge conflict")
                    session.add(Audit(order_id=order.id, event="PAYMENT_CONFLICT", detail={"charge": charge_id, "purchase_cancelled": True}))
            else:
                session.add(Audit(order_id=None, event="PAYMENT_CONFLICT", detail={"charge": charge_id}))
        return existing

    order = session.scalar(select(Order).where(Order.public_ref == payload).with_for_update())
    stored_amount = _stored_amount(amount)
    if not order:
        payment = Payment(telegram_charge_id=charge_id, order_id=None, buyer_id=buyer_id if type(buyer_id) is int else 0,
                          amount_stars=stored_amount, currency=str(currency), payload=str(payload), state="REVIEW",
                          review_reason="UNKNOWN_ORDER", raw_event=event)
        session.add(payment)
        return payment

    valid = _payment_valid_for_order(order, buyer_id, amount, currency)
    late = now() > order.invoice_expires_at
    anomalous = type(amount) is not int or amount <= 0 or type(buyer_id) is not int
    state, reason = ("ELIGIBLE", None) if valid and not late and not order.eligible_payment_id else (
        "REVIEW", "ANOMALOUS_FINANCIAL_EVENT" if anomalous else "LATE_PAYMENT" if late else
        "DUPLICATE_ORDER_CHARGE" if valid else "MISMATCHED_PAYMENT")
    payment = Payment(telegram_charge_id=charge_id, order_id=order.id,
                      buyer_id=buyer_id if type(buyer_id) is int else 0, amount_stars=stored_amount,
                      currency=str(currency), payload=str(payload), state=state, review_reason=reason, raw_event=event)
    session.add(payment)
    session.flush()
    if state == "ELIGIBLE":
        order.eligible_payment_id = payment.id
        order.payment_state = "PAID"
        session.add(Job(order_id=order.id, kind="PURCHASE", state="QUEUED"))
        session.add(Audit(order_id=order.id, event="PAYMENT_ELIGIBLE", detail={"charge": charge_id}))
    else:
        session.add(Audit(order_id=order.id, event="PAYMENT_REVIEW", detail={"reason": reason}))
    return payment
