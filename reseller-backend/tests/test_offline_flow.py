from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, inspect, select

from velmora.config import Settings, UnsafeModeError
from velmora.db import make_engine, validate_schema
from velmora.models import Delivery, Job, Order, Payment, Purchase
from velmora.service import add_product, create_order, intake_payment, now
from velmora.supplier import SimulatedSupplier
from velmora.worker import SimulatedMessenger, Worker, retrieve_delivery


def prepared(runtime, buyer: int = 1001):
    with runtime["sessions"].begin() as s:
        product = add_product(s, f"demo-product-{uuid4().hex}", 42)
        order = create_order(s, buyer, product.id)
        return order.public_ref, order.id


def pay(runtime, ref, charge="charge-1", buyer=1001, amount=42, currency="XTR"):
    with runtime["sessions"].begin() as s:
        return intake_payment(s, charge_id=charge, payload=ref, buyer_id=buyer, amount=amount, currency=currency).id


def worker(runtime, mode="complete", messenger=None, owner="worker-a", supplier=None):
    return Worker(runtime["sessions"], supplier or SimulatedSupplier(runtime["ledger"], mode), runtime["key"],
                  messenger or SimulatedMessenger(), owner)


def counts(runtime):
    with runtime["sessions"]() as s:
        return (s.scalar(select(func.count(Payment.id))), s.scalar(select(func.count(Purchase.id))),
                s.scalar(select(func.count(Delivery.id))))


def test_full_flow_bigint_duplicate_and_private_retrieval(runtime):
    buyer = 2**32 + 123
    ref, order_id = prepared(runtime, buyer)
    pay(runtime, ref, buyer=buyer)
    pay(runtime, ref, buyer=buyer)  # same durable charge replay
    messenger = SimulatedMessenger()
    assert worker(runtime, messenger=messenger, owner="restart-one").process_one() == "COMPLETED"
    assert worker(runtime, messenger=messenger, owner="restart-two").process_one() == "DELIVERED"
    assert counts(runtime) == (1, 1, 1)
    assert worker(runtime).supplier.stats() == {"requests": 1, "calls": 1, "debits": 1}
    with runtime["sessions"]() as s:
        assert retrieve_delivery(s, ref, buyer, runtime["key"]).startswith("SIMULATED-NOT-REAL")
        with pytest.raises(PermissionError):
            retrieve_delivery(s, ref, 9999, runtime["key"])
        with pytest.raises(InvalidToken):
            retrieve_delivery(s, ref, buyer, Fernet.generate_key().decode())
    # A synthetic stale queued purchase job may not overwrite a completed receipt with an uncertainty hold.
    with runtime["sessions"].begin() as s:
        job = s.scalar(select(Job).where(Job.kind == "PURCHASE"))
        job.state, job.lease_until = "QUEUED", None
    assert worker(runtime, owner="stale-receipt").process_one() == "ALREADY_COMPLETED"
    with runtime["sessions"]() as s:
        assert s.scalar(select(Purchase).where(Purchase.order_id == order_id)).state == "COMPLETED"


def test_conflicting_eligible_charge_cancels_queued_work_and_never_debits(runtime):
    ref, _ = prepared(runtime)
    pay(runtime, ref, "same", amount=42)
    pay(runtime, ref, "same", amount=99)
    supplier = SimulatedSupplier(runtime["ledger"])
    assert worker(runtime, supplier=supplier).process_one() is None
    with runtime["sessions"]() as s:
        order = s.scalar(select(Order).where(Order.public_ref == ref))
        payment = s.scalar(select(Payment).where(Payment.telegram_charge_id == "same"))
        job = s.scalar(select(Job).where(Job.order_id == order.id, Job.kind == "PURCHASE"))
        assert payment.state == "REVIEW" and payment.conflict_payload["amount"] == 99
        assert order.payment_state == "REVIEW" and job.state == "CANCELLED"
    assert supplier.stats()["debits"] == 0


def test_synthetic_unpaid_job_is_blocked_before_supplier(runtime):
    ref, order_id = prepared(runtime)
    with runtime["sessions"].begin() as s:
        s.add(Job(order_id=order_id, kind="PURCHASE", state="QUEUED"))
    supplier = SimulatedSupplier(runtime["ledger"])
    assert worker(runtime, supplier=supplier).process_one() == "HOLD"
    assert supplier.stats() == {"requests": 0, "calls": 0, "debits": 0}
    with runtime["sessions"]() as s:
        assert s.get(Order, order_id).payment_state == "UNPAID"


