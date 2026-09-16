from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Audit, Delivery, Job, Order, Payment, Purchase
from .service import now
from .supplier import SimulatedSupplier, SupplierTimeout


class SimulatedMessenger:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[int, str]] = []

    def send(self, buyer_id: int, content: str) -> str:
        if self.fail:
            raise TimeoutError("simulated message failure")
        self.sent.append((buyer_id, content))
        return f"SIM-MSG-{len(self.sent)}"


@dataclass(frozen=True)
class Claim:
    job_id: str
    token: str


class Worker:
    def __init__(self, sessions: sessionmaker[Session], supplier: SimulatedSupplier, fernet_key: str,
                 messenger: SimulatedMessenger, owner: str):
        self.sessions, self.supplier, self.box, self.messenger, self.owner = (
            sessions, supplier, Fernet(fernet_key.encode()), messenger, owner)

    def claim_one(self) -> Claim | None:
        with self.sessions.begin() as s:
            cutoff = now()
            job = s.scalar(select(Job).where(or_(Job.state == "QUEUED", (Job.state == "RUNNING") & (Job.lease_until < cutoff)))
                           .order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
            if not job:
                return None
            token = str(uuid4())
            job.state, job.owner, job.claim_token, job.lease_until, job.attempts = (
                "RUNNING", self.owner, token, cutoff + timedelta(seconds=30), job.attempts + 1)
            return Claim(job.id, token)

    def process_one(self, checkpoint_only: bool = False) -> str | None:
        claim = self.claim_one()
        if not claim:
            return None
        with self.sessions() as s:
            job = s.get(Job, claim.job_id)
            kind = job.kind
        return self._purchase(claim, checkpoint_only) if kind == "PURCHASE" else self._deliver(claim)

    @staticmethod
    def _claimed(job: Job, token: str) -> bool:
        return job.state == "RUNNING" and job.claim_token == token and job.lease_until is not None and job.lease_until > now()

    @staticmethod
    def _lock_order_then_job(s: Session, job_id: str) -> tuple[Order, Job] | None:
        # Fetching the FK is lock-free; all actual row-lock acquisition is consistently order then job.
        job_snapshot = s.get(Job, job_id)
        if not job_snapshot:
            return None
        order = s.scalar(select(Order).where(Order.id == job_snapshot.order_id).with_for_update())
        job = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
        return (order, job) if order and job else None

    def _hold(self, claim: Claim, state: str, reason: str) -> str:
        with self.sessions.begin() as s:
            locked = self._lock_order_then_job(s, claim.job_id)
            if not locked:
                return "STALE_CLAIM"
            order, job = locked
            if not self._claimed(job, claim.token):
                return "STALE_CLAIM"
            job.state, job.last_error, job.lease_until = "HOLD", reason, None
            order.supplier_state = state
            s.add(Audit(order_id=order.id, event="PURCHASE_HELD", detail={"reason": reason}))
        return "HOLD"

    @staticmethod
    def _payment_is_currently_eligible(s: Session, order: Order) -> bool:
        if order.payment_state != "PAID" or not order.eligible_payment_id:
            return False
        payment = s.scalar(select(Payment).where(Payment.id == order.eligible_payment_id).with_for_update())
        return bool(payment and payment.state == "ELIGIBLE" and payment.order_id == order.id and
                    payment.buyer_id == order.buyer_id and payment.amount_stars == order.stars_price and
                    payment.currency == order.currency == "XTR")

    def _purchase(self, claim: Claim, checkpoint_only: bool) -> str:
        # Save identity/body before supplier work. The order lock serializes identity creation.
        with self.sessions.begin() as s:
            locked = self._lock_order_then_job(s, claim.job_id)
            if not locked:
                return "STALE_CLAIM"
            order, job = locked
            if not self._claimed(job, claim.token):
                return "STALE_CLAIM"
            if not self._payment_is_currently_eligible(s, order):
                job.state, job.last_error, job.lease_until = "HOLD", "selected payment is not eligible", None
                s.add(Audit(order_id=order.id, event="PURCHASE_BLOCKED", detail={"reason": "payment_not_eligible"}))
                return "HOLD"
            purchase = s.scalar(select(Purchase).where(Purchase.order_id == order.id).with_for_update())
            if purchase:
                # A stale/reclaimed job must never turn a recorded receipt back into uncertainty.
                if purchase.state == "COMPLETED":
                    job.state, job.last_error, job.lease_until = "DONE", None, None
                    return "ALREADY_COMPLETED"
                # A reclaimed request is evidence of uncertainty, never a reason to mint/replay a purchase.
                job.state, job.last_error, job.lease_until = "HOLD", "existing purchase requires review", None
                order.supplier_state = "UNCERTAIN"
                s.add(Audit(order_id=order.id, event="PURCHASE_HELD", detail={"reason": job.last_error}))
                return "HOLD"
            purchase = Purchase(order_id=order.id, idempotency_key=str(uuid4()),
                                request={"product_id": order.product_snapshot["supplier_product_id"], "quantity": 1},
                                credential_version="SIMULATED-NOT-REAL-v1", state="IN_FLIGHT")
            s.add(purchase)
            order.supplier_state = "IN_FLIGHT"
            s.add(Audit(order_id=order.id, event="PURCHASE_IDENTIFIED", detail={"purchase_id": purchase.id}))
            s.flush()
            order_id, key, request = order.id, purchase.idempotency_key, dict(purchase.request)
        if checkpoint_only:
            return "CHECKPOINT_SAVED"
        # No PostgreSQL transaction or row lock is held across this mock network boundary.
        try:
            result = self.supplier.purchase(key, request)
        except SupplierTimeout:
            return self._hold(claim, "UNCERTAIN", "timeout after possible supplier debit")
        return self._commit_purchase_result(claim, order_id, result)

    def _result_evidence(self, result: object) -> str:
        # Evidence may include a simulated delivery value; retain it encrypted and never in audit/log fields.
        data = json.dumps({"status": result.status, "debited": result.debited,
                           "order_code": result.order_code, "delivery": result.delivery}, sort_keys=True)
        return self.box.encrypt(data.encode()).decode()

    def _commit_purchase_result(self, claim: Claim, order_id: str, result: object) -> str:
        with self.sessions.begin() as s:
            locked = self._lock_order_then_job(s, claim.job_id)
            if not locked:
                return "STALE_CLAIM"
            order, job = locked
            purchase = s.scalar(select(Purchase).where(Purchase.order_id == order_id).with_for_update())
            if not purchase:
                return "STALE_CLAIM"
            if not self._claimed(job, claim.token):
                # Do not overwrite a later terminal outcome. Preserve response evidence for review instead.
                if not purchase.encrypted_response_evidence:
                    purchase.encrypted_response_evidence = self._result_evidence(result)
                s.add(Audit(order_id=order.id, event="STALE_SUPPLIER_RESPONSE", detail={"status": result.status}))
                return "STALE_CLAIM"
            purchase.observed_debit, purchase.supplier_order_code = result.debited, result.order_code
            purchase.outcome = {"status": result.status}
            if result.status == "FAILED_NO_DEBIT":
                purchase.state, order.supplier_state, job.state = "FAILED_CONFIRMED", "FAILED_CONFIRMED", "DONE"
                return "FAILED_CONFIRMED"
            if result.status != "COMPLETED" or not result.delivery:
                purchase.state, order.supplier_state, job.state, job.last_error = "HOLD", "PENDING", "HOLD", "pending or malformed supplier response"
                return "HOLD"
            purchase.state, order.supplier_state, order.delivery_state, job.state = "COMPLETED", "COMPLETED", "QUEUED", "DONE"
            delivery = Delivery(order_id=order_id, buyer_id=order.buyer_id,
                                encrypted_content=self.box.encrypt(result.delivery.encode()).decode(), state="QUEUED")
            s.add(delivery)
            s.add(Job(order_id=order_id, kind="DELIVERY", state="QUEUED"))
            s.add(Audit(order_id=order_id, event="PURCHASE_COMPLETED", detail={"supplier_code": result.order_code}))
            return "COMPLETED"

    def _deliver(self, claim: Claim) -> str:
        # Validate and snapshot stored encrypted result while the claim is still current.
        with self.sessions.begin() as s:
            locked = self._lock_order_then_job(s, claim.job_id)
            if not locked:
                return "STALE_CLAIM"
            _, job = locked
            if not self._claimed(job, claim.token):
                return "STALE_CLAIM"
            delivery = s.scalar(select(Delivery).where(Delivery.order_id == job.order_id).with_for_update())
            if not delivery:
                job.state, job.last_error, job.lease_until = "HOLD", "delivery is missing", None
                return "HOLD"
            content, buyer_id, order_id = self.box.decrypt(delivery.encrypted_content.encode()).decode(), delivery.buyer_id, job.order_id
        try:
            ref = self.messenger.send(buyer_id, content)
        except TimeoutError:
            with self.sessions.begin() as s:
                locked = self._lock_order_then_job(s, claim.job_id)
                if not locked:
                    return "STALE_CLAIM"
                _, job = locked
                if not self._claimed(job, claim.token):
                    return "STALE_CLAIM"
                delivery = s.scalar(select(Delivery).where(Delivery.order_id == order_id).with_for_update())
                job.state, job.last_error, job.lease_until = "QUEUED", "simulated messaging failure", None
                delivery.state, delivery.attempts = "UNCERTAIN", delivery.attempts + 1
            return "RETRY_DELIVERY"
        with self.sessions.begin() as s:
            locked = self._lock_order_then_job(s, claim.job_id)
            if not locked:
                return "STALE_CLAIM"
            order, job = locked
            if not self._claimed(job, claim.token):
                return "STALE_CLAIM"
            delivery = s.scalar(select(Delivery).where(Delivery.order_id == order_id).with_for_update())
            job.state, delivery.state, delivery.message_reference = "DONE", "SENT", ref
            delivery.attempts += 1
            order.delivery_state = "SENT"
        return "DELIVERED"


def retrieve_delivery(session: Session, order_ref: str, buyer_id: int, fernet_key: str) -> str:
    order = session.scalar(select(Order).where(Order.public_ref == order_ref))
    if not order or order.buyer_id != buyer_id:
        raise PermissionError("order is not available to this buyer")
    delivery = session.scalar(select(Delivery).where(Delivery.order_id == order.id))
    if not delivery:
        raise LookupError("delivery is not ready")
    return Fernet(fernet_key.encode()).decrypt(delivery.encrypted_content.encode()).decode()
