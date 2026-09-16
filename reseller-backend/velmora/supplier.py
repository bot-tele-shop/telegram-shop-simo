from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import require_simulation


class SupplierTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class SupplierResult:
    status: str
    debited: bool
    order_code: str | None = None
    delivery: str | None = None


class SimulatedSupplier:
    """Independent durable mock ledger. It is deliberately not the business database."""
    def __init__(self, ledger_path: str | Path, mode: str = "complete", environment: str = "simulation"):
        require_simulation(environment)
        self.path = str(ledger_path)
        self.mode = mode
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS requests (k TEXT PRIMARY KEY, body_hash TEXT NOT NULL, calls INTEGER NOT NULL, debited INTEGER NOT NULL, status TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS debits (k TEXT PRIMARY KEY, code TEXT NOT NULL)")

    @staticmethod
    def _hash(request: dict) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def stats(self) -> dict[str, int]:
        with sqlite3.connect(self.path) as con:
            return {"requests": con.execute("SELECT count(*) FROM requests").fetchone()[0],
                    "calls": con.execute("SELECT coalesce(sum(calls),0) FROM requests").fetchone()[0],
                    "debits": con.execute("SELECT count(*) FROM debits").fetchone()[0]}

    def purchase(self, key: str, request: dict) -> SupplierResult:
        h = self._hash(request)
        # BEGIN IMMEDIATE obtains SQLite's single writer lock before SELECT/INSERT. This makes
        # same-key concurrent mock requests idempotent rather than leaking a unique-key exception.
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT body_hash, debited, status FROM requests WHERE k=?", (key,)).fetchone()
            if row:
                con.execute("UPDATE requests SET calls=calls+1 WHERE k=?", (key,))
                if row[0] != h:
                    con.commit()
                    return SupplierResult("IDEMPOTENCY_CONFLICT", bool(row[1]))
                mode, debited = row[2], bool(row[1])
            else:
                mode, debited = self.mode, self.mode in {"complete", "timeout_after_debit", "pending", "malformed"}
                con.execute("INSERT INTO requests VALUES (?, ?, 1, ?, ?)", (key, h, int(debited), mode))
                if debited:
                    con.execute("INSERT INTO debits VALUES (?, ?)", (key, f"SIM-ORDER-{key[:10]}"))
            code = f"SIM-ORDER-{key[:10]}"
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
        if mode == "timeout_after_debit":
            raise SupplierTimeout("simulated timeout after a debit; no delivery data")
        if mode == "no_debit_failure":
            return SupplierResult("FAILED_NO_DEBIT", False)
        if mode == "pending":
            return SupplierResult("PENDING", True, code)
        if mode == "malformed":
            return SupplierResult("COMPLETED", True, code, None)
        return SupplierResult("COMPLETED", True, code, f"SIMULATED-NOT-REAL-DIGITAL-VALUE:{key}")