def test_distinct_review_late_wrong_and_anomalous_events_are_preserved(runtime):
    ref, _ = prepared(runtime)
    pay(runtime, ref, "c1")
    pay(runtime, ref, "c2")
    pay(runtime, ref, "wrong-buyer", buyer=77)
    pay(runtime, ref, "wrong-amount", amount=41)
    pay(runtime, ref, "wrong-currency", currency="USD")
    pay(runtime, "unknown", "unknown")
    with runtime["sessions"].begin() as s:
        order = s.scalar(select(Order).where(Order.public_ref == ref))
        order.invoice_expires_at = now() - timedelta(seconds=1)
    pay(runtime, ref, "late")
    pay(runtime, ref, "float", amount=42.0)
    pay(runtime, ref, "bool", amount=True)
    with runtime["sessions"]() as s:
        rows = s.scalars(select(Payment).order_by(Payment.telegram_charge_id)).all()
        assert len(rows) == 9
        assert sum(p.state == "ELIGIBLE" for p in rows) == 1
        reasons = {p.review_reason for p in rows if p.state == "REVIEW"}
        assert {"DUPLICATE_ORDER_CHARGE", "MISMATCHED_PAYMENT", "UNKNOWN_ORDER", "LATE_PAYMENT", "ANOMALOUS_FINANCIAL_EVENT"} <= reasons
        assert s.scalar(select(Payment).where(Payment.telegram_charge_id == "float")).raw_event["amount"] == 42.0
        assert s.scalar(select(Payment).where(Payment.telegram_charge_id == "bool")).amount_stars == 0


def test_fresh_migration_matches_bigint_columns_and_model_indexes(runtime):
    inspector = inspect(runtime["engine"])
    assert "BIGINT" in str({c["name"]: c["type"] for c in inspector.get_columns("orders")}["buyer_id"]).upper()
    assert "BIGINT" in str({c["name"]: c["type"] for c in inspector.get_columns("payments")}["buyer_id"]).upper()
    assert "BIGINT" in str({c["name"]: c["type"] for c in inspector.get_columns("deliveries")}["buyer_id"]).upper()
    assert {"ix_orders_buyer_id"} <= {i["name"] for i in inspector.get_indexes("orders")}
    assert {"ix_payments_order_id"} <= {i["name"] for i in inspector.get_indexes("payments")}
    assert {"ix_jobs_order_id", "ix_jobs_state", "ix_jobs_claim_token"} <= {i["name"] for i in inspector.get_indexes("jobs")}


def test_price_validation_and_schema_validation(runtime):
    with runtime["sessions"].begin() as s:
        with pytest.raises(ValueError):
            add_product(s, "float", 1.0)
        with pytest.raises(ValueError):
            add_product(s, "bool", True)
    for name in ("", "public", "pg_catalog", "pg_x", "bad-name", "1bad"):
        with pytest.raises(ValueError):
            validate_schema(name)
    assert validate_schema("demo_ok_1") == "demo_ok_1"
    with pytest.raises(ValueError):
        make_engine(runtime["url"], "public")


def test_real_concurrent_intake_claim_and_consistent_lock_order(runtime):
    ref, _ = prepared(runtime)

    def submit(charge):
        with runtime["sessions"].begin() as s:
            return intake_payment(s, charge_id=charge, payload=ref, buyer_id=1001, amount=42).state

    with ThreadPoolExecutor(max_workers=2) as ex:
        assert sorted(ex.map(submit, ["parallel-a", "parallel-b"])) == ["ELIGIBLE", "REVIEW"]
    a, b = worker(runtime, owner="a"), worker(runtime, owner="b")
    with ThreadPoolExecutor(max_workers=2) as ex:
        claims = list(ex.map(lambda w: w.claim_one(), (a, b)))
    assert sum(c is not None for c in claims) == 1  # actual PostgreSQL SKIP LOCKED competition

    # Worker and conflict intake both lock order then job. Completion within this timeout detects deadlock regressions.
    claim = next(c for c in claims if c)
    c = worker(runtime, owner="consistent-lock", supplier=SimulatedSupplier(runtime["ledger"]))
    # Reclaim expired claim to give c a valid fenced claim.
    with runtime["sessions"].begin() as s:
        s.get(Job, claim.job_id).lease_until = now() - timedelta(seconds=1)
    fresh = c.claim_one()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(c._purchase, fresh, False), ex.submit(submit, "parallel-a")]
        [f.result(timeout=10) for f in futures]


def test_checkpoint_real_subprocess_engine_recovery(runtime):
    ref, _ = prepared(runtime)
    pay(runtime, ref)
    code = """
from velmora.db import make_engine, session_factory
from velmora.supplier import SimulatedSupplier
from velmora.worker import SimulatedMessenger, Worker
import os
s = session_factory(make_engine(os.environ['DATABASE_URL'], os.environ['DATABASE_SCHEMA']))
w = Worker(s, SimulatedSupplier(os.environ['LEDGER']), os.environ['FERNET_KEY'], SimulatedMessenger(), 'subprocess')
print(w.process_one(checkpoint_only=True))
"""
    env = {**os.environ, "DATABASE_URL": runtime["url"], "DATABASE_SCHEMA": runtime["schema"],
           "FERNET_KEY": runtime["key"], "LEDGER": str(runtime["ledger"])}
    result = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "CHECKPOINT_SAVED"
    with runtime["sessions"].begin() as s:
        job = s.scalar(select(Job).where(Job.kind == "PURCHASE"))
        job.lease_until = now() - timedelta(seconds=1)
    assert worker(runtime, owner="recovered").process_one() == "HOLD"
    assert worker(runtime).supplier.stats()["debits"] == 0


