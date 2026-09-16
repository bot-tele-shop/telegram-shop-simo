from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .db import make_engine, validate_schema, session_factory
from .models import Order, Product
from .service import add_product, create_order, intake_payment
from .supplier import SimulatedSupplier
from .worker import SimulatedMessenger, Worker


def main() -> None:
    settings = Settings.from_env()
    schema = validate_schema(settings.schema)
    if not schema.startswith("demo_"):
        raise ValueError("offline demo refuses schemas not prefixed demo_")
    engine = make_engine(settings.database_url, schema)
    sessions = session_factory(engine)
    with sessions() as s:
        if s.scalar(select(Product.id).limit(1)) or s.scalar(select(Order.id).limit(1)):
            raise RuntimeError("offline demo refuses nonempty demo schema; use a fresh unique demo_ schema")
    supplied = os.getenv("SIMULATED_LEDGER")
    if supplied:
        ledger = Path(supplied)
        if ledger.exists():
            raise FileExistsError("refusing to overwrite existing SIMULATED_LEDGER; choose a new path")
    else:
        workspace = Path(tempfile.mkdtemp(prefix="velmora-demo-"))
        ledger = workspace / "supplier.sqlite"
    supplier, messenger = SimulatedSupplier(ledger), SimulatedMessenger()
    with sessions.begin() as s:
        product = add_product(s, "demo-approved-product", 42, "Offline approved demo")
        order = create_order(s, 4242, product.id)
        intake_payment(s, charge_id="demo-charge-001", payload=order.public_ref, buyer_id=4242, amount=42)
        intake_payment(s, charge_id="demo-charge-001", payload=order.public_ref, buyer_id=4242, amount=42)
    offline_worker = Worker(sessions, supplier, settings.fernet_key, messenger, "offline-demo")
    purchase, delivery = offline_worker.process_one(), offline_worker.process_one()
    # Intentional redaction: no reference, key, ledger path, ciphertext, or digital value is reported.
    print(json.dumps({"mode": "simulation", "purchase_job": purchase, "delivery_job": delivery,
                      "supplier": supplier.stats(), "messages_sent": len(messenger.sent),
                      "secrets_printed": False}, sort_keys=True))
    engine.dispose()


if __name__ == "__main__":
    main()
