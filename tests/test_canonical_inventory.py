import pytest
from cryptography.fernet import Fernet

from digital_shelf.inventory import (
    InvalidInventoryTransition,
    InventoryCipher,
    InventoryImporter,
    InventoryImportPreview,
    InventoryState,
    transition_inventory_state,
)


@pytest.fixture
def cipher() -> InventoryCipher:
    key = Fernet.generate_key().decode()
    return InventoryCipher(key=key, key_version="v1")


def test_inventory_cipher_round_trips_without_exposing_plaintext() -> None:
    cipher = InventoryCipher(key=Fernet.generate_key().decode(), key_version="v1")
    token = cipher.encrypt("LICENSE-001")

    assert cipher.decrypt(token) == "LICENSE-001"
    assert cipher.fingerprint("LICENSE-001") == cipher.fingerprint("LICENSE-001")
    assert cipher.fingerprint("LICENSE-001") != cipher.fingerprint("LICENSE-002")
    assert "LICENSE-001" not in repr(token)


def test_import_preview_counts_blank_invalid_and_duplicate_lines(cipher: InventoryCipher) -> None:
    prepared = InventoryImporter(cipher).prepare(
        ["LICENSE-001", "LICENSE-001", "  ", "bad\nline", "x"],
        existing_fingerprints={cipher.fingerprint("x")},
    )

    assert prepared.preview == InventoryImportPreview(
        total_lines=5,
        accepted_count=1,
        duplicate_count=2,
        blank_count=1,
        invalid_count=1,
        issues=((4, "line contains control characters"),),
    )
    rendered = repr(prepared.preview) + repr(prepared.records)
    assert "LICENSE-001" not in rendered
    assert "LICENSE-002" not in rendered


def test_import_preview_does_not_mutate_existing_fingerprints(cipher: InventoryCipher) -> None:
    existing = {cipher.fingerprint("LICENSE-001")}
    prepared = InventoryImporter(cipher).prepare(["LICENSE-001", "LICENSE-002"], existing)

    assert prepared.preview.accepted_count == 1
    assert existing == {cipher.fingerprint("LICENSE-001")}


def test_import_rejects_more_than_500_lines(cipher: InventoryCipher) -> None:
    with pytest.raises(ValueError, match="1-500"):
        InventoryImporter(cipher).prepare(["x"] * 501, set())


def test_import_rejects_invalid_ciphertext() -> None:
    cipher = InventoryCipher(key=Fernet.generate_key().decode(), key_version="v1")

    with pytest.raises(ValueError, match="ciphertext"):
        cipher.decrypt("not-a-fernet-token")


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (InventoryState.AVAILABLE, InventoryState.RESERVED),
        (InventoryState.RESERVED, InventoryState.AVAILABLE),
        (InventoryState.RESERVED, InventoryState.SOLD),
        (InventoryState.SOLD, InventoryState.QUARANTINED),
        (InventoryState.AVAILABLE, InventoryState.RETIRED),
    ],
)
def test_inventory_state_transition_allows_only_forward_operational_moves(
    current: InventoryState, requested: InventoryState
) -> None:
    assert transition_inventory_state(current, requested) is requested


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (InventoryState.SOLD, InventoryState.AVAILABLE),
        (InventoryState.QUARANTINED, InventoryState.AVAILABLE),
        (InventoryState.RETIRED, InventoryState.AVAILABLE),
        (InventoryState.AVAILABLE, InventoryState.SOLD),
    ],
)
def test_inventory_state_transition_rejects_reactivation_or_skips(
    current: InventoryState, requested: InventoryState
) -> None:
    with pytest.raises(InvalidInventoryTransition):
        transition_inventory_state(current, requested)