def test_timeout_pending_malformed_and_delivery_retry_do_not_buy_again(runtime):
    ref, _ = prepared(runtime)
    pay(runtime, ref, "timeout-charge")
    timeout_worker = worker(runtime, mode="timeout_after_debit", owner="timeout")
    assert timeout_worker.process_one() == "HOLD"
    assert timeout_worker.supplier.stats()["debits"] == 1
    with runtime["sessions"]() as s:
        order = s.scalar(select(Order).where(Order.public_ref == ref))
        assert order.supplier_state == "UNCERTAIN"

    for mode in ("pending", "malformed"):
        ref, _ = prepared(runtime)
        pay(runtime, ref, f"{mode}-charge")
        assert worker(runtime, mode=mode, owner=mode).process_one() == "HOLD"
    ref, _ = prepared(runtime)
    pay(runtime, ref, "delivery-charge")
    failing = SimulatedMessenger(fail=True)
    w = worker(runtime, messenger=failing, owner="delivery")
    assert w.process_one() == "COMPLETED"
    assert w.process_one() == "RETRY_DELIVERY"
    calls = w.supplier.stats()["calls"]
    w.messenger.fail = False
    assert w.process_one() == "DELIVERED"
    assert w.supplier.stats()["calls"] == calls


class BlockingSupplier(SimulatedSupplier):
    def __init__(self, *args, started: threading.Event, release: threading.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.started, self.release = started, release

    def purchase(self, key, request):
        result = super().purchase(key, request)  # debit/identity becomes durable before the controlled delay
        self.started.set()
        assert self.release.wait(timeout=10)
        return result


def test_lease_overlap_fencing_keeps_one_debit_and_encrypted_stale_receipt(runtime):
    ref, order_id = prepared(runtime)
    pay(runtime, ref)
    started, release = threading.Event(), threading.Event()
    supplier = BlockingSupplier(runtime["ledger"], started=started, release=release)
    first = worker(runtime, owner="first", supplier=supplier)
    with ThreadPoolExecutor(max_workers=1) as ex:
        running = ex.submit(first.process_one)
        assert started.wait(timeout=10)
        with runtime["sessions"].begin() as s:
            job = s.scalar(select(Job).where(Job.order_id == order_id, Job.kind == "PURCHASE"))
            job.lease_until = now() - timedelta(seconds=1)
        # Second worker reclaims the lease but sees the single saved identity and holds; it does not re-buy.
        assert worker(runtime, owner="second", supplier=supplier).process_one() == "HOLD"
        release.set()
        assert running.result(timeout=10) == "STALE_CLAIM"
    assert supplier.stats() == {"requests": 1, "calls": 1, "debits": 1}
    with runtime["sessions"]() as s:
        purchase = s.scalar(select(Purchase).where(Purchase.order_id == order_id))
        job = s.scalar(select(Job).where(Job.order_id == order_id, Job.kind == "PURCHASE"))
        assert purchase.state == "IN_FLIGHT" and purchase.encrypted_response_evidence
        assert b"SIMULATED-NOT-REAL" in Fernet(runtime["key"].encode()).decrypt(purchase.encrypted_response_evidence.encode())
        assert job.state == "HOLD"


def test_settings_and_mock_supplier_live_mode_are_rejected(runtime, monkeypatch):
    monkeypatch.setenv("VELMORA_MODE", "live")
    with pytest.raises(UnsafeModeError):
        Settings.from_env()
    monkeypatch.setenv("VELMORA_MODE", "simulation")
    monkeypatch.setenv("FERNET_KEY", runtime["key"])
    monkeypatch.setenv("DATABASE_URL", runtime["url"])
    assert Settings.from_env().mode == "simulation"


def test_mock_supplier_concurrent_replay_is_one_debit_and_live_rejected(runtime):
    supplier = SimulatedSupplier(runtime["ledger"])
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: supplier.purchase("same-key", {"product_id": "a", "quantity": 1}), range(2)))
    assert all(r.status == "COMPLETED" for r in results)
    assert supplier.stats() == {"requests": 1, "calls": 2, "debits": 1}
    assert supplier.purchase("same-key", {"product_id": "b", "quantity": 1}).status == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(UnsafeModeError):
        SimulatedSupplier(runtime["ledger"], environment="live")
