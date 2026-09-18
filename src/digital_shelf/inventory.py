"""Encrypted inventory import and duplicate-detection primitives."""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Sequence

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class InventoryImportPreview:
    total_lines: int
    accepted_count: int
    duplicate_count: int
    blank_count: int
    invalid_count: int
    issues: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class EncryptedInventoryRecord:
    fingerprint: str
    ciphertext: str
    key_version: str


@dataclass(frozen=True)
class PreparedInventoryImport:
    preview: InventoryImportPreview
    records: tuple[EncryptedInventoryRecord, ...]


class InventoryCipher:
    """Encrypt stock with Fernet and fingerprint plaintext without storing it."""

    def __init__(self, *, key: str, key_version: str) -> None:
        if not isinstance(key, str) or not key_version.strip():
            raise ValueError("inventory encryption key and key version are required")
        try:
            self._cipher = Fernet(key.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("inventory encryption key is invalid") from exc
        self._fingerprint_key = hashlib.sha256(key.encode()).digest()
        self.key_version = key_version.strip()

    def __repr__(self) -> str:
        return f"InventoryCipher(key_version={self.key_version!r})"

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise ValueError("inventory value must be text")
        return self._cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str):
            raise ValueError("inventory ciphertext is invalid")
        try:
            return self._cipher.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("inventory ciphertext is invalid") from exc

    def fingerprint(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise ValueError("inventory value must be text")
        return hmac.new(
            self._fingerprint_key,
            plaintext.encode(),
            hashlib.sha256,
        ).hexdigest()


class InventoryImporter:
    """Prepare a safe two-stage import; committing records is a DB concern."""

    def __init__(self, cipher: InventoryCipher) -> None:
        self._cipher = cipher

    def prepare(
        self,
        lines: Sequence[str],
        existing_fingerprints: set[str],
    ) -> PreparedInventoryImport:
        if not 1 <= len(lines) <= 500:
            raise ValueError("inventory import must contain 1-500 lines")

        known = set(existing_fingerprints)
        records: list[EncryptedInventoryRecord] = []
        issues: list[tuple[int, str]] = []
        duplicate_count = 0
        blank_count = 0
        invalid_count = 0

        for line_number, raw in enumerate(lines, start=1):
            if not isinstance(raw, str):
                invalid_count += 1
                issues.append((line_number, "line is not text"))
                continue
            value = raw.strip()
            if not value:
                blank_count += 1
                continue
            if len(value) > 1_500:
                invalid_count += 1
                issues.append((line_number, "line exceeds 1500 characters"))
                continue
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                invalid_count += 1
                issues.append((line_number, "line contains control characters"))
                continue

            fingerprint = self._cipher.fingerprint(value)
            if fingerprint in known:
                duplicate_count += 1
                continue
            known.add(fingerprint)
            records.append(
                EncryptedInventoryRecord(
                    fingerprint=fingerprint,
                    ciphertext=self._cipher.encrypt(value),
                    key_version=self._cipher.key_version,
                )
            )

        preview = InventoryImportPreview(
            total_lines=len(lines),
            accepted_count=len(records),
            duplicate_count=duplicate_count,
            blank_count=blank_count,
            invalid_count=invalid_count,
            issues=tuple(issues),
        )
        return PreparedInventoryImport(preview=preview, records=tuple(records))
