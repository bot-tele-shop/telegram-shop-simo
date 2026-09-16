from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from shop.store import Store


@pytest.fixture
def clock():
    # Relative application clock for tests, not a Unix timestamp or API input.
    value = [100.0]
    return value


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def store(tmp_path: Path, key, clock):
    db = Store(tmp_path / "test.sqlite3", key, clock=lambda: clock[0])
    db.initialize()
    db.upsert_product(
        {
            "sku": "sample-key",
            "title": "Licensed sample",
            "description": "A test-only digital key",
            "category": "Keys",
            "price_stars": 25,
            "is_demo": True,
        }
    )
    db.import_stock("sample-key", ["LICENSE-001", "LICENSE-002"])
    db.accept_terms(101, "terms-v1")
    return db


@pytest.fixture
def order(store):
    return store.create_order(101, "sample-key", "terms-v1")


@pytest.fixture
def paid(store, order):
    store.approve_checkout(order["id"], 101, "XTR", 25, "query-1", "terms-v1")
    store.record_payment(order["id"], 101, "XTR", 25, "charge-1")
    return order
