"""Push the local catalog and available stock into Supabase.

The Cloudflare Worker reads products/stock from Supabase; the local CLI stays
the admin tool. Stock stays encrypted with the SAME Fernet key, so the Worker
decrypts exactly what the local tools encrypted.

Usage:
    export SUPABASE_URL="https://<project-ref>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="..."   # dashboard -> Settings -> API
    python tools/push_to_supabase.py

Safe to repeat: products upsert by sku, stock ignores duplicate fingerprints.
"""

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def postgrest(method, path, url, key, rows=None, prefer=None):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(rows).encode() if rows is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"PostgREST {method} {path} failed: {exc.code} {exc.read()[:300]!r}")


def main():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment")

    config = json.loads((ROOT / "config.local.json").read_text(encoding="utf-8"))
    db_path = Path(config.get("database_path", "data/shop-test.sqlite3"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    conn = sqlite3.connect(db_path)

    products = [
        {
            "sku": r[0], "title": r[1], "description": r[2], "category": r[3],
            "price_stars": r[4], "active": bool(r[5]), "is_demo": bool(r[6]), "source": r[7],
        }
        for r in conn.execute(
            "SELECT sku, title, description, category, price_stars, active, is_demo, source "
            "FROM products"
        )
    ]
    if products:
        postgrest(
            "POST", "products?on_conflict=sku", url, key,
            rows=products, prefer="resolution=merge-duplicates,return=minimal",
        )

    stock = [
        {"sku": r[0], "fingerprint": r[1], "ciphertext": r[2], "state": r[3]}
        for r in conn.execute(
            "SELECT sku, fingerprint, ciphertext, state FROM stock WHERE state = 'available'"
        )
    ]
    sent = 0
    for i in range(0, len(stock), 500):
        batch = stock[i : i + 500]
        postgrest(
            "POST", "stock", url, key,
            rows=batch, prefer="resolution=ignore-duplicates,return=minimal",
        )
        sent += len(batch)

    print(f"Pushed {len(products)} products (upserted) and {sent} stock items (dupes skipped).")
    print("Repeat after importing new local stock with: python -m shop stock --sku ... --file ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
